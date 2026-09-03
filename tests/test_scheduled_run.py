"""Proves the canary gate actually blocks a scheduled run.

The wrapper (`run_scheduled.py`) must not just *call* the canary - a red canary
must prevent `cli.py start` from ever running. We prove that with a real fake
CLI that writes a marker file when invoked: after a failing canary the marker
must not exist, and the wrapper must exit non-zero.
"""
from __future__ import annotations

import os
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

from run_scheduled import run_scheduled

FAKE_CLI = textwrap.dedent(
    """\
    import json, os, sys
    with open(os.environ["FAKE_CLI_MARKER"], "w") as f:
        f.write(json.dumps(sys.argv[1:]))
    sys.exit(int(os.environ.get("FAKE_CLI_EXIT", "0")))
    """
)


def _write_fake_cli(path: Path) -> str:
    path.write_text(FAKE_CLI)
    return str(path)


def test_failing_canary_blocks_the_run_and_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "cli-ran.txt"
        cli_path = _write_fake_cli(Path(tmp) / "fake_cli.py")

        old = os.environ.get("FAKE_CLI_MARKER")
        os.environ["FAKE_CLI_MARKER"] = str(marker)
        try:
            with patch("run_scheduled.run_canary", return_value=False):
                code = run_scheduled(
                    manifest=Path(tmp) / "manifest.yaml",
                    topic="some topic",
                    session_profile="/tmp/session.json",
                    target_url="https://tool.example/generate",
                    cli_cmd=cli_path,
                    interpreter=__import__("sys").executable,
                )
        finally:
            if old is None:
                os.environ.pop("FAKE_CLI_MARKER", None)
            else:
                os.environ["FAKE_CLI_MARKER"] = old

        assert code == 2  # canary failure -> non-zero, distinct from a clean 0
        assert not marker.exists(), "cli start must NOT have been invoked when the canary failed"
    print("PASS test_failing_canary_blocks_the_run_and_exits_nonzero")


def test_passing_canary_proceeds_to_start_the_run():
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "cli-ran.txt"
        cli_path = _write_fake_cli(Path(tmp) / "fake_cli.py")
        manifest = str(Path(tmp) / "manifest.yaml")

        old = os.environ.get("FAKE_CLI_MARKER")
        os.environ["FAKE_CLI_MARKER"] = str(marker)
        try:
            with patch("run_scheduled.run_canary", return_value=True):
                code = run_scheduled(
                    manifest=manifest,
                    topic="some topic",
                    session_profile="/tmp/session.json",
                    target_url="https://tool.example/generate",
                    cli_cmd=cli_path,
                    interpreter=__import__("sys").executable,
                )
        finally:
            if old is None:
                os.environ.pop("FAKE_CLI_MARKER", None)
            else:
                os.environ["FAKE_CLI_MARKER"] = old

        assert code == 0
        assert marker.exists(), "cli start SHOULD have been invoked after a passing canary"
        import json

        assert json.loads(marker.read_text()) == ["start", manifest, "some topic"]
    print("PASS test_passing_canary_proceeds_to_start_the_run")


def test_failing_canary_sends_alert():
    """A red canary must both block the run AND dispatch an alert - otherwise
    the 3am silent-failure gap is only half-closed."""
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "cli-ran.txt"
        cli_path = _write_fake_cli(Path(tmp) / "fake_cli.py")
        os.environ["FAKE_CLI_MARKER"] = str(marker)
        try:
            with patch("run_scheduled.run_canary", return_value=False):
                with patch("run_scheduled.send_alert") as send:
                    code = run_scheduled(
                        manifest=str(Path(tmp) / "manifest.yaml"),
                        topic="some topic",
                        session_profile="/tmp/session.json",
                        target_url="https://tool.example/generate",
                        cli_cmd=cli_path,
                        interpreter=__import__("sys").executable,
                        alert_webhook="https://alerts.example/hook",
                    )
        finally:
            os.environ.pop("FAKE_CLI_MARKER", None)

        assert code == 2
        assert send.called, "a failing canary must send an alert"
        args, _ = send.call_args
        assert "canary FAILED" in args[0]
        assert args[1] == "https://alerts.example/hook"
    print("PASS test_failing_canary_sends_alert")


if __name__ == "__main__":
    test_failing_canary_blocks_the_run_and_exits_nonzero()
    test_passing_canary_proceeds_to_start_the_run()
    test_failing_canary_sends_alert()
    print("\nall scheduled run tests passed")
