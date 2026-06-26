"""Remembers which faxes have already been sorted.

ProviderFlow is scanned every hour during the day, but each fax must be sorted
only ONCE. This ledger persists the set of already-processed fax ids (under
%LOCALAPPDATA%) so re-scans skip faxes that are already in a patient folder.
Only successfully-sorted faxes are recorded, so a transient failure is retried
on the next hourly run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


class ProcessedLedger:
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.seen = data
        except Exception:
            self.seen = {}

    def is_seen(self, key: str) -> bool:
        return bool(key) and key in self.seen

    def mark(self, key: str, **meta) -> None:
        if not key:
            return
        self.seen[key] = {"when": datetime.now().isoformat(timespec="seconds"), **meta}

    def prune(self, days: int = 180) -> None:
        """Drop entries older than `days` to bound the file size."""
        cutoff = datetime.now() - timedelta(days=days)
        for key in list(self.seen.keys()):
            try:
                when = datetime.fromisoformat(self.seen[key].get("when", ""))
                if when < cutoff:
                    del self.seen[key]
            except Exception:
                continue

    def save(self) -> None:
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.seen, indent=0), encoding="utf-8")
        tmp.replace(self.path)
