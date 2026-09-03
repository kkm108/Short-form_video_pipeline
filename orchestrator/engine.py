"""The orchestrator: walks a manifest's steps in order, checkpointing after
each one, retrying per the manifest's policy, and refusing to re-run a step
that has already succeeded for this run_id (idempotency).

This is deliberately NOT an open-ended agent loop deciding what to do next -
step order comes entirely from the manifest. LLM calls and browser-automation
calls happen *inside* a step's executor; this layer never improvises.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from executors.base import AwaitingApproval, ExecutorError, ExecutorOutput, StepContext, StepExecutor
from orchestrator.disk_guard import ensure_disk
from orchestrator.manifest import Manifest, StepSpec, load_manifest
from orchestrator.models import RunState, StepResult, StepStatus
from orchestrator.state import StateStore

logger = logging.getLogger("pipeline.engine")


class Pipeline:
    def __init__(self, state: StateStore, executors: dict[str, StepExecutor], workdir: str = "./runs"):
        self.state = state
        self.executors = executors
        self.workdir = workdir

    # ---- starting a fresh run ----------------------------------------------

    def start(self, manifest_path: str, seed_topic: str) -> str:
        manifest = load_manifest(manifest_path)
        # Runtime disk guardrail: refuse a fresh run when there isn't room to
        # produce output (media + ffmpeg write large files). Raises DiskLowError
        # so an unattended scheduler fails loudly instead of quietly filling the
        # drive; run_scheduled.py pages on the non-zero exit.
        ensure_disk(self.workdir, name="run")
        run = RunState.new(seed_topic=seed_topic, platforms=manifest.platforms, manifest_path=manifest_path)
        self.state.create_run(run)
        logger.info("run %s created for topic %r", run.run_id, seed_topic)
        self._advance(manifest, run.run_id, seed_topic)
        return run.run_id

    # ---- resuming after a crash / approval / restart -----------------------

    def resume(self, run_id: str) -> None:
        run = self.state.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run_id {run_id!r}")
        manifest = load_manifest(run.manifest_path)
        ensure_disk(self.workdir, name="run")
        self._advance(manifest, run.run_id, run.seed_topic)
    # ---- core loop -----------------------------------------------------------

    def _advance(self, manifest: Manifest, run_id: str, seed_topic: str) -> None:
        upstream: dict[str, ExecutorOutput] = {}
        run_workdir = str(Path(self.workdir) / run_id)
        Path(run_workdir).mkdir(parents=True, exist_ok=True)

        for spec in manifest.steps:
            existing = self.state.get_step(run_id, spec.name)

            if existing and existing.status == StepStatus.SUCCEEDED:
                # idempotency: never re-run a step that already succeeded for this run_id
                upstream[spec.name] = ExecutorOutput(output_ref=existing.output_ref or "")
                continue

            if existing and existing.status == StepStatus.REJECTED:
                logger.info("run %s stopped: step %s was rejected", run_id, spec.name)
                return

            ok, output = self._run_step_with_retry(run_id, seed_topic, manifest.platforms, spec, upstream, run_workdir)

            if not ok:
                logger.error("run %s halted at step %s after exhausting retries", run_id, spec.name)
                return  # halted and recorded as FAILED - resume() can pick it back up, it won't silently continue

            if output is None:
                # AwaitingApproval was raised: park the run, a human resumes it later
                logger.info("run %s parked at step %s awaiting approval", run_id, spec.name)
                return

            upstream[spec.name] = output

        logger.info("run %s completed all steps", run_id)

    def _run_step_with_retry(
        self,
        run_id: str,
        seed_topic: str,
        platforms: list[str],
        spec: StepSpec,
        upstream: dict[str, ExecutorOutput],
        workdir: str,
    ) -> tuple[bool, Optional[ExecutorOutput]]:
        executor = self.executors.get(spec.executor)
        if executor is None:
            raise KeyError(f"no executor registered for {spec.executor!r} (step {spec.name!r})")

        context = StepContext(
            run_id=run_id,
            seed_topic=seed_topic,
            platforms=platforms,
            step_config=spec.config,
            upstream=upstream,
            workdir=workdir,
            step_name=spec.name,
        )

        attempt = 1
        while True:
            self.state.save_step_result(
                StepResult(run_id=run_id, step_name=spec.name, status=StepStatus.RUNNING, attempt=attempt)
            )
            try:
                output = executor.run(context)
                self.state.save_step_result(
                    StepResult(
                        run_id=run_id,
                        step_name=spec.name,
                        status=StepStatus.SUCCEEDED,
                        attempt=attempt,
                        output_ref=output.output_ref,
                        finished_at=time.time(),
                    )
                )
                return True, output

            except AwaitingApproval:
                self.state.save_step_result(
                    StepResult(run_id=run_id, step_name=spec.name, status=StepStatus.AWAITING_APPROVAL, attempt=attempt)
                )
                return True, None  # not a failure - just parked

            except ExecutorError as exc:
                matches_retry_on = not spec.retry.retry_on or exc.status_code in spec.retry.retry_on
                should_retry = exc.retryable and matches_retry_on and attempt < spec.retry.max_attempts
                logger.warning("run %s step %s attempt %d failed: %s", run_id, spec.name, attempt, exc)

                if not should_retry:
                    self.state.save_step_result(
                        StepResult(
                            run_id=run_id,
                            step_name=spec.name,
                            status=StepStatus.FAILED,
                            attempt=attempt,
                            error=str(exc),
                            finished_at=time.time(),
                        )
                    )
                    return False, None

                _sleep_backoff(spec.retry.backoff, spec.retry.base_delay_s, attempt)
                attempt += 1

            except Exception as exc:
                # An executor raised something other than ExecutorError/AwaitingApproval -
                # a forgotten try/except somewhere, a genuine bug, a dependency's own
                # exception type leaking through. Without this, that crashes the whole
                # process instead of failing one run. Treated as non-retryable by
                # default: an *unknown* failure mode shouldn't be blindly retried the
                # way a recognized, typed ExecutorError can be - better to halt and let
                # a human look at it, then resume() once it's understood.
                logger.exception("run %s step %s attempt %d raised unexpected %s", run_id, spec.name, attempt, type(exc).__name__)
                self.state.save_step_result(
                    StepResult(
                        run_id=run_id,
                        step_name=spec.name,
                        status=StepStatus.FAILED,
                        attempt=attempt,
                        error=f"unexpected {type(exc).__name__}: {exc}",
                        finished_at=time.time(),
                    )
                )
                return False, None


def _sleep_backoff(kind: str, base: float, attempt: int) -> None:
    if kind == "none":
        return
    delay = base * (2 ** (attempt - 1)) if kind == "exponential" else base
    time.sleep(delay)


def approve(state: StateStore, run_id: str, step_name: str) -> None:
    """Called by the human-review surface (Slack button / CLI) on approve.
    Marks the gate step succeeded so the next resume() walks past it."""
    state.save_step_result(
        StepResult(
            run_id=run_id, step_name=step_name, status=StepStatus.SUCCEEDED, output_ref="approved", finished_at=time.time()
        )
    )


def reject(state: StateStore, run_id: str, step_name: str, reason: str = "") -> None:
    state.save_step_result(
        StepResult(run_id=run_id, step_name=step_name, status=StepStatus.REJECTED, error=reason, finished_at=time.time())
    )
