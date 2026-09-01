"""Multi-provider LLM fallback chain for the 'script' step - an alternative
to llm.py's single-endpoint executor, adapted from a chain the user already
had working outside this project. The discovery/priority/fallback logic is
preserved as-is; two things changed:

1. Provider config files (providers/*.json) name a credential (api_key_ref)
   instead of embedding the literal key. Vault.get() resolves it at call
   time, same as every other credential in this project - which also means
   these config files, unlike the originals, are safe to commit. No secret
   ever touches disk outside the OS keychain / env vars.
2. Every failure surfaces as ExecutorError, so a provider's status code
   (e.g. 429) flows into the engine's retry policy the same way every other
   executor's does, and a missing credential for one provider in the chain
   is treated as "try the next provider," not a crash.

Register this under a different executor name (llm_chain) than the
single-endpoint one (llm) - manifests opt in, it doesn't silently replace
the simpler path.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import requests

from credentials.vault import Vault
from executors.base import ExecutorError, ExecutorOutput, StepContext

logger = logging.getLogger("pipeline.llm_chain")

DEFAULT_SYSTEM_PROMPT = (
    "You write tight, punchy scripts for 30-45 second vertical short-form video. "
    "Return ONLY the spoken narration, split into short lines a voiceover can read naturally. "
    "No stage directions, no markdown, no scene headers."
)


def _redact(text: str, *secrets: Optional[str]) -> str:
    """Replace every occurrence of any provided secret with [REDACTED].

    The gemini provider embeds its API key in the request URL as a query
    param, and a failing `requests` call reproduces that URL - key included -
    in its exception message. Without redaction that key would travel into the
    ExecutorError and from there into `logger.warning(... exc)` and any engine
    log of the exception. This is the narrow guard for exactly that path.
    """
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


def discover_providers(provider_dir: str | Path) -> list[dict]:
    """Loads providers/*.json, sorted by the numeric prefix in the filename
    (lower = tried first), skipping any with "enabled": false or a
    malformed/unparseable file (logged, not silently dropped)."""
    d = Path(provider_dir)
    if not d.exists():
        return []
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            cfg = json.loads(f.read_text(encoding="utf-8-sig"))
            cfg["_priority"] = int(f.stem.split("-")[0])
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.warning("skipping unparseable provider config %s: %s", f, exc)
            continue
        if not cfg.get("enabled", True):
            continue
        cfg["_source_file"] = str(f)
        out.append(cfg)
    out.sort(key=lambda x: x["_priority"])
    return out


def resolve_api_key(cfg: dict) -> Optional[str]:
    """Provider configs name a vault key (api_key_ref); Vault.get() then
    falls back to the matching env var the same way every other credential
    here does. type: "cli" providers legitimately have no api_key_ref at
    all - the CLI tool manages its own auth."""
    ref = cfg.get("api_key_ref")
    if not ref:
        return None
    return Vault().get(ref)


def call_provider(cfg: dict, prompt: str, system: str, max_tokens: int) -> str:
    ptype = cfg.get("type", "api")
    if ptype == "cli":
        return _call_cli(cfg, prompt)
    if ptype in ("api", "local"):
        return _call_http(cfg, prompt, system, max_tokens)
    raise ExecutorError(f"unknown provider type {ptype!r} in {cfg.get('_source_file')}", retryable=False)


def _call_http(cfg: dict, prompt: str, system: str, max_tokens: int) -> str:
    provider = cfg.get("provider", "")
    model = cfg.get("model", "")

    try:
        api_key = resolve_api_key(cfg)
    except Exception as exc:
        # Missing credentials for THIS provider - not fatal to the chain,
        # the caller moves on to the next one.
        raise ExecutorError(f"{provider} credentials not available: {exc}", retryable=False) from exc

    body, headers, url = _build_request(provider, cfg.get("base_url", ""), model, api_key, prompt, system, max_tokens)

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=cfg.get("request_timeout_s", 120))
    except requests.RequestException as exc:
        # exc's message can carry the full URL, which for gemini includes the
        # API key as a query param - redact it before it enters the error/log
        # stream (see _redact).
        raise ExecutorError(f"{provider} request failed: {_redact(str(exc), api_key)}") from exc

    if resp.status_code == 429:
        raise ExecutorError(f"{provider} rate limited", status_code=429)
    if not resp.ok:
        raise ExecutorError(
            f"{provider} returned {resp.status_code}: {_redact(resp.text[:300], api_key)}",
            retryable=resp.status_code >= 500, status_code=resp.status_code,
        )

    return _parse_response(provider, resp.json())


def _build_request(provider: str, base_url: str, model: str, api_key: Optional[str], prompt: str, system: str, max_tokens: int):
    if provider == "anthropic":
        url = (base_url or "https://api.anthropic.com/v1") + "/messages"
        headers = {"content-type": "application/json", "x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
    elif provider == "gemini":
        base = base_url or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{model}:generateContent?key={api_key or ''}"
        headers = {"content-type": "application/json"}
        body = {"system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}}
    else:  # openai-compatible: openai, grok, deepseek, local servers, gateways
        url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    return body, headers, url


def _parse_response(provider: str, data: dict) -> str:
    if provider == "anthropic":
        return "".join(c.get("text", "") for c in data.get("content", []))
    if provider == "gemini":
        candidates = data.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        return "".join(p.get("text", "") for p in parts)
    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def _call_cli(cfg: dict, prompt: str) -> str:
    cmd = cfg.get("command")
    if not cmd:
        raise ExecutorError(f"cli provider in {cfg.get('_source_file')} is missing 'command'", retryable=False)
    args = list(cfg.get("args", [])) + [prompt]

    # On Windows the provider is launched through the shell (shell=True), so a
    # missing binary surfaces as a nonzero returncode with a shell error in
    # stderr rather than the OSError the non-shell path raises on POSIX. Both
    # mean the same thing operationally - the command cannot be started - so
    # catch it up front and label it non-retryable (retrying an unfindable
    # command never fixes it), matching what the OSError path already does.
    if shutil.which(cmd) is None:
        raise ExecutorError(f"cli provider command {cmd!r} not found on PATH", retryable=False)

    try:
        result = subprocess.run(
            [cmd] + args, capture_output=True, text=True,
            timeout=cfg.get("timeout_s", 180), shell=(os.name == "nt"),
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutorError(f"cli provider {cmd!r} timed out") from exc
    except OSError as exc:
        raise ExecutorError(f"cli provider {cmd!r} could not be started: {exc}", retryable=False) from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise ExecutorError(f"cli provider {cmd!r} exited {result.returncode}: {result.stderr[:200]}")
    return result.stdout.strip()


class LlmChainExecutor:
    """Tries each configured provider in priority order and uses the first
    one that returns text. Same output shape as LlmScriptExecutor
    (script.txt + {"script": ...}), plus which provider actually served the
    request, so it's a drop-in alternative for the 'script' step."""

    name = "llm_chain"

    def run(self, context: StepContext) -> ExecutorOutput:
        cfg = context.step_config
        provider_dir = cfg.get("provider_dir", "./providers")
        providers = discover_providers(provider_dir)
        if not providers:
            raise ExecutorError(f"no enabled provider configs found in {provider_dir!r}", retryable=False)

        system = cfg.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        max_tokens = cfg.get("max_tokens", 2000)

        failures = []
        for provider_cfg in providers:
            label = provider_cfg.get("provider") or provider_cfg.get("command", "?")
            try:
                text = call_provider(provider_cfg, context.seed_topic, system, max_tokens)
            except ExecutorError as exc:
                logger.warning("chain: %s failed: %s", label, exc)
                failures.append(f"{label}: {exc}")
                continue

            if text.strip():
                out_path = str(Path(context.workdir) / "script.txt")
                Path(out_path).write_text(text.strip())
                return ExecutorOutput(output_ref=out_path, data={"script": text.strip(), "provider_used": label})
            failures.append(f"{label}: returned empty text")

        raise ExecutorError(f"every provider in the chain failed: {'; '.join(failures)}")
