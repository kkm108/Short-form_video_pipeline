"""Scheduled-run wrapper with canary gating.

A cron / systemd timer / GitHub Actions schedule should call this *instead of*
`cli.py` directly. It runs `canary.check.run_canary()` first and only proceeds
to `cli.py start` if the canary passes. On a canary failure it exits non-zero
and logs why - it never silently skips the check, and never launches a real run
against a UI that has drifted (see canary/check.py for what the canary guards).

Invocation (what you'd put in your scheduler):
    python3 run_scheduled.py --manifest manifests/youtube_only.yaml \
        --topic "history's shortest war" \
        --session ./profiles/generation_tool.json \
        --target-url https://your-tool.example.com/generate

`run_scheduled()` is split out from `main()` so tests can call it directly with
a fake canary and a fake CLI and prove that a failing canary actually blocks the
run (see tests/test_scheduled_run.py).

Exit-code semantics: this wrapper returns the exit code of the `cli.py start`
subprocess it launches. Note that `cli.py start` returns 0 even when a run halts
at a failed step - a failed run is a recorded outcome, not a CLI invocation
bug. So the return code here distinguishes "canary blocked the run" (2) and
"CLI crashed / refused to start" (non-zero) from "run was started and recorded
an outcome" (0). A scheduler that wants to page on "the run itself failed" must
inspect run state afterwards (e.g. `cli.py status <run_id>`), not rely on this
process exit code alone.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from typing import Optional

from canary.check import run_canary

logger = logging.getLogger("pipeline.scheduled")


def run_scheduled(
    manifest: str,
    topic: str,
    session_profile: str,
    target_url: str,
    cli_cmd: str = "cli.py",
    interpreter: Optional[str] = None,
) -> int:
    """Run the canary, then - only if it passes - start a real run.

    Returns 0 on success, non-zero if the canary fails (run is blocked) or
    the underlying `cli.py start` exits non-zero. Never runs the real pipeline
    when the canary is red.
    """
    ok = run_canary(session_profile, target_url)
    if not ok:
        logger.error(
            "canary FAILED for %r; NOT starting run for topic %r - investigate the generation tool's UI before retrying",
            target_url,
            topic,
        )
        return 2

    logger.info("canary passed for %r; starting run for topic %r", target_url, topic)
    py = interpreter or sys.executable
    return subprocess.run([py, cli_cmd, "start", manifest, topic]).returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="run_scheduled", description="Canary-gated scheduled runner for the pipeline.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--session", required=True, dest="session_profile", help="browser-use session profile for the canary")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--cli", default="cli.py", help="path to the pipeline CLI (default cli.py)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_scheduled(args.manifest, args.topic, args.session_profile, args.target_url, args.cli)


if __name__ == "__main__":
    raise SystemExit(main())
