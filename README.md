# 5G Network Quality Monitor

Unattended 7x24 network quality testing for a 5G home-wifi router trial.
Run it on a Raspberry Pi connected to the router **via Ethernet**, and you get
a complete, timestamped test report to decide — inside the free-cancellation
window — whether the connection is good enough to keep.

## What it does

- **Ping test** — every 5 s, one ICMP probe per target (default: the router's
  LAN gateway + 1.1.1.1 + 8.8.8.8 + 223.5.5.5 + 168.95.1.1). Logs every probe
  to `logs/ping_YYYYMMDD.csv` (rtt in ms, empty = timeout/loss).
- **Speed test** — every 60 min (configurable), using the Ookla speedtest CLI
  (fallback: `speedtest-cli`). Logs download/upload/ping/jitter/packet-loss/
  server/ISP/external IP to `logs/speedtest_YYYYMMDD.csv`.
  Runs in a background thread, so ping sampling never pauses.
- **External IP tracking** — checks the public IP every 15 min and logs every
  change (typical for 5G CGNAT / IP rebinding).
- **Discord notifications** — every daily report (plus a final report on
  shutdown) is pushed to a Discord webhook as a summary message with the full
  `.md` report and chart `.png` attached. Missing days are caught up
  automatically after restarts.
- **Reports** — markdown summary per day (auto-generated at midnight) and a
  combined report over all days (the 7-day verdict). Optional matplotlib chart
  (PNG) with RTT time series, speed history and 10-minute loss buckets.
- **Self-contained** — systemd service with auto-restart, log rotation with
  retention, and a live `status.json` for quick health checks.

## Layout

```
config.json          # all knobs (targets, intervals, thresholds)
nettest/             # the monitor package (ping, speedtest, storage, report)
scripts/install.sh   # one-shot Pi installer
systemd/             # service unit (installed with your user/paths)
logs/                # daily CSVs (created at runtime)
reports/             # report_YYYYMMDD.md / report_all-days.md / latest.*
status.json          # live status (created at runtime)
```

## Quick start (on the Pi)

```bash
git clone git@github.com:ReinforceZwei/5g-network-test.git
cd 5g-network-test
./scripts/install.sh            # + --charts for PNG charts, --tailscale for TS
```

Full walkthrough (SD card, first boot, Tailscale, reading reports,
troubleshooting): **[SETUP.md](SETUP.md)**.

## One-shot / manual checks

```bash
# Single probe round + one speed test (no files written)
.venv/bin/python -m nettest.main --once

# Regenerate the combined report (use after the 7-day trial)
.venv/bin/python -m nettest.main --report

# Report for one day
.venv/bin/python -m nettest.main --report 2026-09-03
```

## Config reference (`config.json`)

| key | default | meaning |
|---|---|---|
| `targets` | `["1.1.1.1","8.8.8.8","223.5.5.5","168.95.1.1"]` | ping targets (add your ISP DNS / game servers) |
| `auto_add_gateway` | `true` | also ping the router's LAN IP (separates LAN vs WAN issues) |
| `ping_interval_sec` | `5` | probe cadence per target |
| `ping_timeout_sec` | `2` | per-probe timeout |
| `speedtest_interval_min` | `60` | speed test cadence ⚠️ see data-usage note below |
| `speedtest_timeout_sec` | `180` | max duration of one speed test |
| `speedtest_ookla_bin` | `speedtest` | Ookla CLI binary name/path |
| `ip_check_interval_min` | `15` | public-IP change check cadence |
| `log_dir` / `report_dir` | `logs/` / `reports/` | relative to the config file |
| `retention_days` | `14` | CSV auto-delete age |
| `discord_webhook_url` | `""` | Discord webhook URL (empty = notifications off; put it in `config.local.json`, see below) |
| `notify_daily` / `notify_final` | `true` / `true` | send daily / final-shutdown reports to the webhook |
| `thresholds` | loss 1%, ping 60 ms, dl 20 Mbps, ul 5 Mbps | report verdict thresholds |

## Discord notifications

Set your webhook URL (keep it out of git):

```bash
cd ~/5g-network-test
echo '{"discord_webhook_url": "https://discord.com/api/webhooks/..."}' > config.local.json
sudo systemctl restart 5g-network-test
./.venv/bin/python -m nettest.main --test-webhook   # send a test message
```

`config.local.json` is gitignored, so `git pull` never conflicts and the URL
never lands in the repo. Alternative: export `DISCORD_WEBHOOK_URL` in the
service environment instead.

Each notification = one Discord message: a compact summary (verdict,
assessment vs thresholds, speed-test min/avg/max, coverage) plus the full
report `.md` and chart `.png` as attachments. Sent on: daily midnight report,
catch-up for days the Pi was off, and the final report on graceful shutdown.

## Reading the verdict

The report's **Assessment** section mechanically compares the aggregated
metrics against `thresholds` and prints PASS/FAIL per metric plus an overall
verdict (`ACCEPTABLE` / `NOT ACCEPTABLE`). Tune the thresholds to your own
needs — e.g. gamers should also watch p95/jitter even if the average looks fine.

## ⚠️ Data usage

One Ookla speed test transfers roughly **50–150 MB** (at 5G speeds). At the
default 60 min interval that's roughly **1.5–3 GB/day**. If your plan has a
data cap, raise `speedtest_interval_min` (e.g. 120–180) or lower it during
peak times you care about. Ping traffic is negligible.

## Requirements

- Raspberry Pi (any model with Ethernet; Pi 3/4/5 recommended), Raspberry Pi
  OS Lite 64-bit
- The Pi must be wired to the 5G router's LAN port via Ethernet — that is the
  point of the test (Wi-Fi would test the Pi's Wi-Fi, not the 5G plan)
- Internet access for the Ookla CLI download during install

## License

MIT
