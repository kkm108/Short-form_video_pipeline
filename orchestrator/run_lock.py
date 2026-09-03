"""Mutual-exclusion lock so two scheduled pipeline runs never execute at once.

A cron/scheduler could fire `cli.py start` twice in the same window (or a manual
trigger could collide with a scheduled one). Without a lock, two runs would
share `runs/`, the SQLite state DB, and write to the same seeded-topic workdirs,
which corrupts run state and double-spends platform quota.

This is a cross-platform advisory lock on a small lock file (the classic
"lockfile" pattern), using the OS's own byte-range / flock locking:

  * Windows: `msvcrt.locking(LK_NBLCK)` on one byte of the file.
  * POSIX:   `fcntl.flock(LOCK_EX | LOCK_NB)`.

Both are *advisory* (cooperating processes only) and, crucially, the OS releases
them automatically when the holding process exits - crash or otherwise. So we
don't need to clear a stale lock file by hand; if the process dies, the next run
just locks and proceeds. `LockHeld` (not `TimeoutError`) is raised when another
run is currently active, and the caller turns that into a clean non-zero exit.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class LockHeld(RuntimeError):
    """Raised when another pipeline run is currently holding the lock."""


class RunLock:
    """A non-blocking advisory file lock, held while the `with` body runs."""

    def __init__(self, lock_path: str | Path, name: str = "pipeline"):
        self._path = Path(lock_path)
        self._name = name
        self._fd: Optional[int] = None

    def acquire(self) -> "RunLock":
        """Create the lock file and take the advisory lock. Raises LockHeld if
        some other process already holds it. Idempotent per instance."""
        if self._fd is not None:
            return self  # already held by this instance
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            self._try_lock(fd)
        except LockHeld:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        """Release the advisory lock (and drop the handle). Safe to call again."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # -- platform-specific advisory lock ------------------------------------

    @staticmethod
    def _try_lock(fd: int) -> None:
        try:
            import fcntl  # POSIX
        except ImportError:  # pragma: no cover - Windows path
            try:
                import msvcrt
            except ImportError:  # pragma: no cover - neither platform (impossible here)
                raise LockHeld(f"{os.name}: no advisory-lock API (fcntl/msvcrt) available")
            # msvcrt.locking locks `length` bytes from the current file position
            # and requires the file to actually have them; vest one byte.
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise LockHeld("another pipeline run is already in progress (lock held)")
            return
        # fcntl is only stubbed on POSIX; on Windows mypy can't resolve flock.
        # Ignored, not blanket - guarded by the ImportError above at runtime.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError:
            raise LockHeld("another pipeline run is already in progress (lock held)")
