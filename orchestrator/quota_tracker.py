"""Per-platform, per-day quota tracking so scheduled runs don't blindly keep
hitting a wall (YouTube's daily upload quota is the sharpest example - roughly
100 units/day, ~6 units per upload here, so the budget is real).

The ledger is a small JSON file under runs/ (gitignored along with the rest of
the run state). It is deliberately dumb and crash-safe-ish: a write is an atomic
temp-then-rename so a partial write never corrupts the budget.

The retry/backoff side of this is already handled by the engine (a 429 or 5xx
raises ExecutorError with retryable=True and the manifest's retry_on/backoff
policy controls the pacing). What this module adds is *keeping score*: before an
expensive quota-bearing call, check the day's remaining budget; after it,
record the cost. And a quotaExceeded response marks the day exhausted so the
next scheduled trigger backs off instead of spending the whole budget on
retries that will never succeed.

Nothing here touches the network or the vault.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Optional


class QuotaTracker:
    def __init__(self, ledger_path: str | Path):
        self._path = Path(ledger_path)

    # ---- reads -----------------------------------------------------------

    def used_today(self, platform: str, date: Optional[_dt.date] = None) -> int:
        key = (date or _dt.date.today()).isoformat()
        return int(self._load().get(platform, {}).get(key, 0))

    def remaining(self, platform: str, daily_budget: int, date: Optional[_dt.date] = None) -> int:
        return max(0, daily_budget - self.used_today(platform, date))

    def is_exhausted(self, platform: str, date: Optional[_dt.date] = None) -> bool:
        # A platform is "exhausted" if its last marked state for the day was the
        # quotaExceeded flag. We store that as a sentinel cost key.
        key = (date or _dt.date.today()).isoformat()
        return bool(self._load().get(platform, {}).get(f"{key}:exhausted", False))

    # ---- writes ----------------------------------------------------------

    def record(self, platform: str, cost: int = 1, date: Optional[_dt.date] = None) -> int:
        key = (date or _dt.date.today()).isoformat()
        data = self._load()
        platform_data = data.setdefault(platform, {})
        platform_data[key] = platform_data.get(key, 0) + cost
        self._write(data)
        return int(platform_data[key])

    def mark_exhausted(self, platform: str, date: Optional[_dt.date] = None) -> None:
        key = (date or _dt.date.today()).isoformat()
        data = self._load()
        platform_data = data.setdefault(platform, {})
        platform_data[f"{key}:exhausted"] = True
        self._write(data)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)  # atomic on the same filesystem
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# Default daily budget the caller should set in the vault/env if it tracks
# YouTube uploads. YouTube's per-project quota varies by billing model - the
# exact cost-per-upload is the caller's concern; this constant is just a sane
# default budget to check against, override with QUOTA_YOUTUBE_BUDGET.
YOUTUBE_DEFAULT_BUDGET = 96
