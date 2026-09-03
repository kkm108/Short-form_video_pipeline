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
        --target-url https://your-tool.example.com/generate \
        --alert-webhook https://hooks.slack.com/services/REPLACE/ME

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
from alerting.alerter import send_alert

logger = logging.getLogger("pipeline.scheduled")


def run_scheduled(
    manifest: str,
    topic: str,
    session_profile: str,
    target_url: str,
    cli_cmd: str = "cli.py",
    interpreter: Optional[str] = None,
    alert_webhook: Optional[str] = None,
) -> int:
    """Run the canary, then - only if it passes - start a real run.

    Returns 0 on success, non-zero if the canary fails (run is blocked) or
    the underlying `cli.py start` exits non-zero. Never runs the real pipeline
    when the canary is red.
    """
    ok = run_canary(session_profile, target_url)
    if not ok:
        msg = f"canary FAILED for {target_url!r}; scheduled run BLOCKED for topic {topic!r} - investigation needed"
        logger.error(msg)
        send_alert(msg, alert_webhook)
        return 2

    logger.info("canary passed for %r; starting run for topic %r", target_url, topic)
    py = interpreter or sys.executable
    try:
        code = subprocess.run([py, cli_cmd, "start", manifest, topic]).returncode
    except Exception as exc:
        msg = f"run_scheduled could not start cli.py for topic {topic!r}: {exc!r}"
        logger.exception(msg)
        send_alert(msg, alert_webhook)
        return 1

    if code != 0:
        # cli.py start returns non-zero on a crash/refusal. A *recorded* failure
        # (run halting at a step) returns 0, so a non-zero here means the process
        # itself could not run - alert someone.
        msg = f"run_scheduled: cli.py start exited non-zero ({code}) for manifest {manifest!r}, topic {topic!r}"
        logger.error(msg)
        send_alert(msg, alert_webhook)
    return code


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="run_scheduled", description="Canary-gated scheduled runner for the pipeline.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--session", required=True, dest="session_profile", help="browser-use session profile for the canary")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--cli", default="cli.py", help="path to the pipeline CLI (default cli.py)")
    parser.add_argument("--alert-webhook", default=None, help="Slack-compatible webhook URL for failure alerts (or set PIPELINE_ALERT_WEBHOOK)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_scheduled(args.manifest, args.topic, args.session_profile, args.target_url, args.cli, alert_webhook=args.alert_webhook)


if __name__ == "__main__":
    raise SystemExit(main())
