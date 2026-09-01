"""Back up everything needed to resume operation on another machine (or after
data loss) - without ever capturing live credentials.

What goes IN (safe, secret-free by design):
    - pipeline_state.db        run history / resumability checkpoints
    - manifests/*.yaml         run configs
    - providers/*.json         LLM fallback-chain config (referenced secrets only)

What stays OUT (must never appear inside a produced archive):
    - profiles/*.json          live, logged-in browser sessions - these ARE
                               credentials; regenerate via scripts/export_session.py
    - runs/                    generated media - large and regenerable
    - __pycache__/, .git       regenerable by Python / not part of resume state

The archive always contains a top-level `backup-manifest.json` listing what was
included and what was excluded (and why), so you (or a future agent) can verify
a backup at a glance without trusting the format.

    python scripts/backup.py --workspace . --out backup_2026-01-01.tar.gz
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Optional

# (path relative to workspace root, human reason it's included)
INCLUDED: list[tuple[str, str]] = [
    ("pipeline_state.db", "SQLite run history/checkpoints - the resumability store"),
    ("manifests", "run configs (secret-free)"),
    ("providers", "LLM fallback-chain config (references secrets by name, never embeds them)"),
]

# (path relative to workspace root, human reason it's excluded)
EXCLUDED: list[tuple[str, str]] = [
    (
        "profiles",
        "live, logged-in browser sessions - these ARE credentials; never ship in a backup, regenerate via scripts/export_session.py",
    ),
    ("runs", "generated media - large and regenerable, not required to resume a run"),
    ("downloads", "downloaded media - regenerable"),
    ("__pycache__", "Python bytecode cache - regenerable"),
    (".git", "repository history - not part of a run's resume state"),
]

MANIFEST_NAME = "backup-manifest.json"


def _reason_for(path: str, table: list[tuple[str, str]]) -> Optional[str]:
    for root, reason in table:
        if path == root or path.startswith(root + "/"):
            return reason
    return None


def collect_files(workspace: str) -> list[str]:
    """Return the relative (POSIX) paths of files under `workspace` that belong
    in a backup - that is, files under an INCLUDED root and not under any
    EXCLUDED root. Only used to build the manifest's 'included' list."""
    root = Path(workspace)
    if not root.is_dir():
        return []
    included = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(root).as_posix()
        if _reason_for(rel, EXCLUDED) is not None:
            continue
        if _reason_for(rel, INCLUDED) is not None:
            included.append(rel)
    return included


def build_manifest(workspace: str) -> dict:
    included = collect_files(workspace)
    return {
        "generated_at": time.time(),
        "workspace": str(Path(workspace)),
        "included": [
            {"path": p, "why": _reason_for(p, INCLUDED)}
            for p in included
        ],
        "excluded": [
            {"path": root, "why": reason}
            for root, reason in EXCLUDED
        ],
        "note": (
            "Excluded paths are never packaged. profiles/*.json are live "
            "browser-session credentials (regenerate via scripts/export_session.py) "
            "and raw secret values stay in the OS keychain / env vars - both must "
            "be re-set up by hand after a restore (see README 'Backup & restore')."
        ),
    }


def make_archive(workspace: str, out_path: str) -> dict:
    """Write a portable gzipped tar of the safe files (plus its own manifest)
    to `out_path` and return the manifest dict. Raises FileNotFoundError if
    nothing safe exists to back up."""
    included = collect_files(workspace)
    if not included:
        raise FileNotFoundError(f"nothing to back up under {workspace!r} (no included files found)")
    manifest = build_manifest(workspace)

    tmp = out_path + ".tmp"
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
            for rel in included:
                tar.add(Path(workspace) / rel, arcname=rel)
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return manifest


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backup",
        description="Create a portable, credential-free backup archive of pipeline resume state.",
    )
    parser.add_argument("--workspace", default=".", help="pipeline root to back up (default: current dir)")
    parser.add_argument("--out", required=True, help="output archive path, e.g. backup_2026-01-01.tar.gz")
    args = parser.parse_args(argv)

    manifest = make_archive(args.workspace, args.out)
    print(f"backed up {len(manifest['included'])} files -> {args.out}")
    for entry in manifest["excluded"]:
        print(f"  excluded {entry['path']}: {entry['why']}")
    print("verification: run tests/test_backup_restore.py to assert the archive contains no credentials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
