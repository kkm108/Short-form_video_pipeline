"""Covers orchestrator/state.py directly - separate from test_orchestrator.py,
which exercises it only incidentally through the engine.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from orchestrator.models import RunState, StepResult, StepStatus
from orchestrator.state import StateStore


def test_get_run_returns_steps_in_execution_order():
    """Caught by an actual end-to-end run, not a unit test: without an
    explicit ORDER BY, SQLite doesn't guarantee row order, so a multi-step
    run's `.steps` dict could come back in an order that didn't match
    execution order - which broke cli.py's 'what's the most recent step'
    logic (it printed 'completed through script' for a run that had actually
    gone on to park at review_gate). Steps are touched multiple times each
    here (simulated retries) before the final one, so a coincidental pass
    isn't hiding the same bug again.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(str(Path(tmp) / "state.db"))
        run = RunState.new(seed_topic="t", platforms=["youtube"], manifest_path="m.yaml")
        store.create_run(run)

        for step_name in ["alpha", "beta", "gamma"]:
            store.save_step_result(StepResult(run_id=run.run_id, step_name=step_name, status=StepStatus.RUNNING))
            store.save_step_result(StepResult(run_id=run.run_id, step_name=step_name, status=StepStatus.RUNNING, attempt=2))
            store.save_step_result(StepResult(run_id=run.run_id, step_name=step_name, status=StepStatus.SUCCEEDED, attempt=2))

        fetched = store.get_run(run.run_id)
        assert list(fetched.steps.keys()) == ["alpha", "beta", "gamma"]
        print("PASS test_get_run_returns_steps_in_execution_order")


if __name__ == "__main__":
    test_get_run_returns_steps_in_execution_order()
    print("\nall state store tests passed")
