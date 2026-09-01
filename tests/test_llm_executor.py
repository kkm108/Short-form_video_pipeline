"""End-to-end test for the LLM script executor: a real HTTP round trip
against a local mock OpenAI-compatible server (no external API key or
network access needed), including one run pushed all the way through the
Pipeline so retry + state persistence get exercised too, not just the
executor class in isolation.
"""
from __future__ import annotations

import json
import os
import tempfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from executors.base import ExecutorError, StepContext
from executors.llm import LlmScriptExecutor
from orchestrator.engine import Pipeline
from orchestrator.state import StateStore


class _MockLlmServer:
    """Queues canned (status_code, body) responses; pops one per POST.
    Records every request it receives so tests can assert on headers/body."""

    def __init__(self, responses: list[tuple[int, dict]]):
        self.responses = list(responses)
        self.requests_seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests_seen.append(
                    {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
                )
                status, payload = outer.responses.pop(0) if outer.responses else (500, {"error": "no more mock responses"})
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass  # keep test output quiet

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()


def _openai_response(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


def test_llm_executor_writes_script_file():
    server = _MockLlmServer([(200, _openai_response("Line one.\nLine two."))])
    old_key = os.environ.get("LLM_API_KEY")
    os.environ["LLM_API_KEY"] = "test-key-123"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            context = StepContext(
                run_id="t1",
                seed_topic="the shortest war in history",
                platforms=["youtube"],
                workdir=tmp,
                upstream={},
                step_config={"base_url": server.base_url, "model": "test-model"},
            )
            output = LlmScriptExecutor().run(context)

            assert Path(output.output_ref).read_text() == "Line one.\nLine two."
            assert output.data["script"] == "Line one.\nLine two."
            sent = server.requests_seen[0]
            assert sent["auth"] == "Bearer test-key-123"
            assert sent["body"]["messages"][1]["content"] == "the shortest war in history"
            print("PASS test_llm_executor_writes_script_file")
    finally:
        server.stop()
        _restore_env("LLM_API_KEY", old_key)


def test_llm_executor_retries_on_429_then_succeeds():
    server = _MockLlmServer(
        [
            (429, {"error": "rate limited"}),
            (200, _openai_response("Retried script.")),
        ]
    )
    old_key = os.environ.get("TEST_LLM_KEY")
    os.environ["TEST_LLM_KEY"] = "test-key-456"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.yaml"
            manifest_path.write_text(
                textwrap.dedent(
                    f"""
                    run:
                      platforms: [youtube]
                    steps:
                      - name: script
                        executor: llm
                        base_url: {server.base_url}
                        api_key_env: TEST_LLM_KEY
                        retry: {{max_attempts: 3, backoff: fixed, base_delay_s: 0.05, retry_on: [429]}}
                    """
                )
            )
            pipeline = Pipeline(
                state=StateStore(str(Path(tmp) / "state.db")),
                executors={"llm": LlmScriptExecutor()},
                workdir=tmp,
            )
            run_id = pipeline.start(str(manifest_path), "seed topic")
            run = pipeline.state.get_run(run_id)

            assert run.steps["script"].status.value == "succeeded"
            assert run.steps["script"].attempt == 2  # failed once (429), succeeded on retry
            assert len(server.requests_seen) == 2
            assert Path(run.steps["script"].output_ref).read_text() == "Retried script."
            print("PASS test_llm_executor_retries_on_429_then_succeeds")
    finally:
        server.stop()
        _restore_env("TEST_LLM_KEY", old_key)


def test_llm_executor_fails_fast_without_credentials():
    old_key = os.environ.pop("LLM_API_KEY", None)
    old_base = os.environ.pop("LLM_BASE_URL", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            context = StepContext(
                run_id="t3", seed_topic="topic", platforms=["youtube"],
                workdir=tmp, upstream={}, step_config={},
            )
            try:
                LlmScriptExecutor().run(context)
                assert False, "expected ExecutorError"
            except ExecutorError as exc:
                assert exc.retryable is False  # missing config should never burn retry budget
                print("PASS test_llm_executor_fails_fast_without_credentials")
    finally:
        _restore_env("LLM_API_KEY", old_key)
        _restore_env("LLM_BASE_URL", old_base)


def test_llm_script_output_flows_to_next_step():
    """Proves the core step-to-step contract: whatever the llm executor puts
    in its ExecutorOutput is exactly what the next step receives via
    context.upstream['script'] - Invariant 1 from the original spec, checked
    in code instead of just asserted in prose."""
    from executors.base import ExecutorOutput

    server = _MockLlmServer([(200, _openai_response("Handoff check script."))])
    old_key = os.environ.get("LLM_API_KEY")
    os.environ["LLM_API_KEY"] = "test-key-789"
    captured = {}

    class RecordingExecutor:
        name = "recorder"

        def run(self, context):
            captured["script_output"] = context.upstream["script"]
            return ExecutorOutput(output_ref="recorded")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.yaml"
            manifest_path.write_text(
                textwrap.dedent(
                    f"""
                    run:
                      platforms: [youtube]
                    steps:
                      - name: script
                        executor: llm
                        base_url: {server.base_url}
                      - name: next_step
                        executor: recorder
                    """
                )
            )
            pipeline = Pipeline(
                state=StateStore(str(Path(tmp) / "state.db")),
                executors={"llm": LlmScriptExecutor(), "recorder": RecordingExecutor()},
                workdir=tmp,
            )
            pipeline.start(str(manifest_path), "seed topic")

            assert captured["script_output"].data["script"] == "Handoff check script."
            assert Path(captured["script_output"].output_ref).read_text() == "Handoff check script."
            print("PASS test_llm_script_output_flows_to_next_step")
    finally:
        server.stop()
        _restore_env("LLM_API_KEY", old_key)


def _restore_env(key: str, old_value):
    if old_value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = old_value


if __name__ == "__main__":
    test_llm_executor_writes_script_file()
    test_llm_executor_retries_on_429_then_succeeds()
    test_llm_executor_fails_fast_without_credentials()
    test_llm_script_output_flows_to_next_step()
    print("\nall llm executor tests passed")
