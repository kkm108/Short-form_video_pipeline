"""browser-use isn't installed by default (it needs Playwright + browser
binaries, unavailable in a sandboxed build), so the actual generation task
can't run live here. What's fully testable without a real browser or target
site - and matters most for a step that could otherwise burn its retry
budget on a config typo, or silently treat a stalled run as a success - is:
every missing-config case fails fast and clearly, and interpret_history()
correctly distinguishes an actually-successful run from one that merely
didn't crash.

The "package not installed" test forces that with sys.modules rather than
relying on the package actually being absent - the same fix the YouTube
publisher tests needed, for the same reason: this suite is sometimes run in
an environment (like this one, temporarily) with browser-use genuinely
installed to verify signatures against it.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from executors.base import ExecutorError, StepContext
from executors.browser_use_adapter import BrowserUseAdapter, build_llm, interpret_history


def test_fails_fast_without_session_profile_configured():
    context = StepContext(
        run_id="t1", seed_topic="topic", platforms=["youtube"],
        step_config={}, workdir="/tmp", upstream={},
    )
    try:
        BrowserUseAdapter().run(context)
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        print("PASS test_fails_fast_without_session_profile_configured")


def test_fails_fast_when_session_file_does_not_exist():
    context = StepContext(
        run_id="t2", seed_topic="topic", platforms=["youtube"],
        step_config={"session_profile": "/tmp/definitely-does-not-exist.json", "task_template": "do {topic}"},
        workdir="/tmp", upstream={},
    )
    try:
        BrowserUseAdapter().run(context)
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        assert "does-not-exist" in str(exc)
        print("PASS test_fails_fast_when_session_file_does_not_exist")


def test_fails_fast_without_task_template():
    with tempfile.TemporaryDirectory() as tmp:
        session_path = Path(tmp) / "session.json"
        session_path.write_text("{}")
        context = StepContext(
            run_id="t3", seed_topic="topic", platforms=["youtube"],
            step_config={"session_profile": str(session_path)},  # no task_template
            workdir="/tmp", upstream={},
        )
        try:
            BrowserUseAdapter().run(context)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.retryable is False
            print("PASS test_fails_fast_without_task_template")


def test_fails_fast_without_llm_provider_configured():
    """The gap the run() log actually caught: cfg.get('llm') being absent
    used to fall through silently into browser-use's own hosted default,
    which then failed on a BROWSER_USE_API_KEY this project never asked for.
    Now it's a clear, local, non-retryable error instead."""
    with tempfile.TemporaryDirectory() as tmp:
        session_path = Path(tmp) / "session.json"
        session_path.write_text("{}")
        context = StepContext(
            run_id="t4", seed_topic="topic", platforms=["youtube"],
            step_config={"session_profile": str(session_path), "task_template": "do {topic}"},  # no llm_provider
            workdir=tmp, upstream={},
        )
        try:
            BrowserUseAdapter().run(context)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.retryable is False
            assert "llm_provider" in str(exc)
            print("PASS test_fails_fast_without_llm_provider_configured")


def test_reports_missing_browser_use_dependency_clearly():
    """Forced via sys.modules rather than assuming the package is absent -
    see module docstring."""
    with patch.dict(sys.modules, {"browser_use": None}):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            session_path.write_text("{}")
            context = StepContext(
                run_id="t5", seed_topic="topic", platforms=["youtube"],
                step_config={
                    "session_profile": str(session_path),
                    "task_template": "Generate assets for {topic}",
                    "llm_provider": "openai", "llm_model": "gpt-4o-mini", "llm_api_key_env": "TEST_BU_KEY",
                },
                workdir=tmp, upstream={},
            )
            import os
            os.environ["TEST_BU_KEY"] = "fake"
            try:
                BrowserUseAdapter().run(context)
                assert False, "expected ExecutorError"
            except ExecutorError as exc:
                assert "browser-use is not installed" in str(exc)
                assert exc.retryable is False
                print("PASS test_reports_missing_browser_use_dependency_clearly")
            finally:
                del os.environ["TEST_BU_KEY"]


def test_build_llm_fails_fast_without_model():
    try:
        build_llm({"llm_provider": "openai"})
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        print("PASS test_build_llm_fails_fast_without_model")


def test_build_llm_fails_fast_without_api_key():
    import os
    os.environ.pop("LLM_API_KEY", None)
    try:
        build_llm({"llm_provider": "openai", "llm_model": "gpt-4o-mini"})
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        print("PASS test_build_llm_fails_fast_without_api_key")


