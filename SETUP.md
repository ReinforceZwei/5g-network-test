# Setup Guide — Raspberry Pi + 5G router quality monitor

This guide walks you through the whole setup: SD card → first boot → clone the
repo → install → Tailscale → reading the reports → the 7-day decision.

**The idea:** the Pi is wired to the 5G router via Ethernet. Everything the Pi
measures is therefore the quality of the 5G plan + router as seen by a wired
client — exactly what you want to judge.

---

## 0. What you need

- Raspberry Pi (Pi 3/4/5 recommended; any model with an Ethernet port)
- MicroSD card (8 GB+ is plenty) + reader
- Ethernet cable
- A phone/laptop on the same network to control the Pi during setup
- The 5G router must already be online (SIM inserted, WAN up)

## 1. Prepare the SD card

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Choose **Raspberry Pi OS Lite (64-bit)** (no desktop needed).
3. Click the gear icon ⚙️ **before writing** and set:
   - **Enable SSH** (allow public-key auth or password)
   - **Username** `pi`, a strong password
   - **Hostname** e.g. `5gpi`
   - (Optional) your Wi-Fi — only as a fallback; the Pi will use Ethernet
4. Write the card, insert it into the Pi.

## 2. First boot & connect

1. Plug the **Ethernet cable** from the Pi into a LAN port of the 5G router.
2. Plug in the Pi's power (use the official PSU — cheap adapters cause exactly
   the kind of flaky behaviour you're trying to measure).
3. Find the Pi's IP: check the router's admin page (DHCP client list) or run
   `ping 5gpi.local` / `arp -a` from your laptop.
4. SSH in:

   ```bash
   ssh pi@5gpi.local        # or ssh pi@<router-assigned-ip>
   ```

5. Update the base system:

   ```bash
   sudo apt update && sudo apt upgrade -y
   sudo reboot
   ```

## 3. Clone the repo (private repo → SSH key)

The repo is private, so the Pi needs credentials. Easiest: an SSH key.

```bash
ssh-keygen -t ed25519 -C "pi@5gpi" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Copy the printed key, then on your computer (where `gh` is logged in):

```bash
gh ssh-key add -t "5gpi" - < <(ssh pi@5gpi.local 'cat ~/.ssh/id_ed25519.pub')
```

Back on the Pi:

```bash
git clone git@github.com:ReinforceZwei/5g-network-test.git
cd 5g-network-test
```

> Alternative: `gh auth login` on the Pi and clone via HTTPS.

## 4. Install & start the monitor

```bash
cd ~/5g-network-test
./scripts/install.sh --charts --tailscale
```

- `--charts` installs matplotlib so reports include a PNG chart.
- `--tailscale` installs Tailscale (see next section).
- The script: installs system packages → Ookla speedtest CLI → Python venv →
  runs a **self-test** (one ping round + one real speed test) → installs and
  starts the `5g-network-test` systemd service.

Verify it's alive:

```bash
systemctl status 5g-network-test          # active (running)
journalctl -u 5g-network-test -f          # live log
cat status.json                           # per-target state, uptime
tail -f logs/ping_$(date +%Y%m%d).csv     # raw ping data
```

## 5. Tailscale (remote access + reports from anywhere)

Tailscale gives you an encrypted private network between your devices, so you
can SSH into the Pi from anywhere (phone included) — very handy when the 5G
router is the only thing at the studio and you're not there.

```bash
sudo tailscale up --ssh
```

- It prints a URL like `https://login.tailscale.com/a/xxxx` — open it on any
  device and log in with your account. The Pi joins your tailnet.
- `--ssh` enables **Tailscale SSH**: you can SSH into the Pi without
  configuring keys, from any of your devices:

  ```bash
  tailscale ssh pi@5gpi        # from a laptop with Tailscale installed
  ```

  or `ssh pi@5gpi` using the tailnet IP / MagicDNS name.

- From a phone: install the Tailscale app, sign in, then use a terminal app
  (Termius, Blink, ...) to `ssh pi@5gpi`.

**Useful during the trial:**

```bash
# From any of your devices — is the Pi's link up right now?
tailscale ping 5gpi

# Pull the current reports while away from home:
scp -r pi@5gpi:~/5g-network-test/reports .

# Quick health check without a shell:
cat ~/5g-network-test/status.json        # via tailscale ssh
```

> Note: Tailscale traffic itself flows through the 5G router, so if the 5G
> connection drops, the Pi becomes unreachable over Tailscale too — that's
> expected, and itself a (coarse) availability signal.

## 6. Reading the reports

Everything lands in `~/5g-network-test/`:

