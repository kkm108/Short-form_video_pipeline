"""Proves the review gate's one load-bearing property: there is no code path
in which it returns success on its own. It either parks (AwaitingApproval)
or - if the webhook itself is down - still parks. Only an external
engine.approve() call, exercised already in test_orchestrator.py, can move a
run past it.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from executors.base import AwaitingApproval, ExecutorOutput, StepContext
from executors.human_checkpoint import HumanCheckpointExecutor


class _MockWebhook:
    def __init__(self):
        self.received: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.received.append(json.loads(self.rfile.read(length) or b"{}"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


def test_checkpoint_notifies_and_parks():
    webhook = _MockWebhook()
    try:
        context = StepContext(
            run_id="run_abc", seed_topic="a topic", platforms=["youtube"],
            step_config={"notify_webhook": webhook.url}, workdir="/tmp",
            upstream={"assembly": ExecutorOutput(output_ref="/tmp/assembled.mp4")},
        )
        try:
            HumanCheckpointExecutor().run(context)
            assert False, "expected AwaitingApproval"
        except AwaitingApproval:
            pass

        assert len(webhook.received) == 1
        assert "run_abc" in webhook.received[0]["text"]
        assert "/tmp/assembled.mp4" in webhook.received[0]["text"]
        print("PASS test_checkpoint_notifies_and_parks")
    finally:
        webhook.stop()


def test_checkpoint_still_parks_if_webhook_is_dead():
    context = StepContext(
        run_id="run_xyz", seed_topic="a topic", platforms=["youtube"],
        step_config={"notify_webhook": "http://127.0.0.1:1"},  # nothing listens on port 1
        workdir="/tmp", upstream={},
    )
    try:
        HumanCheckpointExecutor().run(context)
        assert False, "expected AwaitingApproval even though the webhook is unreachable"
    except AwaitingApproval:
        print("PASS test_checkpoint_still_parks_if_webhook_is_dead")


def test_checkpoint_parks_even_with_no_webhook_configured():
    context = StepContext(
        run_id="run_none", seed_topic="a topic", platforms=["youtube"],
        step_config={}, workdir="/tmp", upstream={},
    )
    try:
        HumanCheckpointExecutor().run(context)
        assert False, "expected AwaitingApproval"
    except AwaitingApproval:
        print("PASS test_checkpoint_parks_even_with_no_webhook_configured")


if __name__ == "__main__":
    test_checkpoint_notifies_and_parks()
    test_checkpoint_still_parks_if_webhook_is_dead()
    test_checkpoint_parks_even_with_no_webhook_configured()
    print("\nall human checkpoint tests passed")