def test_build_llm_rejects_unsupported_provider():
    import os
    os.environ["LLM_API_KEY"] = "fake"
    try:
        build_llm({"llm_provider": "carrier-pigeon", "llm_model": "x"})
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
    finally:
        del os.environ["LLM_API_KEY"]
    print("PASS test_build_llm_rejects_unsupported_provider")


def test_build_llm_constructs_real_openai_client():
    """browser-use IS installed in this environment (see module docstring) -
    while it is, confirm build_llm() actually produces a real, usable
    ChatOpenAI instance from plain manifest config, not just that it fails
    correctly when misconfigured."""
    try:
        import browser_use.llm  # type: ignore[import-not-found]  # noqa: F401, optional dep; guarded by the except below
    except ImportError:
        print("SKIP test_build_llm_constructs_real_openai_client (browser-use not installed here)")
        return

    import os
    os.environ["TEST_BU_KEY2"] = "fake-key"
    try:
        llm = build_llm({"llm_provider": "openai", "llm_model": "gpt-4o-mini", "llm_api_key_env": "TEST_BU_KEY2"})
        assert type(llm).__name__ == "ChatOpenAI"
        print("PASS test_build_llm_constructs_real_openai_client")
    finally:
        del os.environ["TEST_BU_KEY2"]


class _FakeHistory:
    """Duck-types browser_use.agent.views.AgentHistoryList's public surface -
    checked against the real installed package (0.13.8), not guessed."""

    def __init__(self, successful, done=True, errors=None, final=None, steps=3):
        self._successful = successful
        self._done = done
        self._errors = errors or []
        self._final = final
        self._steps = steps

    def is_successful(self):
        return self._successful

    def is_done(self):
        return self._done

    def has_errors(self):
        return bool(self._errors)

    def errors(self):
        return self._errors

    def number_of_steps(self):
        return self._steps

    def final_result(self):
        return self._final


def test_interpret_history_raises_when_explicitly_unsuccessful():
    history = _FakeHistory(successful=False, errors=["could not find the generate button"])
    try:
        interpret_history(history, cfg={})
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert "could not find the generate button" in str(exc)
        print("PASS test_interpret_history_raises_when_explicitly_unsuccessful")


def test_interpret_history_raises_when_unjudged_and_not_done():
    """is_successful() returning None means 'ran to completion but nobody
    judged it' - that's only a real failure if is_done() also says the task
    never finished. Both conditions together is the actual failure case."""
    history = _FakeHistory(successful=None, done=False)
    try:
        interpret_history(history, cfg={})
        assert False, "expected ExecutorError"
    except ExecutorError:
        print("PASS test_interpret_history_raises_when_unjudged_and_not_done")


def test_interpret_history_succeeds_when_unjudged_but_done():
    history = _FakeHistory(successful=None, done=True, steps=7)
    result = interpret_history(history, cfg={"download_dir": "/tmp/does-not-exist-here"})
    assert result["steps_taken"] == 7
    print("PASS test_interpret_history_succeeds_when_unjudged_but_done")


def test_interpret_history_extracts_downloaded_files_on_success():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "img1.png").write_bytes(b"fake")
        (Path(tmp) / "img2.jpg").write_bytes(b"fake")
        (Path(tmp) / "notes.txt").write_bytes(b"ignored")  # not a media extension

        history = _FakeHistory(successful=True, final="done generating assets")
        result = interpret_history(history, cfg={"download_dir": tmp})

        assert result["clip_paths"] == sorted([str(Path(tmp) / "img1.png"), str(Path(tmp) / "img2.jpg")])
        assert result["final_result"] == "done generating assets"
        print("PASS test_interpret_history_extracts_downloaded_files_on_success")


if __name__ == "__main__":
    test_fails_fast_without_session_profile_configured()
    test_fails_fast_when_session_file_does_not_exist()
    test_fails_fast_without_task_template()
    test_fails_fast_without_llm_provider_configured()
    test_reports_missing_browser_use_dependency_clearly()
    test_build_llm_fails_fast_without_model()
    test_build_llm_fails_fast_without_api_key()
    test_build_llm_rejects_unsupported_provider()
    test_build_llm_constructs_real_openai_client()
    test_interpret_history_raises_when_explicitly_unsuccessful()
    test_interpret_history_raises_when_unjudged_and_not_done()
    test_interpret_history_succeeds_when_unjudged_but_done()
    test_interpret_history_extracts_downloaded_files_on_success()
    print("\nall browser_use adapter tests passed (the live generation task itself still needs a real browser-use install + session)")
