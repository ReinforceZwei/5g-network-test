"""Report generation: markdown summary (+ optional PNG chart) from the CSVs."""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .ping import detect_default_gateway


# ---------------------------------------------------------------- loading

def _load_csv_rows(log_dir: Path, prefix: str, date: str | None) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(log_dir.glob(f"{prefix}_*.csv")):
        if date and not path.name.endswith(f"_{date}.csv"):
            continue
        with open(path, newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- stats

def _fmt(v, digits=2) -> str:
    if v is None or v == "":
        return "-"
    try:
        return f"{float(v):.{digits}f}"
    except (ValueError, TypeError):
        return str(v)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _per_target_stats(ping_rows: list[dict]) -> dict:
    by_target: dict[str, list[dict]] = defaultdict(list)
    for r in ping_rows:
        by_target[r.get("target", "")].append(r)

    stats: dict[str, dict] = {}
    for target, rows in by_target.items():
        rows.sort(key=lambda r: r["ts"])
        rtts: list[float] = []
        timeouts = 0
        for r in rows:
            v = r.get("rtt_ms")
            if r.get("status") == "timeout" or v in (None, ""):
                timeouts += 1
            else:
                try:
                    rtts.append(float(v))
                except ValueError:
                    timeouts += 1
        s = sorted(rtts)
        stats[target] = {
            "probes": len(rows),
            "ok": len(rtts),
            "timeouts": timeouts,
            "loss_pct": 100.0 * timeouts / len(rows) if rows else None,
            "min": s[0] if s else None,
            "avg": statistics.mean(rtts) if rtts else None,
            "max": s[-1] if s else None,
            "p95": _pct(s, 0.95),
            "p99": _pct(s, 0.99),
            "jitter": statistics.pstdev(rtts) if len(rtts) > 1 else (0.0 if rtts else None),
        }
    return stats


def _find_outages(ping_rows: list[dict], interval_sec: float, min_consecutive: int = 3) -> list[dict]:
    """Consecutive timeout runs (>= min_consecutive) per target, as periods."""
    by_target: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    for r in ping_rows:
        dt = _parse_ts(r.get("ts", ""))
        if dt is None:
            continue
        by_target[r.get("target", "")].append((dt, r))

    outages: list[dict] = []
    for target, items in by_target.items():
        items.sort(key=lambda x: x[0])
        run: list[tuple[datetime, dict]] = []
        gap = interval_sec * 3
        for dt, r in items:
            lost = r.get("status") == "timeout" or r.get("rtt_ms") in (None, "")
            if lost:
                if run and (dt - run[-1][0]).total_seconds() > gap:
                    if len(run) >= min_consecutive:
                        outages.append(_outage(target, run, interval_sec))
                    run = []
                run.append((dt, r))
            else:
                if len(run) >= min_consecutive:
                    outages.append(_outage(target, run, interval_sec))
                run = []
        if len(run) >= min_consecutive:
            outages.append(_outage(target, run, interval_sec))
    outages.sort(key=lambda o: o["start"])
    return outages


def _outage(target: str, run: list, interval_sec: float) -> dict:
    return {
        "target": target,
        "start": run[0][0],
        "end": run[-1][0],
        "count": len(run),
        "duration_s": (run[-1][0] - run[0][0]).total_seconds() + interval_sec,
    }


def _speed_summary(speed_rows: list[dict]) -> dict | None:
    if not speed_rows:
        return None

    def series(key: str) -> list[float]:
        vals = []
        for r in speed_rows:
            try:
                vals.append(float(r[key]))
            except (ValueError, TypeError, KeyError):
                pass
        return vals

    def mm(vals):
        if not vals:
            return (None, None, None)
        return (min(vals), statistics.mean(vals), max(vals))

    return {
        "count": len(speed_rows),
        "download": mm(series("download_mbps")),
        "upload": mm(series("upload_mbps")),
        "ping": mm(series("ping_ms")),
        "jitter": mm(series("jitter_ms")),
    }


# ---------------------------------------------------------------- chart

def _make_chart(ping_rows: list[dict], speed_rows: list[dict], out_png: Path) -> bool:
    """Optional matplotlib chart. Returns False when matplotlib is unavailable."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception:
        return False
    try:
        fig, axes = plt.subplots(3, 1, figsize=(13, 11), dpi=110)

        # 1) RTT time series per target
        ax = axes[0]
        by_target: dict[str, list] = defaultdict(list)
        for r in ping_rows:
            dt = _parse_ts(r.get("ts", ""))
            if dt is None:
                continue
            try:
                v = float(r["rtt_ms"])
            except (ValueError, TypeError, KeyError):
                continue
            by_target[r.get("target", "")].append((dt, v))
        for target, pts in by_target.items():
            pts.sort(key=lambda x: x[0])
            if len(pts) > 4000:  # downsample for readability
                pts = pts[:: (len(pts) // 4000) + 1]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], lw=0.7, label=target)
        ax.set_ylabel("RTT (ms)")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(alpha=0.3)

        # 2) Download / upload over time
        ax = axes[1]
        pts = [
            (dt, float(r["download_mbps"]), float(r["upload_mbps"]))
            for r in speed_rows
            if (dt := _parse_ts(r.get("ts", ""))) and r.get("download_mbps") not in (None, "")
        ]
        if pts:
            pts.sort(key=lambda x: x[0])
            xs = [p[0] for p in pts]
            ax.plot(xs, [p[1] for p in pts], marker="o", ms=4, label="download")
            ax.plot(xs, [p[2] for p in pts], marker="s", ms=4, label="upload")
        ax.set_ylabel("Mbps")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

        # 3) Loss % per 10-minute bucket per target
        ax = axes[2]
        buckets: dict[str, dict[datetime, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in ping_rows:
            dt = _parse_ts(r.get("ts", ""))
            if dt is None:
                continue
            b = dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)
            cell = buckets[r.get("target", "")][b]
            cell[1] += 1
            if r.get("status") == "ok" and r.get("rtt_ms") not in (None, ""):
                cell[0] += 1
        for target, bmap in buckets.items():
            items = sorted(bmap.items())
            xs = [it[0] for it in items]
            ys = [100.0 * (1 - it[1][0] / it[1][1]) for it in items]
            ax.plot(xs, ys, marker=".", ms=3, lw=0.7, label=target)
        ax.set_ylabel("Loss % (10-min)")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

        for a in axes:
            a.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            a.tick_params(axis="x", labelsize=7)
        fig.tight_layout()
        fig.savefig(out_png)
        plt.close(fig)
        return True
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- report

def generate_report(cfg: Config, date: str | None = None, out_dir: Path | None = None) -> Path:
    """Generate a markdown report.

    date: "YYYYMMDD" for a single day, or None for all available days (the
    combined 7-day verdict report). Returns the report file path.
    """
    out_dir = out_dir or cfg.report_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    label = date if date else "all-days"

    ping_rows = _load_csv_rows(cfg.log_dir, "ping", date)
    speed_rows = _load_csv_rows(cfg.log_dir, "speedtest", date)

    lines: list[str] = []
    lines.append(f"# 5G Network Quality Report — {label}")
    lines.append("")
    lines.append(f"- Generated: {_now_iso()}")
    lines.append(f"- Targets: {', '.join(cfg.effective_targets)}")
    lines.append(
        f"- Ping interval: {cfg.ping_interval_sec:g}s (timeout {cfg.ping_timeout_sec:g}s), "
        f"speedtest every {cfg.speedtest_interval_min} min"
    )
    lines.append("")

    if not ping_rows and not speed_rows:
        lines.append("**No data available for this period yet.**")
        lines.append("")
        lines.append("The monitor may not have run, or logs were cleaned up.")

    if ping_rows:
        first = min(_parse_ts(r["ts"]) for r in ping_rows if _parse_ts(r["ts"]))
        last = max(_parse_ts(r["ts"]) for r in ping_rows if _parse_ts(r["ts"]))
        n_targets = len({r.get("target") for r in ping_rows})
        expected = int((last - first).total_seconds() / cfg.ping_interval_sec) + 1
        expected *= n_targets  # one row per target per round
        coverage = 100.0 * len(ping_rows) / expected if expected else None
        lines.append("## Coverage")
        lines.append("")
        lines.append(f"- Period: {first} → {last}")
        lines.append(
            f"- Samples: {len(ping_rows)} / {expected} expected "
            f"({_fmt(coverage, 1)}% — a gap means the Pi was off/rebooting/unreachable)"
        )
        lines.append("")

        stats = _per_target_stats(ping_rows)
        lines.append("## Ping results (per target)")
        lines.append("")
        lines.append("| target | probes | ok | loss % | min | avg | max | p95 | p99 | jitter |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for t, s in stats.items():
            lines.append(
                f"| {t} | {s['probes']} | {s['ok']} | {_fmt(s['loss_pct'], 2)} | "
                f"{_fmt(s['min'])} | {_fmt(s['avg'])} | {_fmt(s['max'])} | "
                f"{_fmt(s['p95'])} | {_fmt(s['p99'])} | {_fmt(s['jitter'])} |"
            )
        lines.append("")

        outages = _find_outages(ping_rows, cfg.ping_interval_sec)
        if outages:
            shown = outages[:20]
            lines.append(f"## Outage events (≥3 consecutive timeouts; showing {len(shown)}/{len(outages)})")
            lines.append("")
            lines.append("| start | end | duration (s) | target |")
            lines.append("|---|---|---|---|")
            for o in shown:
                lines.append(
                    f"| {o['start']} | {o['end']} | {o['duration_s']:.0f} | {o['target']} |"
                )
            lines.append("")

    if speed_rows:
        lines.append("## Speed tests")
        lines.append("")
        lines.append("| ts | engine | server | ISP | external IP | ping ms | jitter ms | dl Mbps | ul Mbps | loss % |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in speed_rows:
            lines.append(
                f"| {r.get('ts', '')} | {r.get('engine', '')} | {r.get('server', '')} | "
                f"{r.get('isp', '')} | {r.get('external_ip', '')} | {_fmt(r.get('ping_ms'))} | "
                f"{_fmt(r.get('jitter_ms'))} | {_fmt(r.get('download_mbps'))} | "
                f"{_fmt(r.get('upload_mbps'))} | {_fmt(r.get('packet_loss_pct'))} |"
            )
        lines.append("")
        summary = _speed_summary(speed_rows)
        if summary:
            lines.append("### Speed test summary (min / avg / max)")
            lines.append("")
            lines.append("| metric | min | avg | max |")
            lines.append("|---|---|---|---|")
            for name, vals in (
                ("download (Mbps)", summary["download"]),
                ("upload (Mbps)", summary["upload"]),
                ("ping (ms)", summary["ping"]),
                ("jitter (ms)", summary["jitter"]),
            ):
                lines.append(
                    f"| {name} | {_fmt(vals[0])} | {_fmt(vals[1])} | {_fmt(vals[2])} |"
                )
            lines.append("")

    # External IP info from speedtest rows + a best-effort current IP
    ext_ips = [r.get("external_ip", "") for r in speed_rows if r.get("external_ip")]
    seen_ips = list(dict.fromkeys(ext_ips))
    if seen_ips:
        lines.append("## External IP (CGNAT / rebind check)")
        lines.append("")
        lines.append(f"- Distinct IPs seen: {len(seen_ips)} — {', '.join(seen_ips)}")
        lines.append("")
        if len(seen_ips) > 1:
            lines.append("> ⚠️ The public IP changed during the test window (typical for CGNAT on 5G).")
            lines.append("")

    # Assessment vs thresholds
    th = cfg.thresholds or {}
    if ping_rows and th:
        stats = _per_target_stats(ping_rows)
        # Exclude the LAN gateway from the WAN assessment (its loss/latency is
        # a router/LAN concern, not a 5G-plan concern).
        gw = detect_default_gateway() if cfg.auto_add_gateway else None
        wan_stats = {t: s for t, s in stats.items() if t != gw}
        if not wan_stats:
            wan_stats = stats
        worst_loss = max((s["loss_pct"] or 0.0) for s in wan_stats.values())
        avg_pings = [s["avg"] for s in wan_stats.values() if s["avg"] is not None]
        avg_ping = statistics.mean(avg_pings) if avg_pings else None
        summary = _speed_summary(speed_rows)
        avg_dl = summary["download"][1] if summary else None
        avg_ul = summary["upload"][1] if summary else None

        lines.append("## Assessment (vs configured thresholds)")
        lines.append("")
        lines.append("| metric | value | threshold | verdict |")
        lines.append("|---|---|---|---|")
        verdicts = []
        checks = [
            ("worst target loss %", worst_loss, th.get("loss_pct"), "low"),
            ("average ping (ms)", avg_ping, th.get("avg_ping_ms"), "low"),
            ("average download (Mbps)", avg_dl, th.get("min_download_mbps"), "high"),
            ("average upload (Mbps)", avg_ul, th.get("min_upload_mbps"), "high"),
        ]
        for name, value, thr, better in checks:
            if value is None or thr is None:
                verdicts.append("N/A")
                lines.append(f"| {name} | {_fmt(value)} | {_fmt(thr)} | N/A |")
            else:
                ok = value <= thr if better == "low" else value >= thr
                verdicts.append("PASS" if ok else "FAIL")
                lines.append(f"| {name} | {_fmt(value)} | {_fmt(thr)} | {'PASS ✅' if ok else 'FAIL ❌'} |")
        lines.append("")
        if "FAIL" in verdicts:
            verdict = "NOT ACCEPTABLE — consider cancelling within the free-cancellation window"
        else:
            verdict = "ACCEPTABLE — keep the plan"
        lines.append(f"**Overall verdict: {verdict}**")
        lines.append("")
        lines.append("> The verdict is a mechanical check against the thresholds in config.json.")
        lines.append("> Adjust them to match your own expectations (e.g. gaming needs low jitter).")

    md = "\n".join(lines) + "\n"
    report_path = out_dir / f"report_{label}.md"
    report_path.write_text(md)
    (out_dir / "latest.md").write_text(md)

    png = out_dir / f"report_{label}.png"
    if _make_chart(ping_rows, speed_rows, png):
        (out_dir / "latest.png").write_bytes(png.read_bytes())
    return report_path
