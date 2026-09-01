"""LLM step: seed topic -> script.

Provider-agnostic on purpose: point base_url at any OpenAI-compatible
/chat/completions endpoint - a direct provider, or a free/low-cost gateway
like the OmniRoute-style aggregator from the research pass - and nothing
else in the pipeline needs to know which one you picked.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from executors.base import ExecutorError, ExecutorOutput, StepContext

DEFAULT_SYSTEM_PROMPT = (
    "You write tight, punchy scripts for 30-45 second vertical short-form video. "
    "Return ONLY the spoken narration, split into short lines a voiceover can read "
    "naturally. No stage directions, no markdown, no scene headers."
)


class LlmScriptExecutor:
    name = "llm"

    def run(self, context: StepContext) -> ExecutorOutput:
        cfg = context.step_config
        api_key_env = cfg.get("api_key_env", "LLM_API_KEY")
        base_url = cfg.get("base_url") or os.environ.get("LLM_BASE_URL")
        api_key = os.environ.get(api_key_env)
        model = cfg.get("model", "gpt-4o-mini")

        if not base_url or not api_key:
            raise ExecutorError(
                f"llm executor needs 'base_url' (manifest or LLM_BASE_URL env) and an "
                f"API key in the {api_key_env} env var - retrying won't fix missing config",
                retryable=False,
            )

        try:
            resp = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": cfg.get("system_prompt", DEFAULT_SYSTEM_PROMPT)},
                        {"role": "user", "content": context.seed_topic},
                    ],
                },
                timeout=cfg.get("request_timeout_s", 60),
            )
        except requests.RequestException as exc:
            raise ExecutorError(f"llm request failed: {exc}") from exc

        if resp.status_code == 429:
            raise ExecutorError("llm provider rate limited", status_code=429)
        if not resp.ok:
            raise ExecutorError(
                f"llm provider returned {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code >= 500,
                status_code=resp.status_code,
            )

        try:
            script = resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ExecutorError(f"unexpected llm response shape: {resp.text[:300]}", retryable=False) from exc

        if not script:
            raise ExecutorError("llm returned an empty script", retryable=True)

        out_path = str(Path(context.workdir) / "script.txt")
        Path(out_path).write_text(script)

        return ExecutorOutput(output_ref=out_path, data={"script": script})