| path | content |
|---|---|
| `logs/ping_YYYYMMDD.csv` | every probe: `ts,target,status,rtt_ms` (empty rtt = loss) |
| `logs/speedtest_YYYYMMDD.csv` | every speed test with server/ISP/external IP |
| `logs/events_YYYYMMDD.csv` | startup/shutdown, speedtest failures, IP changes |
| `reports/report_YYYYMMDD.md` | auto-generated daily summary at midnight |
| `reports/report_all-days.md` | combined report — **the 7-day verdict** |
| `reports/latest.md` / `latest.png` | always the newest report/chart |
| `status.json` | live state (updated every 30 s) |

Generate the combined verdict report any time:

```bash
cd ~/5g-network-test
.venv/bin/python -m nettest.main --report
```

The report includes per-target loss%/min/avg/max/p95/p99/jitter, outage events,
a speed-test history table + min/avg/max summary, external-IP changes, and a
mechanical **Assessment vs thresholds** section with an overall verdict
(ACCEPTABLE / NOT ACCEPTABLE). `report_all-days.png` shows RTT time series,
download/upload over time, and 10-minute loss buckets.

### Discord notifications (optional)

Have the daily report delivered to a Discord channel automatically:

```bash
cd ~/5g-network-test
echo '{"discord_webhook_url": "https://discord.com/api/webhooks/..."}' > config.local.json
sudo systemctl restart 5g-network-test
.venv/bin/python -m nettest.main --test-webhook     # one test message
```

- `config.local.json` is **gitignored** — the webhook URL never enters the
  repo, and `git pull` won't clobber it. (Alternative: `DISCORD_WEBHOOK_URL`
  env var in the service unit.)
- What you get: one Discord message per event — **daily report** (at midnight),
  **catch-up** for days the Pi was off/restarted across midnight, and a
  **final report** when the service shuts down gracefully. Each message
  contains a compact summary (verdict + assessment + speed-test min/avg/max +
  coverage) with the full `.md` report and the chart `.png` attached.
- Toggle with `notify_daily` / `notify_final` in config; empty URL = off.

## 7. The 7-day decision checklist

Inside the free-cancellation window, check the combined report for:

1. **Packet loss** — any sustained loss beyond ~0.5–1% (look at the outage
   events table and the 10-min loss chart). Occasional single lost pings are
   normal on 5G; repeated multi-second outages are not.
2. **Ping stability** — the RTT chart: flat lines good, sawtooth/spikes bad.
   Compare p95 vs avg: if p95 is 3-4x the average, expect stutter in
   real-time apps. `avg_ping_ms` threshold defaults to 60 ms.
3. **Speed consistency** — min vs avg download in the speed-test summary.
   A plan that peaks at 300 Mbps but drops to 5 Mbps every few hours is worse
   than a steady 50 Mbps. Default floor: 20 Mbps down / 5 Mbps up.
4. **Speed tests at the times you care about** — evening peak matters more
   than 3 a.m. You can raise `speedtest_interval_min` to 30 during days you
   care about, then lower it again (edit `config.json`, then
   `sudo systemctl restart 5g-network-test`).
5. **IP stability** — multiple distinct external IPs = CGNAT churn. Not a
   defect by itself, but a problem if you need inbound access or stable
   geo-location (gaming/matchmaking).

If the verdict is `NOT ACCEPTABLE` or the charts show what you can't live
with, cancel within the window. If borderline, run a second 7-day window with
adjusted thresholds to get a clearer verdict.

## 8. Tuning

Edit `~/5g-network-test/config.json`, then:

```bash
sudo systemctl restart 5g-network-test
```

Common changes: add your ISP DNS or a game server to `targets`; change
`speedtest_interval_min` (see the data-usage note in README); adjust
`thresholds` to match your plan's advertised speeds (e.g. a 300 Mbps plan
deserves `min_download_mbps: 150`).

## 9. Troubleshooting

| symptom | fix |
|---|---|
| `ping: command not found` | `sudo apt install iputils-ping` (install.sh does this) |
| speedtest rows empty / "no speedtest engine found" | `ls -l /usr/local/bin/speedtest`; else `.venv/bin/pip install speedtest-cli` |
| service keeps restarting | `journalctl -u 5g-network-test -n 50` — look for the last error line |
| no `logs/` created | check the service user owns the repo dir: `sudo chown -R $USER:$USER ~/5g-network-test` |
| Pi unreachable but router works | check the Ethernet cable & the router's DHCP (Pi uses DHCP) |
| want to stop the monitor | `sudo systemctl disable --now 5g-network-test` |

## 10. Uninstall

```bash
sudo systemctl disable --now 5g-network-test
sudo rm /etc/systemd/system/5g-network-test.service && sudo systemctl daemon-reload
rm -rf ~/5g-network-test
# optionally: sudo apt remove tailscale && sudo rm /usr/local/bin/speedtest
```
