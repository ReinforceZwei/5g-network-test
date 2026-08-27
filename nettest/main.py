"""5G network quality tester — main entry point.

Modes:
    python -m nettest.main [--config path]              # 7x24 monitoring
    python -m nettest.main --once [--config path]       # one probe round + one speedtest
    python -m nettest.main --report [YYYY-MM-DD]        # report (all days if no date)
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config
from .ping import detect_default_gateway, probe_once
from .report import generate_report
from .speedtest import SpeedtestError, run_speedtest
from .storage import DailyCSV, cleanup_old

log = logging.getLogger("nettest")

PING_FIELDS = ["ts", "target", "status", "rtt_ms"]
SPEED_FIELDS = [
    "ts", "engine", "server", "isp", "external_ip",
    "ping_ms", "jitter_ms", "download_mbps", "upload_mbps", "packet_loss_pct",
]
EVENT_FIELDS = ["ts", "level", "message"]

IP_PROVIDERS = (
    "https://api.ipinfo.io/ip",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _day_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _num(v):
    if v in (None, ""):
        return None
    try:
        return round(float(v), 2)
    except (ValueError, TypeError):
        return None


def _fetch_external_ip() -> str | None:
    for url in IP_PROVIDERS:
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    return None


class Monitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.ping_csv = DailyCSV(cfg.log_dir, "ping", PING_FIELDS)
        self.speed_csv = DailyCSV(cfg.log_dir, "speedtest", SPEED_FIELDS)
        self.event_csv = DailyCSV(cfg.log_dir, "events", EVENT_FIELDS)
        self.speedtest_running = False
        self._start_ts = time.time()
        self.status: dict = {
            "started_at": _now_iso(),
            "version": __version__,
            "targets": [],
            "gateway": None,
            "per_target": {},
            "last_speedtest": None,
            "last_external_ip": None,
            "ip_changes": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------ helpers

    def event(self, level: str, msg: str) -> None:
        log.log(getattr(logging, level.upper(), logging.INFO), msg)
        self.event_csv.write(_day_str(), {"ts": _now_iso(), "level": level, "message": msg})

    def _probe_round(self) -> None:
        ts = _now_iso()
        day = _day_str()
        rows = []
        with self.lock:
            st = self.status["per_target"]
            for t in self.cfg.effective_targets:
                ok, rtt = probe_once(t, self.cfg.ping_timeout_sec)
                rows.append({
                    "ts": ts,
                    "target": t,
                    "status": "ok" if ok else "timeout",
                    "rtt_ms": f"{rtt:.2f}" if ok else "",
                })
                info = st.setdefault(t, {
                    "last_ok": None, "last_rtt_ms": None,
                    "consecutive_losses": 0, "probes": 0,
                })
                info["probes"] += 1
                if ok:
                    info["last_ok"] = ts
                    info["last_rtt_ms"] = rtt
                    info["consecutive_losses"] = 0
                else:
                    info["consecutive_losses"] += 1
        self.ping_csv.write(day, rows)

    def _speedtest_worker(self) -> None:
        self.speedtest_running = True
        try:
            self.event("info", "speedtest starting")
            res = run_speedtest(self.cfg.speedtest_ookla_bin, self.cfg.speedtest_timeout_sec)
            row = {
                "ts": _now_iso(),
                "engine": res["engine"],
                "server": res.get("server", ""),
                "isp": res.get("isp", ""),
                "external_ip": res.get("external_ip", ""),
                "ping_ms": _num(res.get("ping_ms")),
                "jitter_ms": _num(res.get("jitter_ms")),
                "download_mbps": _num(res.get("download_mbps")),
                "upload_mbps": _num(res.get("upload_mbps")),
                "packet_loss_pct": _num(res.get("packet_loss_pct")),
            }
            self.speed_csv.write(_day_str(), row)
            with self.lock:
                self.status["last_speedtest"] = row["ts"]
                if row["external_ip"]:
                    self.status["last_external_ip"] = row["external_ip"]
            self.event(
                "info",
                f"speedtest ok: dl={row['download_mbps']} Mbps ul={row['upload_mbps']} "
                f"Mbps ping={row['ping_ms']} ms",
            )
        except SpeedtestError as exc:
            self.event("error", f"speedtest failed: {exc}")
            with self.lock:
                self.status["errors"] += 1
        finally:
            self.speedtest_running = False

    def _ip_check(self) -> None:
        ip = _fetch_external_ip()
        if not ip:
            return
        with self.lock:
            prev = self.status["last_external_ip"]
            if prev and prev != ip:
                self.status["ip_changes"] += 1
                self.event("warn", f"external IP changed: {prev} -> {ip}")
            self.status["last_external_ip"] = ip
        if not prev:
            self.event("info", f"external IP: {ip}")

    def _write_status(self) -> None:
        with self.lock:
            status = dict(self.status)
        status["now"] = _now_iso()
        status["uptime_sec"] = int(time.time() - self._start_ts)
        try:
            (self.cfg.root / "status.json").write_text(json.dumps(status, indent=2))
        except OSError as exc:
            log.warning("could not write status.json: %s", exc)

    def _rollover_if_needed(self, last_day: str) -> str:
        day = _day_str()
        if day == last_day:
            return last_day
        self.ping_csv.flush()
        self.speed_csv.flush()
        self.event_csv.flush()
        try:
            generate_report(self.cfg, date=last_day)
            log.info("daily report generated for %s", last_day)
        except Exception as exc:  # report must never kill the monitor
            log.warning("daily report failed: %s", exc)
        for prefix in ("ping", "speedtest", "events"):
            cleanup_old(self.cfg.log_dir, prefix, self.cfg.retention_days)
        return day

    # ------------------------------------------------------------ lifecycle

    def _signal_handler(self, signum, _frame):
        log.info("signal %s received, shutting down", signum)
        self.stop_event.set()

    def run(self) -> None:
        cfg = self.cfg
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        self.status["targets"] = list(cfg.effective_targets)
        if cfg.auto_add_gateway:
            self.status["gateway"] = detect_default_gateway()
        self.event(
            "info",
            f"monitor started: targets={cfg.effective_targets} "
            f"ping_interval={cfg.ping_interval_sec:g}s "
            f"speedtest_interval={cfg.speedtest_interval_min}min",
        )

        day = _day_str()
        now = time.monotonic()
        next_ping = now
        next_speedtest = now + 15  # first speed test shortly after start (baseline)
        next_ipcheck = now + 10
        next_status = now + 5
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_ping:
                    self._probe_round()
                    next_ping = now + cfg.ping_interval_sec
                if now >= next_speedtest and not self.speedtest_running:
                    threading.Thread(target=self._speedtest_worker, daemon=True).start()
                    next_speedtest = now + cfg.speedtest_interval_min * 60
                if now >= next_ipcheck:
                    self._ip_check()
                    next_ipcheck = now + cfg.ip_check_interval_min * 60
                if now >= next_status:
                    self._write_status()
                    next_status = now + 30
                day = self._rollover_if_needed(day)
                self.stop_event.wait(0.5)
        finally:
            self._shutdown(day)

    def _shutdown(self, day: str) -> None:
        self.event("info", "monitor stopped")
        self.ping_csv.flush()
        self.speed_csv.flush()
        self.event_csv.flush()
        try:
            generate_report(self.cfg, date=day)
            log.info("final report generated for %s", day)
        except Exception as exc:
            log.warning("final report failed: %s", exc)
        self.ping_csv.close()
        self.speed_csv.close()
        self.event_csv.close()
        self._write_status()


# ------------------------------------------------------------ one-off modes

def run_once(cfg: Config) -> None:
    """Quick manual check: probe every target once, run one speed test."""
    print(f"nettest {__version__} — one-shot check")
    print("=" * 60)
    if cfg.auto_add_gateway:
        gw = detect_default_gateway()
        print(f"Default gateway (LAN side of router): {gw or 'not detected'}")
    print(f"Ping targets: {', '.join(cfg.effective_targets)}")
    print()
    for t in cfg.effective_targets:
        ok, rtt = probe_once(t, cfg.ping_timeout_sec)
        print(f"  {t:16} {'ok' if ok else 'TIMEOUT':<7} {f'{rtt:.2f} ms' if ok else '-'}")
    print()
    print("Running speed test (30-60 s)...")
    try:
        res = run_speedtest(cfg.speedtest_ookla_bin, cfg.speedtest_timeout_sec)
        print(f"  Engine      : {res['engine']}")
        print(f"  Server      : {res.get('server', '-')}")
        print(f"  ISP         : {res.get('isp', '-')}")
        print(f"  Download    : {res.get('download_mbps', 0):.1f} Mbps")
        print(f"  Upload      : {res.get('upload_mbps', 0):.1f} Mbps")
        print(f"  Ping        : {_fmt_ms(res.get('ping_ms'))}")
        print(f"  Jitter      : {_fmt_ms(res.get('jitter_ms'))}")
        if res.get("external_ip"):
            print(f"  External IP : {res['external_ip']}")
    except SpeedtestError as exc:
        print(f"  FAILED: {exc}")
    ip = _fetch_external_ip()
    print(f"External IP (HTTP check): {ip or 'unreachable'}")


def _fmt_ms(v) -> str:
    return f"{v:.1f} ms" if v is not None else "-"


def _normalize_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return s
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        raise SystemExit(f"invalid date '{s}' — use YYYY-MM-DD")


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="nettest",
        description="5G home-router network quality monitor (ping + speedtest, 7x24).",
    )
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument(
        "--once", action="store_true",
        help="run a single ping round + one speed test, print results, exit",
    )
    parser.add_argument(
        "--report", nargs="?", const="", default=None, metavar="YYYY-MM-DD",
        help="generate a report and exit. Without a date: all available days "
             "combined (use this after the 7-day trial).",
    )
    parser.add_argument("--version", action="version", version=f"nettest {__version__}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    cfg = Config.load(args.config)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if args.once:
        run_once(cfg)
        return 0
    if args.report is not None:
        date = _normalize_date(args.report)
        path = generate_report(cfg, date=date)
        print(f"Report written to {path}")
        return 0
    Monitor(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
