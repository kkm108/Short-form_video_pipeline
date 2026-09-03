"""Durable-disk-space guardrail for scheduled runs.

The boot probe checks disk at boot (>=5GB); this is the *runtime* partner: it
guards right before a run starts so a fresh run never begins when there isn't
enough room to produce output. Media generation (Gemini/OpenAI images +
voiceover) and ffmpeg assembly write large files - a handful of scheduled runs
can fill a small drive mid-run, which is exactly the silent-hard-to-notice
failure mode this pipeline is designed to avoid.

Stdlib only (shutil.disk_usage is cross-platform on Windows/Posix). The
threshold reads from the PIPELINE_MIN_FREE_GB env var so it's a scheduling
config value, not a secret and not hardcoded.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class DiskLowError(RuntimeError):
    """Raised when a path's disk doesn't have the minimum free space needed."""


def free_gb(path: str | Path) -> float:
    """Free space (GB) on the filesystem containing ``path``."""
    usage = shutil.disk_usage(str(path))
    return usage.free / (1024 ** 3)


def ensure_disk(path: str | Path, min_free_gb: float | None = None, name: str = "run") -> float:
    """Raise DiskLowError if free space at ``path`` is below the threshold.

    Returns the free GB on success. ``min_free_gb`` defaults to env
    PIPELINE_MIN_FREE_GB (default 2.0). ``name`` is only used to make the error
    message readable.
    """
    if min_free_gb is None:
        min_free_gb = float(os.environ.get("PIPELINE_MIN_FREE_GB", "2.0"))
    Path(path).mkdir(parents=True, exist_ok=True)
    free = free_gb(path)
    if free < min_free_gb:
        raise DiskLowError(
            f"{name} requires at least {min_free_gb:g}GB free but only {free:.2f}GB is available - "
            "skipping to avoid filling the disk"
        )
    return free
