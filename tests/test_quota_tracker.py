"""Pure state tests for the per-day quota ledger: record tallies, remaining
goes non-negative, mark_exhausted persists, and the JSON round-trips (so a
scheduled process restart doesn't lose the day's budget).
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path

from orchestrator.quota_tracker import QuotaTracker


def test_record_accumulates_and_remaining_is_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        tracker = QuotaTracker(Path(tmp) / "ledger.json")
        tracker.record("youtube", cost=3)
        assert tracker.used_today("youtube") == 3
        tracker.record("youtube", cost=2)
        assert tracker.used_today("youtube") == 5
        assert tracker.remaining("youtube", daily_budget=10) == 5
        # remaining never goes below zero even if budget is smaller than used
        assert tracker.remaining("youtube", daily_budget=4) == 0
        print("PASS test_record_accumulates_and_remaining_is_bounded")


def test_isolation_across_platforms_and_days():
    with tempfile.TemporaryDirectory() as tmp:
        tracker = QuotaTracker(Path(tmp) / "ledger.json")
        d1 = dt.date(2026, 9, 1)
        d2 = dt.date(2026, 9, 2)
        tracker.record("youtube", cost=6, date=d1)
        assert tracker.used_today("youtube", date=d1) == 6
        assert tracker.used_today("youtube", date=d2) == 0, "a new day resets the budget"
        tracker.record("instagram", cost=1, date=d1)
        assert tracker.used_today("instagram", date=d1) == 1
        assert tracker.used_today("youtube", date=d1) == 6, "platforms do not share a budget"
        print("PASS test_isolation_across_platforms_and_days")


def test_mark_exhausted_and_persistence_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.json"
        tracker = QuotaTracker(path)
        tracker.record("youtube", cost=10)
        tracker.mark_exhausted("youtube")
        assert tracker.is_exhausted("youtube") is True

        # A fresh tracker reading the same file sees the state (restart = safe)
        reloaded = QuotaTracker(path)
        assert reloaded.used_today("youtube") == 10
        assert reloaded.is_exhausted("youtube") is True
        assert json.loads(path.read_text(encoding="utf-8"))["youtube"] is not None
        print("PASS test_mark_exhausted_and_persistence_round_trip")


def test_corrupt_ledger_degrades_to_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.json"
        path.write_text("{not valid json", encoding="utf-8")
        tracker = QuotaTracker(path)
        assert tracker.used_today("youtube") == 0
        assert tracker.remaining("youtube", daily_budget=5) == 5
        print("PASS test_corrupt_ledger_degrades_to_empty")


if __name__ == "__main__":
    test_record_accumulates_and_remaining_is_bounded()
    test_isolation_across_platforms_and_days()
    test_mark_exhausted_and_persistence_round_trip()
    test_corrupt_ledger_degrades_to_empty()
    print("\nall quota tracker tests passed")

