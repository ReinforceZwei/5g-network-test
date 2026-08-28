"""Speed test wrapper.

Preferred engine: Ookla speedtest CLI (https://www.speedtest.net/apps/cli)
    - accurate, gives jitter + packet loss + external IP.
Fallback engine: `speedtest-cli` (pip install speedtest-cli), pure Python.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class SpeedtestError(Exception):
    pass


def _find(binary: str) -> str | None:
    """Locate a binary: on PATH, or in the running interpreter's own prefix
    (covers venv installs where .venv/bin is not on PATH, e.g. systemd)."""
    found = shutil.which(binary)
    if found:
        return found
    candidate = Path(sys.prefix) / "bin" / binary
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def run_speedtest(ookla_bin: str = "speedtest", timeout: int = 180) -> dict:
    """Run one speed test and return a dict of results.

    Raises SpeedtestError if no engine is available or all engines fail.
    """
    ookla = shutil.which(ookla_bin)  # Ookla: PATH / absolute path only (avoid
    # picking up the speedtest-cli `speedtest` alias from the venv)
    if ookla:
        try:
            return _run_ookla(ookla, timeout)
        except SpeedtestError:
            pass  # fall through to the Python client
    cli = _find("speedtest-cli")
    if cli:
        return _run_cli(cli, timeout)
    raise SpeedtestError(
        "no speedtest engine found: install the Ookla CLI (see SETUP.md) "
        "or run: .venv/bin/pip install speedtest-cli"
    )


def _run_ookla(bin_path: str, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            [bin_path, "--format=json", "--accept-license", "--accept-gdpr"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SpeedtestError(f"ookla speedtest timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise SpeedtestError(
            f"ookla speedtest failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SpeedtestError(f"ookla output not JSON: {proc.stdout[:200]!r}") from exc
    server = data.get("server") or {}
    iface = data.get("interface") or {}
    ping = data.get("ping") or {}
    dl = data.get("download") or {}
    ul = data.get("upload") or {}
    return {
        "engine": "ookla",
        "server": f"{server.get('name', '')} ({server.get('location', '')})".strip(),
        "isp": data.get("isp", ""),
        "external_ip": iface.get("externalIp", ""),
        "ping_ms": ping.get("latency"),
        "jitter_ms": ping.get("jitter"),
        # Ookla CLI JSON reports bandwidth in BYTES per second (not bits) —
        # convert to Mbps with *8/1e6. (speedtest-cli reports bits/s, see _run_cli.)
        "download_mbps": (dl.get("bandwidth") or 0) * 8 / 1e6,
        "upload_mbps": (ul.get("bandwidth") or 0) * 8 / 1e6,
        "packet_loss_pct": data.get("packetLoss"),
    }


def _run_cli(bin_path: str, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            [bin_path, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SpeedtestError(f"speedtest-cli timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise SpeedtestError(
            f"speedtest-cli failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SpeedtestError("speedtest-cli output not JSON") from exc
    server = data.get("server") or {}
    client = data.get("client") or {}
    return {
        "engine": "speedtest-cli",
        "server": (
            f"{server.get('name', '')} "
            f"({server.get('location', '')}, {server.get('country', '')})"
        ).strip(),
        "isp": client.get("isp", ""),
        "external_ip": client.get("ip", ""),
        "ping_ms": data.get("ping"),
        "jitter_ms": None,
        "download_mbps": (data.get("download") or 0) / 1e6,
        "upload_mbps": (data.get("upload") or 0) / 1e6,
        "packet_loss_pct": None,
    }
