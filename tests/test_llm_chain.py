"""Three provider shapes (anthropic, gemini, openai-compatible) each get a
real HTTP round trip against a local mock, not just a code read-through -
this project has a track record of exactly this kind of request/response
shape being subtly wrong until actually exercised. The CLI path gets a real
subprocess call against a tiny throwaway script, not a mock, since the
whole point of that path is "shell out to a real executable."
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from executors.base import ExecutorError, StepContext
from executors.llm_chain import (
    LlmChainExecutor,
    _call_cli,
    _call_http,
    call_provider,
    discover_providers,
    resolve_api_key,
)


class _MockLlmServer:
    """Branches on request path so one server can stand in for all three
    provider shapes at once (anthropic hits /messages, gemini hits
    /models/...:generateContent, openai-compatible hits /chat/completions)."""

    def __init__(self, response_body: dict, status: int = 200):
        self.response_body = response_body
        self.status = status
        self.requests_seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests_seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(outer.response_body).encode())

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


def _set_env(key: str, value: str):
    old = os.environ.get(key)
    os.environ[key] = value
    return old


def _restore_env(key: str, old):
    if old is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = old


def test_discover_providers_sorts_by_priority_and_skips_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "010-second.json").write_text(json.dumps({"provider": "b", "type": "api"}))
        (Path(tmp) / "000-first.json").write_text(json.dumps({"provider": "a", "type": "api"}))
        (Path(tmp) / "005-off.json").write_text(json.dumps({"provider": "c", "type": "api", "enabled": False}))
        (Path(tmp) / "999-broken.json").write_text("{not valid json")

        found = discover_providers(tmp)
        assert [p["provider"] for p in found] == ["a", "b"]
        print("PASS test_discover_providers_sorts_by_priority_and_skips_disabled")


def test_resolve_api_key_via_env_fallback():
    old = _set_env("MY_TEST_PROVIDER_KEY", "secret-123")
    try:
        assert resolve_api_key({"api_key_ref": "my_test_provider_key"}) == "secret-123"
        print("PASS test_resolve_api_key_via_env_fallback")
    finally:
        _restore_env("MY_TEST_PROVIDER_KEY", old)


def test_resolve_api_key_returns_none_when_no_ref_configured():
    assert resolve_api_key({"type": "cli", "command": "claude"}) is None
    print("PASS test_resolve_api_key_returns_none_when_no_ref_configured")


def test_call_http_anthropic_shape():
    server = _MockLlmServer({"content": [{"type": "text", "text": "Hello from Claude."}]})
    old = _set_env("TEST_ANTHROPIC_KEY", "ant-key-abc")
    try:
        cfg = {"provider": "anthropic", "model": "claude-test", "base_url": server.base_url, "api_key_ref": "test_anthropic_key"}
        text = _call_http(cfg, "write something", "be terse", 500)

        assert text == "Hello from Claude."
        sent = server.requests_seen[0]
        assert sent["path"] == "/messages"
        assert sent["headers"]["x-api-key"] == "ant-key-abc"
        assert sent["headers"]["anthropic-version"] == "2023-06-01"
        assert sent["body"]["system"] == "be terse"
        assert sent["body"]["messages"][0]["content"] == "write something"
        print("PASS test_call_http_anthropic_shape")
    finally:
        server.stop()
        _restore_env("TEST_ANTHROPIC_KEY", old)


def test_call_http_gemini_shape():
    server = _MockLlmServer({"candidates": [{"content": {"parts": [{"text": "Hello from Gemini."}]}}]})
    old = _set_env("TEST_GEMINI_KEY", "gem-key-xyz")
    try:
        cfg = {"provider": "gemini", "model": "gemini-test", "base_url": server.base_url, "api_key_ref": "test_gemini_key"}
        text = _call_http(cfg, "write something", "be terse", 500)

        assert text == "Hello from Gemini."
        sent = server.requests_seen[0]
        assert sent["path"] == "/models/gemini-test:generateContent?key=gem-key-xyz"
        assert sent["body"]["contents"][0]["parts"][0]["text"] == "write something"
        assert sent["body"]["system_instruction"]["parts"][0]["text"] == "be terse"
        print("PASS test_call_http_gemini_shape")
    finally:
        server.stop()
        _restore_env("TEST_GEMINI_KEY", old)


def test_call_http_openai_compatible_shape():
    server = _MockLlmServer({"choices": [{"message": {"content": "Hello from GPT."}}]})
    old = _set_env("TEST_OPENAI_KEY", "oai-key-123")
    try:
        cfg = {"provider": "openai", "model": "gpt-test", "base_url": server.base_url, "api_key_ref": "test_openai_key"}
        text = _call_http(cfg, "write something", "be terse", 500)

        assert text == "Hello from GPT."
        sent = server.requests_seen[0]
        assert sent["path"] == "/chat/completions"
        assert sent["headers"]["authorization"] == "Bearer oai-key-123"
        assert sent["body"]["messages"][1]["content"] == "write something"
        print("PASS test_call_http_openai_compatible_shape")
    finally:
        server.stop()
        _restore_env("TEST_OPENAI_KEY", old)


def test_call_http_raises_retryable_on_429():
    server = _MockLlmServer({"error": "slow down"}, status=429)
    old = _set_env("TEST_RATE_KEY", "k")
    try:
        cfg = {"provider": "openai", "model": "m", "base_url": server.base_url, "api_key_ref": "test_rate_key"}
        try:
            _call_http(cfg, "p", "s", 100)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.status_code == 429
            assert exc.retryable is True
            print("PASS test_call_http_raises_retryable_on_429")
    finally:
        server.stop()
        _restore_env("TEST_RATE_KEY", old)


def test_call_http_missing_credential_does_not_crash_and_is_non_retryable():
    """A missing key for one provider shouldn't look like a transient
    network problem - it needs config, not a retry."""
    os.environ.pop("TEST_MISSING_KEY_XYZ", None)
    cfg = {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1", "api_key_ref": "test_missing_key_xyz"}
    try:
        _call_http(cfg, "p", "s", 100)
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        print("PASS test_call_http_missing_credential_does_not_crash_and_is_non_retryable")


def _make_fake_cli_script(tmp: str, reply: str, exit_code: int = 0) -> str:
    """A real, tiny executable - proves _call_cli's subprocess mechanics
    (command + args + prompt-as-last-arg + capturing stdout) against an
    actual process, not a mock."""
    script_path = Path(tmp) / "fake_cli.py"
    script_path.write_text(textwrap.dedent(f"""
        import sys
        prompt = sys.argv[-1]
        print({reply!r} + " (prompt was: " + prompt + ")")
        sys.exit({exit_code})
    """))
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return str(script_path)


def test_call_cli_invokes_real_subprocess_and_captures_stdout():
    with tempfile.TemporaryDirectory() as tmp:
        script = _make_fake_cli_script(tmp, "canned reply")
        cfg = {"command": sys.executable, "args": [script]}
        result = _call_cli(cfg, "my prompt")
        assert result == "canned reply (prompt was: my prompt)"
        print("PASS test_call_cli_invokes_real_subprocess_and_captures_stdout")


def test_call_cli_raises_on_nonzero_exit():
    with tempfile.TemporaryDirectory() as tmp:
        script = _make_fake_cli_script(tmp, "boom", exit_code=1)
        cfg = {"command": sys.executable, "args": [script]}
        try:
            _call_cli(cfg, "p")
            assert False, "expected ExecutorError"
        except ExecutorError:
            print("PASS test_call_cli_raises_on_nonzero_exit")


def test_call_cli_raises_non_retryable_when_command_missing():
    try:
        call_provider({"type": "cli", "command": "/no/such/binary-xyz"}, "p", "s", 100)
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        print("PASS test_call_cli_raises_non_retryable_when_command_missing")


def test_chain_falls_through_to_second_provider_when_first_fails():
    dead_server_url = "http://127.0.0.1:1"  # nothing listens here
    good_server = _MockLlmServer({"choices": [{"message": {"content": "second provider answered"}}]})
    old = _set_env("TEST_CHAIN_KEY", "k")
    try:
        with tempfile.TemporaryDirectory() as provider_dir, tempfile.TemporaryDirectory() as workdir:
            (Path(provider_dir) / "000-broken.json").write_text(
                json.dumps({"provider": "openai", "model": "m", "base_url": dead_server_url, "api_key_ref": "test_chain_key"})
            )
            (Path(provider_dir) / "010-good.json").write_text(
                json.dumps({"provider": "openai", "model": "m", "base_url": good_server.base_url, "api_key_ref": "test_chain_key"})
            )
            context = StepContext(
                run_id="r1", seed_topic="topic here", platforms=["youtube"],
                step_config={"provider_dir": provider_dir}, workdir=workdir, upstream={},
            )
            output = LlmChainExecutor().run(context)

            assert output.data["script"] == "second provider answered"
            assert output.data["provider_used"] == "openai"
            assert len(good_server.requests_seen) == 1
            print("PASS test_chain_falls_through_to_second_provider_when_first_fails")
    finally:
        good_server.stop()
        _restore_env("TEST_CHAIN_KEY", old)


def test_chain_stops_at_first_success_and_skips_remaining_providers():
    first_server = _MockLlmServer({"choices": [{"message": {"content": "first provider answered"}}]})
    old = _set_env("TEST_CHAIN_KEY2", "k")
    try:
        with tempfile.TemporaryDirectory() as provider_dir, tempfile.TemporaryDirectory() as workdir:
            (Path(provider_dir) / "000-first.json").write_text(
                json.dumps({"provider": "openai", "model": "m", "base_url": first_server.base_url, "api_key_ref": "test_chain_key2"})
            )
            # A second provider that would fail loudly if ever called - never should be
            (Path(provider_dir) / "010-unreachable.json").write_text(
                json.dumps({"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1", "api_key_ref": "test_chain_key2"})
            )
            context = StepContext(
                run_id="r2", seed_topic="topic", platforms=["youtube"],
                step_config={"provider_dir": provider_dir}, workdir=workdir, upstream={},
            )
            output = LlmChainExecutor().run(context)

            assert output.data["script"] == "first provider answered"
            print("PASS test_chain_stops_at_first_success_and_skips_remaining_providers")
    finally:
        first_server.stop()
        _restore_env("TEST_CHAIN_KEY2", old)


def test_chain_raises_when_every_provider_fails():
    with tempfile.TemporaryDirectory() as provider_dir, tempfile.TemporaryDirectory() as workdir:
        (Path(provider_dir) / "000-dead.json").write_text(
            json.dumps({"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1", "api_key_ref": "totally_unset_key"})
        )
        context = StepContext(
            run_id="r3", seed_topic="topic", platforms=["youtube"],
            step_config={"provider_dir": provider_dir}, workdir=workdir, upstream={},
        )
        try:
            LlmChainExecutor().run(context)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert "every provider in the chain failed" in str(exc)
            print("PASS test_chain_raises_when_every_provider_fails")


def test_chain_raises_clearly_when_provider_dir_is_empty():
    with tempfile.TemporaryDirectory() as provider_dir, tempfile.TemporaryDirectory() as workdir:
        context = StepContext(
            run_id="r4", seed_topic="topic", platforms=["youtube"],
            step_config={"provider_dir": provider_dir}, workdir=workdir, upstream={},
        )
        try:
            LlmChainExecutor().run(context)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.retryable is False
            print("PASS test_chain_raises_clearly_when_provider_dir_is_empty")


if __name__ == "__main__":
    test_discover_providers_sorts_by_priority_and_skips_disabled()
    test_resolve_api_key_via_env_fallback()
    test_resolve_api_key_returns_none_when_no_ref_configured()
    test_call_http_anthropic_shape()
    test_call_http_gemini_shape()
    test_call_http_openai_compatible_shape()
    test_call_http_raises_retryable_on_429()
    test_call_http_missing_credential_does_not_crash_and_is_non_retryable()
    test_call_cli_invokes_real_subprocess_and_captures_stdout()
    test_call_cli_raises_on_nonzero_exit()
    test_call_cli_raises_non_retryable_when_command_missing()
    test_chain_falls_through_to_second_provider_when_first_fails()
    test_chain_stops_at_first_success_and_skips_remaining_providers()
    test_chain_raises_when_every_provider_fails()
    test_chain_raises_clearly_when_provider_dir_is_empty()
    print("\nall llm_chain tests passed")
