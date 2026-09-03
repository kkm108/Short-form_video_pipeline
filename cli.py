"""Command-line entry point.

    python3 cli.py start manifests/short_form.yaml "history's shortest war"
    python3 cli.py resume run_ab12cd34ef56
    python3 cli.py approve run_ab12cd34ef56 review_gate
    python3 cli.py reject  run_ab12cd34ef56 review_gate "voiceover pacing is off"
    python3 cli.py status  run_ab12cd34ef56

`approve`/`reject` are what a Slack slash-command or a tiny webhook handler
would call in a real deployment - this CLI form is the same code path,
useful for local testing before wiring up a chat surface.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from credentials.vault import credentials_provider
from executors.base import StepExecutor
from executors.browser_use_adapter import BrowserUseAdapter
from executors.ffmpeg_assembly import FfmpegAssemblyExecutor
from executors.gemini_media import GeminiMediaExecutor
from executors.human_checkpoint import HumanCheckpointExecutor
from executors.llm import LlmScriptExecutor
from executors.llm_chain import LlmChainExecutor
from executors.media_chain import MediaChainExecutor
from executors.publish_step import SinglePlatformPublishExecutor
from executors.publishers.instagram import InstagramPublisher
from executors.publishers.tiktok import TikTokPublisher
from executors.publishers.youtube import YouTubePublisher
from executors.stubs import StubMediaGenerationExecutor, StubPublishExecutor, StubScriptExecutor
from orchestrator.engine import Pipeline, approve, reject
from orchestrator.models import StepStatus
from orchestrator.quota_tracker import QuotaTracker, YOUTUBE_DEFAULT_BUDGET
from orchestrator.run_lock import LockHeld, RunLock
from orchestrator.state import StateStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_pipeline(db_path: str = "pipeline_state.db", workdir: str = "./runs") -> tuple[Pipeline, StateStore]:
    state = StateStore(db_path)
    quota_dir = Path(workdir)
    quota_dir.mkdir(parents=True, exist_ok=True)
    quota = QuotaTracker(quota_dir / "quota_ledger.json")
    youtube_budget = int(os.environ.get("QUOTA_YOUTUBE_BUDGET", str(YOUTUBE_DEFAULT_BUDGET)))
    youtube_quota_cost = int(os.environ.get("QUOTA_YOUTUBE_COST", "1"))
    executors: dict[str, StepExecutor] = {
        "llm": LlmScriptExecutor(),
        "llm_chain": LlmChainExecutor(),
        "browser_use": BrowserUseAdapter(),
        "gemini_media": GeminiMediaExecutor(),
        "media_chain": MediaChainExecutor(),
        "ffmpeg": FfmpegAssemblyExecutor(),
        "human_checkpoint": HumanCheckpointExecutor(),
        # Publisher construction doesn't touch credentials yet - Vault.get() is
        # only called inside publish(), so this works even before you've set
        # any platform's tokens. A manifest step for a platform you haven't
        # configured just fails clearly (CredentialNotFound) when it runs,
        # instead of the CLI refusing to start at all.
        "publish_youtube": SinglePlatformPublishExecutor(
            YouTubePublisher(
                credentials_provider,
                quota_tracker=quota,
                quota_cost=youtube_quota_cost,
                quota_budget=youtube_budget,
            )
        ),
        "publish_instagram": SinglePlatformPublishExecutor(InstagramPublisher(credentials_provider)),
        "publish_tiktok": SinglePlatformPublishExecutor(TikTokPublisher(credentials_provider)),
        # Zero-setup stand-ins - see manifests/dry_run.yaml.
        "llm_dryrun": StubScriptExecutor(),
        "media_generation_dryrun": StubMediaGenerationExecutor(),
        "publish_youtube_dryrun": StubPublishExecutor("youtube"),
        "publish_instagram_dryrun": StubPublishExecutor("instagram"),
        "publish_tiktok_dryrun": StubPublishExecutor("tiktok"),
    }
    return Pipeline(state, executors, workdir=workdir), state


def main() -> None:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("manifest")
    p_start.add_argument("seed_topic")

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("run_id")

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("run_id")
    p_approve.add_argument("step_name")

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("run_id")
    p_reject.add_argument("step_name")
    p_reject.add_argument("reason", nargs="?", default="")

    p_status = sub.add_parser("status")
    p_status.add_argument("run_id")

    sub.add_parser("list")

    args = parser.parse_args()
    pipeline, state = build_pipeline()

    if args.command in ("start", "resume", "approve"):
        # Mutual-exclusion so a second scheduled/manual trigger can't run the
        # pipeline concurrently (they'd share runs/, the state DB, and platform
        # quota). Advisory lock on runs/run.lock is auto-released by the OS if
        # this process dies mid-run, so there's no stale-lock problem.
        lock = RunLock(Path(pipeline.workdir) / "run.lock")
        try:
            with lock:
                _dispatch(pipeline, state, args)
        except LockHeld:
            print(f"another pipeline run is already in progress - refusing to start {args.command} for {args.run_id if hasattr(args, 'run_id') else args.seed_topic}", file=sys.stderr)
            sys.exit(3)
    else:
        _dispatch(pipeline, state, args)


def _dispatch(pipeline: Pipeline, state: StateStore, args) -> None:
    if args.command == "start":
        run_id = pipeline.start(args.manifest, args.seed_topic)
        _print_run_outcome(state, run_id, verb="started")
    elif args.command == "resume":
        pipeline.resume(args.run_id)
        _print_run_outcome(state, args.run_id, verb="resumed")
    elif args.command == "approve":
        approve(state, args.run_id, args.step_name)
        pipeline.resume(args.run_id)
        _print_run_outcome(state, args.run_id, verb="approved - resumed")
    elif args.command == "reject":
        reject(state, args.run_id, args.step_name, args.reason)
        print(f"rejected {args.run_id} at {args.step_name}")
    elif args.command == "status":
        run = state.get_run(args.run_id)
        if run is None:
            print("no such run", file=sys.stderr)
            sys.exit(1)
        for name, result in run.steps.items():
            err = f"  ({result.error[:80]})" if result.error else ""
            print(f"{name:20s} {result.status.value:18s} attempt={result.attempt}{err}")
    elif args.command == "list":
        for run_id in state.list_runs():
            print(run_id)


def _print_run_outcome(state: StateStore, run_id: str, verb: str) -> None:
    """`pipeline.start()`/`resume()` never raise just because a step failed -
    they halt the run and return normally, since a failed run is an expected,
    recorded outcome, not a bug in the CLI invocation. That means the caller
    has to go look at run state to know what actually happened; this is that
    look, so 'started X' doesn't print as if it succeeded when step one
    failed immediately after."""
    run = state.get_run(run_id)
    print(f"{verb} {run_id}")
    if not run or not run.steps:
        return
    last_step_name, last_result = list(run.steps.items())[-1]
    if last_result.status == StepStatus.SUCCEEDED:
        print(f"  -> completed through '{last_step_name}'")
    elif last_result.status == StepStatus.AWAITING_APPROVAL:
        print(f"  -> parked at '{last_step_name}', awaiting approval")
    elif last_result.status == StepStatus.FAILED:
        print(f"  -> halted at '{last_step_name}': {last_result.error}")
    elif last_result.status == StepStatus.REJECTED:
        print(f"  -> stopped: '{last_step_name}' was rejected ({last_result.error})")


if __name__ == "__main__":
    main()
