"""Multi-vendor media chain: produces the images and voiceover for the
assembly step, trying each enabled media provider in providers/*.json in
priority order and falling back per artifact, the same way executors/llm_chain
does for the script. So a Google outage or rate-limit can switch images or
narration to OpenAI (and vice versa) instead of halting the run.

Provider configs (also consumed by the single-vendor executor):
    vendor: "gemini" | "openai"   # which handler in media_providers to use
    api_key_ref: <vault key name>  # resolved via the vault -> env var, never literal
    images: true / tts: true       # which artifacts this provider can produce
    image_model / tts_model / voice / ...  # passed through to the handler

The output schema matches what the ffmpeg assembly step consumes:
data["voiceover_path"] and data["clip_paths"]. Only the artifacts that a
provider actually produced are attributed to it (data["provider"] lists both).
If no provider yields an image, or no provider yields narration, the chain
fails with ExecutorError (retryable=False - retrying won't add a provider).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from executors.base import ExecutorError, ExecutorOutput, StepContext
from executors.llm_chain import resolve_api_key
from executors.media_providers import (
    _GEMINI_BASE as GEMINI_BASE,
    _OPENAI_BASE as OPENAI_BASE,
    gemini_images,
    gemini_voiceover,
    openai_images,
    openai_voiceover,
)

logger = logging.getLogger("pipeline.media_chain")

_VENDOR_IMAGES = {"gemini": gemini_images, "openai": openai_images}
_VENDOR_TTS = {"gemini": gemini_voiceover, "openai": openai_voiceover}


def discover_media_providers(provider_dir: str | Path) -> list[dict]:
    """Same discovery/priority rules as llm_chain.discover_providers: numbered
    JSON files, lower priority number first, enabled:false skipped, malformed
    files logged and skipped. Only media-capable providers (vendor in our
    table) are kept."""
    d = Path(provider_dir)
    if not d.exists():
        return []
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            cfg = json.loads(f.read_text(encoding="utf-8-sig"))
            cfg["_priority"] = int(f.stem.split("-")[0])
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.warning("skipping unparseable media provider config %s: %s", f, exc)
            continue
        if not cfg.get("enabled", True):
            continue
        vendor = cfg.get("vendor")
        if vendor not in _VENDOR_IMAGES or vendor not in _VENDOR_TTS:
            continue
        cfg["_source_file"] = str(f)
        out.append(cfg)
    out.sort(key=lambda x: x["_priority"])
    return out


class MediaChainExecutor:
    name = "media_chain"

    def run(self, context: StepContext) -> ExecutorOutput:
        cfg = context.step_config
        workdir = Path(context.workdir)
        provider_dir = cfg.get("provider_dir", "./providers")
        providers = discover_media_providers(provider_dir)
        if not providers:
            raise ExecutorError(f"no enabled media-capable provider configs found in {provider_dir!r}", retryable=False)

        script_step = context.upstream.get("script")
        script = script_step.data.get("script", "") if script_step else ""
        if not script.strip():
            raise ExecutorError("media_chain needs a non-empty script from a preceding 'script' step", retryable=False)

        # Resolve every provider's key up front so a missing credential for one
        # provider is "skip it," not a crash - same spirit as llm_chain.
        resolved: list[tuple[dict, Optional[str]]] = []
        for p in providers:
            ref = p.get("api_key_ref")
            if not ref:
                resolved.append((p, None))
                continue
            try:
                resolved.append((p, resolve_api_key(p)))
            except Exception as exc:
                logger.warning("media chain: skipping %s, unresolved credential: %s", p.get("_source_file"), exc)
                resolved.append((p, None))

        topic = context.seed_topic
        image_failures: list[str] = []
        tts_failures: list[str] = []
        clip_paths: list[str] = []
        voiceover: Optional[str] = None
        image_provider: Optional[str] = None
        tts_provider: Optional[str] = None

        # --- images: first provider that returns them wins ------------------
        for p, key in resolved:
            if not p.get("images", True):
                continue
            label = p.get("vendor") or p.get("_source_file", "?")
            if not key:
                image_failures.append(f"{label}: no credential")
                continue
            try:
                clip_paths = _VENDOR_IMAGES[p["vendor"]](
                    p, script, topic, key, _base_url(p), workdir
                )
                image_provider = label
                break
            except ExecutorError as exc:
                logger.warning("media chain: %s images failed: %s", label, exc)
                image_failures.append(f"{label}: {exc}")
                continue

        # --- voiceover: first provider that returns it wins -----------------
        for p, key in resolved:
            if not p.get("tts", True):
                continue
            label = p.get("vendor") or p.get("_source_file", "?")
            if not key:
                tts_failures.append(f"{label}: no credential")
                continue
            try:
                voiceover = _VENDOR_TTS[p["vendor"]](p, script, key, _base_url(p), workdir)
                tts_provider = label
                break
            except ExecutorError as exc:
                logger.warning("media chain: %s voiceover failed: %s", label, exc)
                tts_failures.append(f"{label}: {exc}")
                continue

        if not clip_paths:
            raise ExecutorError(f"every media provider failed to produce images: {'; '.join(image_failures)}", retryable=False)
        if not voiceover:
            raise ExecutorError(f"every media provider failed to produce a voiceover: {'; '.join(tts_failures)}", retryable=False)

        return ExecutorOutput(
            output_ref=str(workdir),
            data={
                "voiceover_path": voiceover,
                "clip_paths": clip_paths,
                "music_path": None,
                "provider": f"images={image_provider}/voiceover={tts_provider}",
            },
        )


def _base_url(p: dict) -> str:
    if p.get("base_url"):
        return p["base_url"]
    if p.get("vendor") == "openai":
        return OPENAI_BASE
    return GEMINI_BASE
