"""Proves the orchestrator's three load-bearing properties without touching
ffmpeg, browser-use, or any real API:

  1. retry+backoff  - a flaky executor eventually succeeds within its budget,
                       and halts (not silently continues) once exhausted
  2. idempotency    - a step that already succeeded is never re-run on resume
  3. approval gate  - the run parks and does NOT proceed until approve() is called
"""
from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

from executors.base import AwaitingApproval, Executor, ExecutorError, ExecutorOutput, StepContext
from orchestrator.engine import Pipeline, approve
from orchestrator.state import StateStore


class CountingExecutor(Executor):
    """Fails `fail_times` times, then succeeds. Used to test retry."""

    name = "counting"

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    def run(self, context: StepContext) -> ExecutorOutput:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ExecutorError("simulated failure")
        return ExecutorOutput(output_ref=f"ok-{self.calls}")


class GateExecutor(Executor):
    name = "gate"

    def run(self, context: StepContext) -> ExecutorOutput:
        raise AwaitingApproval()


def _write_manifest(tmpdir: str, retry_max: int) -> str:
    path = Path(tmpdir) / "manifest.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            run:
              platforms: [youtube]
            steps:
              - name: flaky_step
                executor: counting
                retry: {{max_attempts: {retry_max}, backoff: none}}
              - name: review_gate
                executor: gate
            """
        )
    )
    return str(path)


def test_retry_then_success():
    with tempfile.TemporaryDirectory() as tmp:
        counting = CountingExecutor(fail_times=2)
        pipeline = Pipeline(
            state=StateStore(str(Path(tmp) / "state.db")),
            executors={"counting": counting, "gate": GateExecutor()},
            workdir=tmp,
        )
        run_id = pipeline.start(_write_manifest(tmp, retry_max=5), "test topic")
        run = pipeline.state.get_run(run_id)
        assert run.steps["flaky_step"].status.value == "succeeded"
        assert counting.calls == 3  # 2 failures + 1 success
        print("PASS test_retry_then_success")


def test_retry_exhausted_halts_run():
    with tempfile.TemporaryDirectory() as tmp:
        counting = CountingExecutor(fail_times=10)
        pipeline = Pipeline(
            state=StateStore(str(Path(tmp) / "state.db")),
            executors={"counting": counting, "gate": GateExecutor()},
            workdir=tmp,
        )
        run_id = pipeline.start(_write_manifest(tmp, retry_max=3), "test topic")
        run = pipeline.state.get_run(run_id)
        assert run.steps["flaky_step"].status.value == "failed"
        assert counting.calls == 3
        assert "review_gate" not in run.steps  # never reached - halted, not skipped
        print("PASS test_retry_exhausted_halts_run")


def test_idempotent_resume_does_not_rerun_succeeded_step():
    with tempfile.TemporaryDirectory() as tmp:
        counting = CountingExecutor(fail_times=0)
        state = StateStore(str(Path(tmp) / "state.db"))
        pipeline = Pipeline(state=state, executors={"counting": counting, "gate": GateExecutor()}, workdir=tmp)

        run_id = pipeline.start(_write_manifest(tmp, retry_max=1), "test topic")
        assert counting.calls == 1
        run = state.get_run(run_id)
        assert run.steps["flaky_step"].status.value == "succeeded"
        assert run.steps["review_gate"].status.value == "awaiting_approval"

        # simulate a crash + restart: resume() must NOT call the already-succeeded step again
        pipeline.resume(run_id)
        assert counting.calls == 1  # unchanged
        print("PASS test_idempotent_resume_does_not_rerun_succeeded_step")


class BuggyExecutor(Executor):
    """Simulates an executor author forgetting to wrap a dependency's own
    exception type (a raw KeyError, here) as ExecutorError."""

    name = "buggy"

    def run(self, context: StepContext) -> ExecutorOutput:
        raise KeyError("some_unwrapped_dependency_error")


def test_unexpected_exception_fails_run_instead_of_crashing_process():
    with tempfile.TemporaryDirectory() as tmp:
        state = StateStore(str(Path(tmp) / "state.db"))
        pipeline = Pipeline(state=state, executors={"buggy": BuggyExecutor()}, workdir=tmp)
        path = Path(tmp) / "manifest.yaml"
        path.write_text(
            textwrap.dedent(
                """
                run:
                  platforms: [youtube]
                steps:
                  - name: broken_step
                    executor: buggy
                """
            )
        )

        run_id = pipeline.start(str(path), "test topic")  # must NOT raise KeyError up to the caller
        run = state.get_run(run_id)
        assert run.steps["broken_step"].status.value == "failed"
        assert "KeyError" in run.steps["broken_step"].error
        print("PASS test_unexpected_exception_fails_run_instead_of_crashing_process")


def test_approval_gate_blocks_until_approved():
    with tempfile.TemporaryDirectory() as tmp:
        state = StateStore(str(Path(tmp) / "state.db"))
        pipeline = Pipeline(state=state, executors={"counting": CountingExecutor(), "gate": GateExecutor()}, workdir=tmp)

        run_id = pipeline.start(_write_manifest(tmp, retry_max=1), "test topic")
        run = state.get_run(run_id)
        assert run.steps["review_gate"].status.value == "awaiting_approval"
        assert len(run.steps) == 2  # nothing after the gate has run

        approve(state, run_id, "review_gate")
        pipeline.resume(run_id)
        run = state.get_run(run_id)
        assert run.steps["review_gate"].status.value == "succeeded"
        print("PASS test_approval_gate_blocks_until_approved")


if __name__ == "__main__":
    test_retry_then_success()
    test_retry_exhausted_halts_run()
    test_idempotent_resume_does_not_rerun_succeeded_step()
    test_unexpected_exception_fails_run_instead_of_crashing_process()
    test_approval_gate_blocks_until_approved()
    print("\nall orchestrator tests passed")
