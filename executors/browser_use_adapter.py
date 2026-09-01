"""Generic wrapper around browser-use for steps that drive a consumer AI
tool's own web UI (image/video generation, TTS) using a *pre-authenticated*
browser session - never a harvested cookie for a public-facing social
account. That distinction is the whole reason this exists as a separate,
narrower-scope module from anything touching YouTube/Instagram/TikTok: it
automates a tool you're already logged into for yourself, not a platform
you're posting to an audience through.

Signatures below (Agent, Browser, AgentHistoryList, ChatOpenAI/ChatAnthropic)
are checked against browser-use 0.13.8's real, installed API, not written
from memory. Two things only showed up from doing that:

1. Agent(llm=None) does NOT mean "no LLM" - browser-use silently falls back
   to its own hosted ChatBrowserUse() default, which then demands a
   BROWSER_USE_API_KEY you almost certainly don't have and were never told
   to set. build_llm() below exists specifically so a missing LLM config
   fails with a clear, actionable error from this manifest's own config
   checks, before browser-use's fallback ever gets a chance to run.
2. agent.run() returning without raising does NOT mean the task succeeded -
   it can run out of steps or stall without completing the goal and still
   hand back a normal-looking AgentHistoryList. interpret_history() checks
   is_successful()/is_done()/has_errors() explicitly rather than assuming
   "didn't crash" means "worked."

Both build_llm() and interpret_history() are plain functions specifically so
they're unit-testable without a real browser, a real target site, or (for
interpret_history) even browser-use installed at all - see
tests/test_browser_use_adapter.py. Running the actual task still needs a
real Playwright browser install and a session file you generate once
(scripts/export_session.py), neither of which is available in a sandboxed
build environment.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from executors.base import ExecutorError, ExecutorOutput, StepContext

_MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".mp4", ".webp"}


class BrowserUseAdapter:
    name = "browser_use"

    def run(self, context: StepContext) -> ExecutorOutput:
        cfg = context.step_config
        session_path = cfg.get("session_profile")
        if not session_path:
            raise ExecutorError("browser_use step needs 'session_profile' in its manifest config", retryable=False)
        if not Path(session_path).exists():
            raise ExecutorError(
                f"missing browser-use session profile at {session_path!r} - generate one by running "
                "scripts/export_session.py once yourself",
                retryable=False,
            )

        task_template = cfg.get("task_template")
        if not task_template:
            raise ExecutorError("browser_use step needs a 'task_template' in its manifest config", retryable=False)

        script_step = context.upstream.get("script")
        script = script_step.data.get("script", "") if script_step else ""
        task = task_template.format(topic=context.seed_topic, script=script)

        try:
            llm = build_llm(cfg)  # can itself raise ImportError (browser_use.llm) - handled below, same as _run_task's
            result = asyncio.run(self._run_task(task, session_path, llm, cfg))
        except ImportError as exc:
            raise ExecutorError(
                "browser-use is not installed - pip install browser-use, then playwright install chromium",
                retryable=False,
            ) from exc
        except ExecutorError:
            raise  # already the right shape (e.g. from build_llm or interpret_history) - don't re-wrap it
        except Exception as exc:  # browser-use surfaces its own exception types; caught broadly since it's an optional dep
            raise ExecutorError(f"browser-use task failed: {exc}") from exc

        return ExecutorOutput(output_ref=result["output_dir"], data=result)

    async def _run_task(self, task: str, session_path: str, llm: Any, cfg: dict) -> dict:
        from browser_use import Agent, Browser  # type: ignore[import-not-found]  # optional dep, imported lazily (see module docstring)

        browser = Browser(storage_state=session_path, headless=cfg.get("headless", True))
        agent: Agent[Any, Any] = Agent(task=task, browser=browser, llm=llm)
        history = await agent.run(max_steps=cfg.get("max_steps", 40))
        return interpret_history(history, cfg)


def build_llm(cfg: dict) -> Any:
    """Builds a browser-use LLM client from plain, YAML-representable config
    (provider name + model + which env var holds the key), since a manifest
    can't hold a live Python object directly. See module docstring for why
    this exists instead of just passing cfg.get('llm') through."""
    provider = cfg.get("llm_provider")
    if not provider:
        raise ExecutorError(
            "browser_use step needs 'llm_provider' (e.g. 'openai' or 'anthropic') in its manifest config - "
            "without it, browser-use falls back to its own hosted default and asks for a BROWSER_USE_API_KEY "
            "you almost certainly don't have",
            retryable=False,
        )

    model = cfg.get("llm_model")
    if not model:
        raise ExecutorError("browser_use step needs 'llm_model' alongside 'llm_provider'", retryable=False)

    api_key_env = cfg.get("llm_api_key_env", "LLM_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ExecutorError(
            f"browser_use step needs an API key in the {api_key_env} env var", retryable=False
        )

    base_url = cfg.get("llm_base_url")

    if provider == "openai":
        from browser_use.llm import ChatOpenAI  # type: ignore[import-not-found]  # optional dep
        return ChatOpenAI(model=model, api_key=api_key, base_url=base_url)
    if provider == "anthropic":
        from browser_use.llm import ChatAnthropic  # type: ignore[import-not-found]  # optional dep
        return ChatAnthropic(model=model, api_key=api_key, base_url=base_url)

    raise ExecutorError(
        f"unsupported llm_provider {provider!r} - add it in executors/browser_use_adapter.py's build_llm(), "
        "or use 'openai'/'anthropic' with a custom llm_base_url for any OpenAI- or Anthropic-compatible gateway",
        retryable=False,
    )


def interpret_history(history: Any, cfg: dict) -> dict:
    """agent.run() returning without raising does not mean the task
    succeeded - see point 2 in the module docstring. Treating "didn't
    crash" as "succeeded" here would hand the assembly step empty or wrong
    clip_paths and fail confusingly one step downstream, instead of failing
    clearly at the step that actually had the problem."""
    successful = history.is_successful()  # bool | None - None means "ran to completion but unjudged"
    if successful is False or (successful is None and not history.is_done()):
        reasons = [e for e in history.errors() if e] if history.has_errors() else []
        detail = "; ".join(reasons) or history.final_result() or "no further detail from browser-use"
        raise ExecutorError(f"browser-use task did not complete successfully after {history.number_of_steps()} steps: {detail}")

    download_dir = cfg.get("download_dir", "./downloads")
    return {
        "output_dir": download_dir,
        "clip_paths": _list_media_files(download_dir),
        "voiceover_path": cfg.get("voiceover_path"),  # set once you know which tool produces the voiceover
        "steps_taken": history.number_of_steps(),
        "final_result": history.final_result(),
    }


def _list_media_files(download_dir: str) -> list[str]:
    d = Path(download_dir)
    if not d.exists():
        return []
    return sorted(str(p) for p in d.glob("*") if p.suffix.lower() in _MEDIA_EXTS)
