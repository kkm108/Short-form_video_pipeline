"""Gemini-native media generation (single-provider convenience executor):
generates the images and voiceover for the assembly step by calling Gemini
directly over HTTP - no browser, no session profile. This is the browser-free
media_generation step for a Google/Gemini setup, and it is also one provider in
the multi-vendor media chain (see executors/media_chain.py and
executors/media_providers.py for the shared per-vendor logic).

The heavy lifting (request bodies, response parsing, key redaction, retryable
classification) lives in executors/media_providers.py; this class just adapts
it to the StepExecutor contract and the output schema the ffmpeg assembly step
consumes: data["voiceover_path"] and data["clip_paths"].
"""
from __future__ import annotations

from pathlib import Path

from executors.base import ExecutorError, ExecutorOutput, StepContext
from executors.llm_chain import resolve_api_key
from executors.media_providers import gemini_images, gemini_voiceover

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiMediaExecutor:
    """Generates the media bundle (clip images + voiceover) from a manifest's
    script via the Gemini API."""

    name = "gemini_media"

    def run(self, context: StepContext) -> ExecutorOutput:
        cfg = context.step_config
        workdir = Path(context.workdir)
        base_url = cfg.get("base_url", _GEMINI_BASE)

        script_step = context.upstream.get("script")
        script = script_step.data.get("script", "") if script_step else ""
        if not script.strip():
            raise ExecutorError("gemini_media needs a non-empty script from a preceding 'script' step", retryable=False)

        try:
            api_key = resolve_api_key(cfg)
        except Exception as exc:
            raise ExecutorError(f"gemini_media credential not available: {exc}", retryable=False) from exc
        if not api_key:
            raise ExecutorError(
                "gemini_media needs a Gemini API key - set the vault key named by api_key_ref "
                "(e.g. export GEMINI_API_KEY or `keyring set faceless-pipeline gemini_api_key`)",
                retryable=False,
            )

        images = gemini_images(cfg, script, context.seed_topic, api_key, base_url, workdir)
        voiceover = gemini_voiceover(cfg, script, api_key, base_url, workdir)

        return ExecutorOutput(
            output_ref=str(workdir),
            data={
                "voiceover_path": voiceover,
                "clip_paths": images,
                "music_path": None,
                "provider": "gemini",
            },
        )
