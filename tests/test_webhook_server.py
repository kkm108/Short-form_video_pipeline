"""End-to-end test for the Slack review_gate callback receiver: a realistic
fake Slack interactive-message payload POSTed at the endpoint must actually
change the target run's state - the same approve()/reject() path the CLI uses,
not a no-op 'we received something' acknowledgment.
"""
from __future__ import annotations

import json
import tempfile
import textwrap
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from executors.base import AwaitingApproval, Executor, ExecutorOutput, StepContext
from orchestrator.engine import Pipeline
from orchestrator.state import StateStore
from webhook_server import make_server


class GateExecutor(Executor):
    name = "gate"

    def run(self, context: StepContext) -> ExecutorOutput:
        raise AwaitingApproval()


def _start_parked_run(tmp: str) -> tuple[Pipeline, str]:
    """Create a pipeline whose run parks at review_gate in AWAITING_APPROVAL,
    the state a real review webhook would be asked to move forward."""
    state = StateStore(str(Path(tmp) / "state.db"))
    pipeline = Pipeline(state=state, executors={"gate": GateExecutor()}, workdir=tmp)
    manifest = Path(tmp) / "manifest.yaml"
    manifest.write_text(
        textwrap.dedent(
            """
            run:
              platforms: [youtube]
            steps:
              - name: review_gate
                executor: gate
            """
        )
    )
    run_id = pipeline.start(str(manifest), "a topic to review")
    run = state.get_run(run_id)
    assert run is not None
    assert run.steps["review_gate"].status.value == "awaiting_approval"
    return pipeline, run_id


def _post_callback(port: int, action_id: str, run_id: str, step_name: str = "review_gate") -> int:
    """Post a form-encoded Slack interactive-message payload and return the
    HTTP status code."""
    callback = {
        "type": "interactive_message",
        "team": {"id": "T0001", "domain": "example"},
        "user": {"id": "U0001", "name": "reviewer"},
        "channel": {"id": "C0001", "name": "reviews"},
        "actions": [
            {
                "type": "button",
                "action_id": action_id,
                "text": "Approve",
                "value": json.dumps({"run_id": run_id, "step_name": step_name}),
            }
        ],
    }
    body = urllib.parse.urlencode({"payload": json.dumps(callback)}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


class _ServedServer:
    """Starts the webhook server on an ephemeral port in a background daemon
    thread and tears it down on exit - the same thread-per-server pattern the
    other mock-server tests use."""

    def __init__(self, pipeline):
        self.server = make_server(pipeline)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_approve_callback_moves_parked_run_to_succeeded():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline, run_id = _start_parked_run(tmp)
        served = _ServedServer(pipeline)
        try:
            status = _post_callback(served.port, "approve", run_id)
            assert status == 200
            run = pipeline.state.get_run(run_id)
            assert run.steps["review_gate"].status.value == "succeeded"
            print("PASS test_approve_callback_moves_parked_run_to_succeeded")
        finally:
            served.close()


def test_reject_callback_marks_run_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline, run_id = _start_parked_run(tmp)
        served = _ServedServer(pipeline)
        try:
            status = _post_callback(served.port, "reject", run_id)
            assert status == 200
            run = pipeline.state.get_run(run_id)
            assert run.steps["review_gate"].status.value == "rejected"
            print("PASS test_reject_callback_marks_run_rejected")
        finally:
            served.close()


def test_unknown_action_is_rejected_with_400():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline, run_id = _start_parked_run(tmp)
        served = _ServedServer(pipeline)
        try:
            try:
                _post_callback(served.port, "definitely-not-an-action", run_id)
                assert False, "expected HTTPError (400)"
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
                run = pipeline.state.get_run(run_id)
                assert run.steps["review_gate"].status.value == "awaiting_approval"  # untouched
            print("PASS test_unknown_action_is_rejected_with_400")
        finally:
            served.close()


if __name__ == "__main__":
    test_approve_callback_moves_parked_run_to_succeeded()
    test_reject_callback_marks_run_rejected()
    test_unknown_action_is_rejected_with_400()
    print("\nall webhook server tests passed")
