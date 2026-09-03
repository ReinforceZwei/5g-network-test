"""Append-only CSV storage with per-day rollover, buffered flush and cleanup."""
from __future__ import annotations

import csv
import threading
from datetime import datetime, timedelta
from pathlib import Path


class DailyCSV:
    """Writes rows to logs/<prefix>_YYYYMMDD.csv, rolling over at midnight.

    Rows are buffered and flushed every `flush_every` rows (or via flush()),
    which keeps writes cheap on an SD card while staying durable.
    Thread-safe: speedtest rows arrive from a background thread.
    """

    def __init__(self, directory: Path, prefix: str, fields: list[str], flush_every: int = 64):
        self.directory = Path(directory)
        self.prefix = prefix
        self.fields = fields
        self.flush_every = flush_every
        self._lock = threading.Lock()
        self._fh = None
        self._day: str | None = None
        self._pending: list[list[str]] = []
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cell(v) -> str:
        """Serialize one value: None/missing become '' (never the string 'None')."""
        return "" if v is None else str(v)

    def _ensure_day(self, day: str) -> None:
        if self._day == day:
            return
        self._close_fh()
        path = self.directory / f"{self.prefix}_{day}.csv"
        is_new = not path.exists()
        self._fh = open(path, "a", newline="")
        if is_new:
            csv.writer(self._fh, lineterminator="\n").writerow(self.fields)
        self._day = day

    def write(self, day: str, rows: list[dict] | dict) -> None:
        if isinstance(rows, dict):
            rows = [rows]
        with self._lock:
            self._ensure_day(day)
            for row in rows:
                self._pending.append([self._cell(row.get(f)) for f in self.fields])
            if len(self._pending) >= self.flush_every:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._pending and self._fh:
            # csv.writer quotes fields containing commas/etc. — a raw
            # ",".join() would corrupt rows whose fields contain commas
            # (e.g. speedtest-cli server names like "Cebu City (, PH)").
            writer = csv.writer(self._fh, lineterminator="\n")
            writer.writerows(self._pending)
            self._fh.flush()
            self._pending.clear()

    def _close_fh(self) -> None:
        if self._fh is not None:
            self._flush_locked()
            self._fh.close()
            self._fh = None
            self._day = None

    def close(self) -> None:
        with self._lock:
            self._close_fh()


def cleanup_old(directory: Path, prefix: str, retention_days: int) -> None:
    """Delete rotated files older than retention_days (the current day survives)."""
    if retention_days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    for path in Path(directory).glob(f"{prefix}_*.csv"):
        try:
            day = datetime.strptime(path.stem.split("_")[-1], "%Y%m%d")
        except ValueError:
            continue
        if day < cutoff:
            path.unlink(missing_ok=True)
