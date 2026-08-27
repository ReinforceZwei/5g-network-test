"""Configuration loading.

Configuration lives in a JSON file (default: config.json next to the package).
Relative log_dir / report_dir paths are resolved against the config file's
directory, so the config can be moved around with the repo.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .ping import detect_default_gateway

DEFAULTS: dict = {
    # Public ping targets. Add your own (ISP DNS, game servers, ...) as needed.
    "targets": ["1.1.1.1", "8.8.8.8", "223.5.5.5", "168.95.1.1"],
    # Also probe the default gateway (the 5G router LAN side) to separate
    # LAN problems from WAN problems.
    "auto_add_gateway": True,
    "ping_interval_sec": 5,
    "ping_timeout_sec": 2,
    # How often to run a full speed test (minutes).
    "speedtest_interval_min": 60,
    "speedtest_timeout_sec": 180,
    # Ookla CLI binary name/path. Falls back to `speedtest-cli` (pip) if absent.
    "speedtest_ookla_bin": "speedtest",
    # How often to check the public/external IP for changes (CGNAT rebinds).
    "ip_check_interval_min": 15,
    "log_dir": "logs",
    "report_dir": "reports",
    # Delete rotated CSV files older than this many days.
    "retention_days": 14,
    # Thresholds used by the report's assessment section.
    "thresholds": {
        "loss_pct": 1.0,
        "avg_ping_ms": 60.0,
        "min_download_mbps": 20.0,
        "min_upload_mbps": 5.0,
    },
}

_FIELDS = {
    "targets", "auto_add_gateway", "ping_interval_sec", "ping_timeout_sec",
    "speedtest_interval_min", "speedtest_timeout_sec", "speedtest_ookla_bin",
    "ip_check_interval_min", "log_dir", "report_dir", "retention_days",
    "thresholds",
}


@dataclass
class Config:
    root: Path
    targets: list = field(default_factory=list)
    auto_add_gateway: bool = True
    ping_interval_sec: float = 5
    ping_timeout_sec: float = 2
    speedtest_interval_min: int = 60
    speedtest_timeout_sec: int = 180
    speedtest_ookla_bin: str = "speedtest"
    ip_check_interval_min: int = 15
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    report_dir: Path = field(default_factory=lambda: Path("reports"))
    retention_days: int = 14
    thresholds: dict = field(default_factory=dict)
    _effective: list | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        raw: dict = {}
        if path.exists():
            raw = json.loads(path.read_text())
        merged = {**DEFAULTS, **raw}
        root = path.resolve().parent
        kwargs = {k: v for k, v in merged.items() if k in _FIELDS}
        cfg = cls(root=root, **kwargs)
        cfg.log_dir = _resolve(root, cfg.log_dir)
        cfg.report_dir = _resolve(root, cfg.report_dir)
        return cfg

    @property
    def effective_targets(self) -> list:
        """Configured targets + auto-detected default gateway (first, deduped)."""
        if self._effective is None:
            targets = list(self.targets)
            if self.auto_add_gateway:
                gw = detect_default_gateway()
                if gw and gw not in targets:
                    targets.insert(0, gw)
            self._effective = targets
        return self._effective


def _resolve(root: Path, value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (root / p).resolve()
