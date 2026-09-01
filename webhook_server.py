"""Slack interactive-message callback receiver for `review_gate`.

An additional way in for approvals, alongside `cli.py approve`/`reject` - not
a replacement. When a human clicks an Approve/Reject button on the review
message the pipeline posts, Slack sends a form-encoded POST (a `payload` field
containing URL-encoded JSON) to this endpoint. It parses that, then calls the
exact same `orchestrator.engine.approve()` / `reject()` functions the CLI
uses, and resumes the run the way `cli.py approve` does.

Nothing here, and nothing in `human_checkpoint.py`, can approve a run on its
own - this only turns a human button click into the same state transitions the
CLI performs.
"""
from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs

from orchestrator.engine import Pipeline, approve, reject

logger = logging.getLogger("pipeline.webhook")

# Interactivity callback action ids - must match what review_gate's buttons
# carry (see executors/human_checkpoint.py's _build_notification).
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"


def _resolve_action(action: dict) -> tuple[bool, str, str, str]:
    """Turn one Slack action dict into (approved, run_id, step_name, reason).

    The button's `value` is a JSON string carrying run_id/step_name (and an
    optional reason), so it round-trips through Slack verbatim without being
    guessable from the URL or the message text.
    """
    action_id = action.get("action_id", "")
    approved = action_id == ACTION_APPROVE
    if action_id not in (ACTION_APPROVE, ACTION_REJECT):
        raise ValueError(f"unknown review action {action_id!r}")

    try:
        payload = json.loads(action.get("value", "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("button value was not valid JSON") from exc

    run_id = payload.get("run_id")
    step_name = payload.get("step_name")
    if not run_id or not step_name:
        raise ValueError("button value is missing run_id/step_name")

    return approved, str(run_id), str(step_name), str(payload.get("reason", ""))


def process_callback(pipeline: Pipeline, approved: bool, run_id: str, step_name: str, reason: str = "") -> None:
    """The state-transition half of a callback: approve/reject then resume,
    mirroring `cli.py`'s approve/reject subcommands."""
    if approved:
        approve(pipeline.state, run_id, step_name)
    else:
        reject(pipeline.state, run_id, step_name, reason)
    pipeline.resume(run_id)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Serves one Slack interactive-message callback.

    `_pipeline` and `_logger` are class attributes supplied by
    `make_server()`, since BaseHTTPRequestHandler instantiates a fresh handler
    per request with no constructor args.
    """

    _pipeline: Pipeline

    def do_POST(self) -> None:
        try:
            self._handle()
        except Exception as exc:  # keep the wire contract simple: never 500 with a traceback to Slack
            logger.error("webhook callback failed: %s", exc)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        fields = parse_qs(raw)
        payload_json = fields.get("payload", [None])[0]
        if not payload_json:
            raise ValueError("missing 'payload' form field")

        try:
            callback = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("payload was not valid JSON") from exc

        # TODO: verify Slack's request signature before trusting the payload.
        # Slack signs each callback with the app's Signing Secret and sends it
        # in X-Slack-Signature / X-Slack-Request-Timestamp. Compute
        # sha256(f"{version}:{timestamp}:{raw_body}") with the secret, compare
        # with a constant-time check, and reject on mismatch. Without a routed
        # request secret we intentionally do not claim to be secure here -
        # this endpoint should only be exposed behind auth/a private network
        # until that's wired up. Deliberately not silently skipped: it's a
        # known, documented gap, not an accidental one.

        actions = callback.get("actions", [])
        if not actions:
            raise ValueError("callback has no actions")

        approved, run_id, step_name, reason = _resolve_action(actions[0])
        process_callback(self._pipeline, approved, run_id, step_name, reason)
        logger.info("webhook %s for run %s at step %s", "approved" if approved else "rejected", run_id, step_name)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def log_message(self, *args):
        pass


def make_server(pipeline: Pipeline, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Build + serve one callback endpoint bound to `pipeline`'s state store.
    `port=0` picks an ephemeral port (handy for tests); a fresh handler
    instance is created per request and wired to `pipeline` via class attrs."""
    _CallbackHandler._pipeline = pipeline
    return ThreadingHTTPServer((host, port), _CallbackHandler)


def main(argv: Optional[list[str]] = None) -> None:
    """Run the endpoint for real. Reuses the same pipeline (and therefore the
    same SQLite state store) as the CLI, so approvals here and approvals typed
    into `cli.py` see the same runs."""
    parser = argparse.ArgumentParser(prog="webhook_server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Local import: build_pipeline pulls in every executor, none of which need
    # credentials at construction time, same as `cli.py start` does.
    from cli import build_pipeline

    pipeline, _ = build_pipeline()

    server = make_server(pipeline, host=args.host, port=args.port)
    logger.info("Slack review webhook listening on %s:%d", args.host, server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
