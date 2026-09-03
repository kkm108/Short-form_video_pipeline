"""Advisory run-lock tests, all cross-platform (work on Windows via msvcrt and
POSIX via fcntl). The key property: a second acquisition while the lock is held
raises LockHeld - which is what the CLI turns into a clean non-zero exit - and
the lock is reusable once released, exactly like a process that died mid-run
releasing it to the next starter.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from orchestrator.run_lock import LockHeld, RunLock


def test_acquire_and_release_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "run.lock"
        lock = RunLock(lock_path)
        lock.acquire()
        assert lock_path.exists(), "acquiring the lock must create the lock file"
        # release is idempotent
        lock.release()
        lock.release()
        # and after release, a fresh lock on the same path works again
        again = RunLock(lock_path)
        again.acquire()
        again.release()
        print("PASS test_acquire_and_release_round_trip")


def test_second_acquisition_while_held_raises():
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "run.lock"
        first = RunLock(lock_path)
        first.acquire()
        second = RunLock(lock_path)
        try:
            second.acquire()
            assert False, "expected LockHeld while the first lock is held"
        except LockHeld:
            pass
        first.release()
        # once released, the second can proceed (simulates the crashed runner
        # being gone)
        second.acquire()
        second.release()
        print("PASS test_second_acquisition_while_held_raises")


def test_context_manager_hooks():
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "run.lock"
        with RunLock(lock_path):
            # re-acquiring from a second handle in the same scope must fail
            try:
                RunLock(lock_path).acquire()
                assert False, "expected LockHeld inside the with-block"
            except LockHeld:
                pass
        # exiting the with-block released it
        RunLock(lock_path).acquire().release()
        print("PASS test_context_manager_hooks")


if __name__ == "__main__":
    test_acquire_and_release_round_trip()
    test_second_acquisition_while_held_raises()
    test_context_manager_hooks()
    print("\nall run lock tests passed")
