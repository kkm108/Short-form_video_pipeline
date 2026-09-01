"""Backup/restore round-trip + credential-exclusion regression tests.

A backup must contain everything needed to resume a run on a fresh checkout
(the SQLite run-history DB, manifests, providers configs) and must *never*
contain a live browser session (`profiles/*.json`) or any raw secret value.
We exercise the whole loop for real: seed state, back up, wipe the working
directory, restore, and assert `cli.py status <run>` still reports the run --
so a failure here means "you cannot actually recover after data loss", not
just "the manifest file looks right".
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from orchestrator.models import RunState, StepResult, StepStatus
from orchestrator.state import StateStore
from scripts.backup import make_archive
from scripts.restore import UnsafeArchiveEntry, _safe_relpath, restore_archive

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "cli.py"
RUN_ID = "run_backup_0001"
SECRET = "live-session-secret-do-not-ship-xyz789"


def _seed_workspace(ws: Path) -> None:
    (ws / "manifests").mkdir(parents=True)
    (ws / "manifests" / "dry_run.yaml").write_text("platforms: [youtube]\nsteps: []\n", encoding="utf-8")
    (ws / "providers").mkdir(parents=True)
    (ws / "providers" / "000-gemini.json").write_text('{"type": "api", "provider": "gemini"}', encoding="utf-8")
    state = StateStore(str(ws / "pipeline_state.db"))
    state.create_run(
        RunState(
            run_id=RUN_ID,
            seed_topic="history's shortest war",
            platforms=["youtube"],
            manifest_path="manifests/dry_run.yaml",
            created_at=123.0,
        )
    )
    state.save_step_result(
        StepResult(
            run_id=RUN_ID,
            step_name="script",
            status=StepStatus.FAILED,
            attempt=2,
            error="LLM chain exhausted",
            started_at=10.0,
            finished_at=11.0,
        )
    )
    (ws / "profiles").mkdir(parents=True)
    (ws / "profiles" / "gen.json").write_text("stored_cookies=" + SECRET, encoding="utf-8")


def test_backup_wipe_restore_preserves_run_status():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        _seed_workspace(ws)
        archive = str(Path(tmp) / "backup.tar.gz")

        manifest = make_archive(str(ws), archive)
        assert any(e["path"] == "profiles" for e in manifest["excluded"]), "manifest must explain why profiles is excluded"

        # Simulate total data loss on this box.
        shutil.rmtree(str(ws))
        ws.mkdir()
        assert not (ws / "pipeline_state.db").exists()

        restored = restore_archive(archive, str(ws))
        assert "pipeline_state.db" in restored
        assert (ws / "pipeline_state.db").exists()
        assert (ws / "manifests" / "dry_run.yaml").exists()
        assert not (ws / "profiles").exists(), "restore must not recreate a credentials directory"

        # cli.py status must work against the restored state with no setup.
        out = subprocess.run(
            [sys.executable, str(CLI), "status", RUN_ID],
            cwd=str(ws),
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr
        assert "script" in out.stdout and "failed" in out.stdout, out.stdout
    print("PASS test_backup_wipe_restore_preserves_run_status")


def test_archive_never_contains_profiles_or_secret_values():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        _seed_workspace(ws)
        archive = str(Path(tmp) / "backup.tar.gz")

        manifest = make_archive(str(ws), archive)
        assert manifest["excluded"], "expected an excluded list in the manifest"

        with tarfile.open(archive, "r:*") as tar:
            members = list(tar)
            names = [m.name for m in members]
            assert not any(n.startswith("profiles") for n in names), f"profiles leaked into archive: {names}"
            # check decompressed content of every member, not raw gzip bytes
            # (compression would make a raw-substring check vacuously pass)
            for m in members:
                if m.isdir():
                    continue
                content = tar.extractfile(m).read()
                assert SECRET.encode() not in content, f"secret leaked into archive member {m.name!r}"
                assert b"stored_cookies=" not in content

        manifest_inside = tarfile.open(archive).extractfile("backup-manifest.json").read()
        assert SECRET.encode() not in manifest_inside
        assert json.loads(manifest_inside)["excluded"]
    print("PASS test_archive_never_contains_profiles_or_secret_values")


def _write_tar_with(members: dict, path: str) -> None:
    """Write a crafted single-file tar: name -> bytes."""
    with tarfile.open(path, "w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))


def test_restore_refuses_profiles_even_with_leading_dot_prefix():
    # A crafted archive can name its members as ./profiles/... to dodge a
    # check on the raw name - the guard must apply to the NORMALIZED path.
    with tempfile.TemporaryDirectory() as tmp:
        for bad in ("./profiles/evil.json", "profiles/evil.json"):
            archive = str(Path(tmp) / "bad.tar")
            _write_tar_with({bad: b"stored_cookie=live-session\n"}, archive)
            ws = Path(tmp) / "ws"
            try:
                restore_archive(archive, str(ws))
                assert False, f"expected UnsafeArchiveEntry for {bad!r}"
            except UnsafeArchiveEntry:
                pass
            assert not (ws / "profiles").exists()
    print("PASS test_restore_refuses_profiles_even_with_leading_dot_prefix")


def test_restore_refuses_path_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        for bad in ("../escape.txt", "./../../x", "/etc/passwd"):
            archive = str(Path(tmp) / "bad.tar")
            _write_tar_with({bad: b"boom"}, archive)
            ws = Path(tmp) / "ws"
            try:
                restore_archive(archive, str(ws))
                assert False, f"expected UnsafeArchiveEntry for {bad!r}"
            except UnsafeArchiveEntry:
                pass
            assert len(list(ws.rglob("escape.txt"))) == 0
    print("PASS test_restore_refuses_path_traversal")


def test_safe_relpath_preserves_dotfiles_and_strips_one_prefix():
    assert _safe_relpath("manifests/a.yaml") == "manifests/a.yaml"
    assert _safe_relpath("./manifests/a.yaml") == "manifests/a.yaml"
    assert _safe_relpath(".env") == ".env"
    assert _safe_relpath("./.env") == ".env"
    for bad in ("../x", "./../../x", "/abs"):
        try:
            _safe_relpath(bad)
            assert False, f"expected UnsafeArchiveEntry for {bad!r}"
        except UnsafeArchiveEntry:
            pass
    print("PASS test_safe_relpath_preserves_dotfiles_and_strips_one_prefix")


if __name__ == "__main__":
    test_backup_wipe_restore_preserves_run_status()
    test_archive_never_contains_profiles_or_secret_values()
    test_restore_refuses_profiles_even_with_leading_dot_prefix()
    test_restore_refuses_path_traversal()
    test_safe_relpath_preserves_dotfiles_and_strips_one_prefix()
    print("\nall backup/restore tests passed")
