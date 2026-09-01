"""Restore a backup onto a (fresh) checkout so resumability survives a move.

    python3 scripts/restore.py --archive backup_2026-01-01.tar.gz --workspace .

Unpacks every entry from the archive produced by scripts/backup.py into
`workspace` - the SQLite run-history DB, manifests, and providers configs. It
will NOT extract browser-session profiles or any path that could carry a
credential, and it refuses path-traversal entries by construction.

After a restore the manual steps that can't be automated (because the backup
correctly never contained them) are: re-run scripts/export_session.py to mint a
fresh browser session, and re-seed the vault secrets (OS keychain / env vars).
See README 'Backup & restore'.
"""
from __future__ import annotations

import argparse
import os
import shutil
import tarfile
from pathlib import Path
from typing import Optional

MANIFEST_NAME = "backup-manifest.json"
FORBIDDEN_PREFIXES = ("profiles",)  # live browser-session credentials - never restored from a backup


class UnsafeArchiveEntry(Exception):
    """Raised when an archive member would restore a credential or escape the workspace."""


def _safe_relpath(name: str) -> str:
    """Normalize an archive member name to a safe relative POSIX path, or raise.

    Strips exactly one leading ``./`` prefix (a common tar convention) without
    touching real leading dots (a legit ``.env`` stays ``.env``), and rejects
    traversal (``..``) and absolute paths. A blanket ``lstrip(\"./\")`` would
    both strip the dots off a real dotfile and silent ``..`` before the check -
    both wrong.
    """
    import posixpath

    raw = name.replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    normalized = posixpath.normpath(raw)
    if normalized in ("", ".", os.curdir, os.pardir):
        raise UnsafeArchiveEntry(f"refuses to extract bare path {name!r}")
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise UnsafeArchiveEntry(f"path escapes workspace: {name!r}")
    return normalized


def restore_archive(archive: str, workspace: str) -> list[str]:
    """Extract all safe members of `archive` into `workspace`.

    Returns the list of restored relative paths. Raises UnsafeArchiveEntry if
    any member is forbidden (a credential path or path traversal)."""
    ws = Path(workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)

    restored: list[str] = []
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            # Normalize FIRST, then apply every safety rule to the normalized
            # path. Checking a prefix against the raw member.name is how a
            # crafted `./profiles/...` used to slip past the guard.
            rel = _safe_relpath(member.name)
            if rel.startswith(FORBIDDEN_PREFIXES):
                raise UnsafeArchiveEntry(
                    f"archive contains forbidden credential path {rel!r}; refusing to restore"
                )
            if rel == MANIFEST_NAME:
                continue
            if member.issym() or member.islnk():
                raise UnsafeArchiveEntry(f"archive contains a link member {member.name!r}; refusing")
            target = (ws / rel).resolve()
            if not target.is_relative_to(ws):
                raise UnsafeArchiveEntry(f"path escapes workspace: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                restored.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                # extractfile only returns None for special members (dirs, links);
                # we already handled dirs above and rejected links earlier.
                raise UnsafeArchiveEntry(f"cannot read archive member {member.name!r}")
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.utime(target, (member.mtime, member.mtime))
            restored.append(rel)
    return restored


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="restore",
        description="Unpack a pipeline backup archive onto a fresh checkout.",
    )
    parser.add_argument("--archive", required=True, help="the archive produced by scripts/backup.py")
    parser.add_argument("--workspace", default=".", help="pipeline root to restore into (default: current dir)")
    args = parser.parse_args(argv)

    restored = restore_archive(args.archive, args.workspace)
    print(f"restored {len(restored)} entries into {args.workspace}")
    print("manual steps after restore (backup contains no credentials by design):")
    print("  1. scripts/export_session.py <url> <session>   # re-mint a live browser session into profiles/")
    print("  2. re-seed vault secrets (OS keychain or env vars) for each get_*_ref you use")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
