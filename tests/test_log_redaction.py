"""Secret-safe logging regression test.

Every logger.* call in this codebase must be safe to run with DEBUG logging
on and paste into a bug report. The known failure mode is a credential
reaching a log message through an exception's str() - specifically the gemini
provider embedding its API key in the request URL, so a failing request
reproduces that URL (key included) in the exception text, which then lands in
logger.warning(...). These tests run a representative slice of the pipeline
with fake-but-realistic-looking secrets and assert none of the secret values
ever appear in captured log output.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from executors.base import ExecutorError, StepContext
from executors.llm_chain import LlmChainExecutor, _call_http

FAKE_KEY = "sk-F4k3-realistic-looking-api-key-0123456789ABCDEFXYZ"
ENV_VAR = "TEST_REDACT_KEY"


class _CaptureLogs(logging.Handler):
    """One-shot handler that records every formatted message it sees."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


def _gemini_provider_config(provider_dir: str, base_url: str) -> str:
    path = Path(provider_dir) / "000-gemini.json"
    path.write_text(
        json.dumps(
            {"type": "api", "provider": "gemini", "model": "gemini-2.0-flash", "base_url": base_url, "api_key_ref": "test_redact_key"}
        )
    )
    return str(path)


def test_gemini_request_error_message_redacts_api_key():
    cfg = {"type": "api", "provider": "gemini", "model": "m", "base_url": "http://127.0.0.1:1", "api_key_ref": "test_redact_key"}
    old = os.environ.get(ENV_VAR)
    os.environ[ENV_VAR] = FAKE_KEY
    try:
        # Unreachable base_url forces a ConnectionError whose str contains the
        # full URL - which for gemini carries ?key=<FAKE_KEY>.
        try:
            _call_http(cfg, "prompt", "system", 100)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert FAKE_KEY not in str(exc)
            assert "[REDACTED]" in str(exc)
    finally:
        if old is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = old
    print("PASS test_gemini_request_error_message_redacts_api_key")


def test_pipeline_executor_logs_do_not_contain_secret_values():
    # A realistic secret baked into a gemini request should never show up in
    # any captured log line, whether the failure is reported by the chain's own
    # logger or bubbles up through the engine's ExecutorError logging.
    capture = _CaptureLogs()
    root = logging.getLogger()
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory() as provider_dir, tempfile.TemporaryDirectory() as workdir:
            _gemini_provider_config(provider_dir, "http://127.0.0.1:1")
            old = os.environ.get(ENV_VAR)
            os.environ[ENV_VAR] = FAKE_KEY
            try:
                context = StepContext(
                    run_id="r-redact",
                    seed_topic="a topic",
                    platforms=["youtube"],
                    step_config={"provider_dir": provider_dir},
                    workdir=workdir,
                    upstream={},
                    step_name="script",
                )
                try:
                    LlmChainExecutor().run(context)
                    assert False, "expected ExecutorError (every provider failed)"
                except ExecutorError:
                    pass
            finally:
                if old is None:
                    os.environ.pop(ENV_VAR, None)
                else:
                    os.environ[ENV_VAR] = old
    finally:
        root.removeHandler(capture)

    assert capture.messages, "expected at least one log line to be emitted"
    for msg in capture.messages:
        assert FAKE_KEY not in msg, f"secret leaked into log: {msg!r}"
    print("PASS test_pipeline_executor_logs_do_not_contain_secret_values")


if __name__ == "__main__":
    test_gemini_request_error_message_redacts_api_key()
    test_pipeline_executor_logs_do_not_contain_secret_values()
    print("\nall log redaction tests passed")
