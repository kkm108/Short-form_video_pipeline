"""Runtime disk-space guardrail: free_gb is positive on this machine, ensure_disk
passes with room to spare and raises DiskLowError below the threshold, and the
engine's start() refuses to create a run (and so doesn't touch a step executor)
when the guard fires. The engine case patches ensure_disk directly so it's not
dependent on the machine actually being low on disk.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from orchestrator.disk_guard import DiskLowError, ensure_disk, free_gb
from orchestrator.engine import Pipeline
from orchestrator.state import StateStore


def test_free_gb_returns_positive_number():
    with tempfile.TemporaryDirectory() as tmp:
        free = free_gb(tmp)
        assert isinstance(free, float) and free > 0
        print("PASS test_free_gb_returns_positive_number")


def test_ensure_disk_passes_when_enough_room():
    with tempfile.TemporaryDirectory() as tmp, patch("shutil.disk_usage") as du:
        du.return_value = type("U", (), {"free": 50 * 1024 ** 3})()
        free = ensure_disk(Path(tmp) / "sub", min_free_gb=2.0)
        assert free == 50.0
        print("PASS test_ensure_disk_passes_when_enough_room")


def test_ensure_disk_raises_below_threshold():
    with tempfile.TemporaryDirectory() as tmp, patch("shutil.disk_usage") as du:
        du.return_value = type("U", (), {"free": 1 * 1024 ** 3})()
        try:
            ensure_disk(Path(tmp), min_free_gb=2.0)
            assert False, "expected DiskLowError"
        except DiskLowError as exc:
            assert "2" in str(exc)  # threshold surfaced in the message
            print("PASS test_ensure_disk_raises_below_threshold")


def test_engine_start_guards_before_creating_run():
    with tempfile.TemporaryDirectory() as tmp:
        state = StateStore(str(Path(tmp) / "state.db"))
        pipeline = Pipeline(state=state, executors={}, workdir=str(Path(tmp) / "runs"))
        # Simulate a low-disk environment deterministically, regardless of the
        # machine that runs the test.
        with patch("orchestrator.engine.ensure_disk", side_effect=DiskLowError("simulated low disk")), \
             patch.object(state, "create_run") as create_run:
            try:
                pipeline.start("manifests/dry_run.yaml", "anything")
                assert False, "expected DiskLowError from the engine pre-flight"
            except DiskLowError:
                pass
        # The guard fires before create_run - the run must never reach the store.
        create_run.assert_not_called()
        print("PASS test_engine_start_guards_before_creating_run")


if __name__ == "__main__":
    test_free_gb_returns_positive_number()
    test_ensure_disk_passes_when_enough_room()
    test_ensure_disk_raises_below_threshold()
    test_engine_start_guards_before_creating_run()
    print("\nall disk guard tests passed")
