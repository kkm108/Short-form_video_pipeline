"""Per-vendor media generators (images + voiceover) used by the media chain.
Each function goes through a specific vendor's plain-HTTP API and returns real
artifacts on disk. The point of splitting these out of the executor classes is
that executors/media_chain.py can try vendors in priority order and fall back
per artifact without duplicating the HTTP/parse logic.

API surfaces here were verified against official docs (Sep 2026), not written
from memory (see AGENTS.md):
- Gemini:              gemini-3.1-flash-image (IMAGE modality)
                       and gemini-3.1-flash-tts-preview (AUDIO modality), both
                       POST {base}/models/{model}:generateContent with
                       x-goog-api-key. Base64 inlineData back.
- OpenAI:              POST /v1/images/generations (gpt-image-2, response_format
                       b64_json -> data[0].b64_json) and /v1/audio/speech
                       (gpt-4o-mini-tts, voice + optional instructions, returns
                       raw audio bytes). Authorization: Bearer.

Every failure is ExecutorError with a deliberate retryable= value; the api_key
is redacted from any error/log text (same guard as llm_chain's gemini path).
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Optional

import requests

from executors.base import ExecutorError
from executors.llm_chain import _redact

logger = logging.getLogger("pipeline.media_providers")

# --------------------------------------------------------------------------
# Gemini (used alone by executors/gemini_media.py and as a chain provider)
# --------------------------------------------------------------------------

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
_GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
_GEMINI_VOICE = "Kore"
_IMAGE_MIME_TO_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def gemini_images(cfg: dict, prompt_base: str, topic: str, api_key: str, base_url: str, workdir: Path) -> list[str]:
    model = cfg.get("image_model", _GEMINI_IMAGE_MODEL)
    n = int(cfg.get("n_images", 6))
    prompt_template = cfg.get("image_prompt_template", "Illustrate this short-video narration, frame {i}: {script}")
    aspect = cfg.get("aspect_ratio")
    image_size = cfg.get("image_size")  # 512 / 1K / 2K / 4K

    if n < 1 or n > 20:
        raise ExecutorError(f"gemini n_images={n} out of range [1, 20] (provider config)", retryable=False)

    clip_paths: list[str] = []
    for i in range(1, n + 1):
        prompt = prompt_template.format(i=i, script=prompt_base, topic=topic)
        if not prompt.strip():
            prompt = prompt_base
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        if aspect:
            body["generationConfig"].setdefault("responseFormat", {})["image"] = {"aspectRatio": aspect}
        if image_size:
            body["generationConfig"]["imageSize"] = image_size

        resp = _gemini_post(base_url, model, body, api_key)
        inline = _first_gemini_inline(resp)
        if inline is None:
            raise ExecutorError(f"gemini image call returned no inlineData image (model {model!r})", retryable=False)
        data_b64, mime = inline
        ext = _IMAGE_MIME_TO_EXT.get(mime, ".png")
        out_path = workdir / f"clip_{i:03d}{ext}"
        out_path.write_bytes(base64.b64decode(data_b64))
        clip_paths.append(str(out_path))

    return clip_paths


def gemini_voiceover(cfg: dict, text: str, api_key: str, base_url: str, workdir: Path) -> str:
    model = cfg.get("tts_model", _GEMINI_TTS_MODEL)
    voice = cfg.get("voice", _GEMINI_VOICE)

    body: dict[str, Any] = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }

    resp = _gemini_post(base_url, model, body, api_key)
    inline = _first_gemini_inline(resp)
    if inline is None:
        raise ExecutorError(f"gemini TTS call returned no inlineData audio (model {model!r})", retryable=False)
    data_b64, mime = inline
    ext = ".wav"
    if mime in ("audio/mpeg", "audio/mp3"):
        ext = ".mp3"
    out_path = workdir / f"voiceover{ext}"
    out_path.write_bytes(base64.b64decode(data_b64))
    return str(out_path)


def _gemini_post(base_url: str, model: str, body: dict, api_key: str) -> dict:
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    headers = {"content-type": "application/json", "x-goog-api-key": api_key}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=120)
    except requests.RequestException as exc:
        raise ExecutorError(f"gemini media request failed: {_redact(str(exc), api_key)}") from exc

    if resp.status_code == 429:
        raise ExecutorError("gemini media rate limited", status_code=429)
    if not resp.ok:
        raise ExecutorError(
            f"gemini media returned {resp.status_code}: {_redact(resp.text[:300], api_key)}",
            retryable=resp.status_code >= 500,
            status_code=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ExecutorError(f"gemini media returned non-JSON: {_redact(resp.text[:200], api_key)}", retryable=False) from exc


def _first_gemini_inline(data: dict) -> Optional[tuple[str, str]]:
    candidates = data.get("candidates", [])
    if not candidates or not isinstance(candidates[0], dict):
        return None
    for part in candidates[0].get("content", {}).get("parts", []):
        inline = part.get("inlineData") if isinstance(part, dict) else None
        if inline and inline.get("data"):
            return inline["data"], inline.get("mimeType", "image/png")
    return None


# --------------------------------------------------------------------------
# OpenAI (the second vendor in the media chain)
# --------------------------------------------------------------------------

_OPENAI_BASE = "https://api.openai.com/v1"
_OPENAI_IMAGE_MODEL = "gpt-image-2"
_OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
_OPENAI_VOICE = "coral"
_OPENAI_IMAGE_SIZE = "1024x1536"  # 9:16 portrait, matches short-form output


def openai_images(cfg: dict, prompt_base: str, topic: str, api_key: str, base_url: str, workdir: Path) -> list[str]:
    model = cfg.get("image_model", _OPENAI_IMAGE_MODEL)
    n = int(cfg.get("n_images", 6))
    size = cfg.get("image_size", _OPENAI_IMAGE_SIZE)
    quality = cfg.get("quality", "medium")

    if n < 1 or n > 20:
        raise ExecutorError(f"openai n_images={n} out of range [1, 20] (provider config)", retryable=False)

    prompt_template = cfg.get("image_prompt_template", "Vertical 9:16 keyframe for a short video, frame {i}: {script}. No text overlays.")

    clip_paths: list[str] = []
    for i in range(1, n + 1):
        prompt = prompt_template.format(i=i, script=prompt_base, topic=topic)
        if not prompt.strip():
            prompt = prompt_base
        body = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "b64_json",
        }
        resp_json = _openai_post(base_url, path="/images/generations", body=body, api_key=api_key)
        data = resp_json.get("data") or []
        if not data or not data[0].get("b64_json"):
            raise ExecutorError(f"openai image call returned no b64_json image (model {model!r})", retryable=False)
        out_path = workdir / f"clip_{i:03d}.png"
        out_path.write_bytes(base64.b64decode(data[0]["b64_json"]))
        clip_paths.append(str(out_path))

    return clip_paths


def openai_voiceover(cfg: dict, text: str, api_key: str, base_url: str, workdir: Path) -> str:
    model = cfg.get("tts_model", _OPENAI_TTS_MODEL)
    voice = cfg.get("voice", _OPENAI_VOICE)
    instructions = cfg.get("tts_instructions", "Read the narration naturally and evenly, pausing for breath at line breaks.")

    body: dict[str, Any] = {"model": model, "input": text, "voice": voice}
    if instructions:
        body["instructions"] = instructions
    body["response_format"] = "mp3"

    resp = _openai_raw(base_url, path="/audio/speech", body=body, api_key=api_key)
    out_path = workdir / "voiceover.mp3"
    out_path.write_bytes(resp)
    return str(out_path)


def _openai_post(base_url: str, path: str, body: dict, api_key: str) -> dict:
    resp = _openai_request(base_url, path, body, api_key)
    try:
        return resp.json()
    except ValueError as exc:
        raise ExecutorError(f"openai returned non-JSON: {_redact(resp.text[:200], api_key)}", retryable=False) from exc


def _openai_raw(base_url: str, path: str, body: dict, api_key: str) -> bytes:
    resp = _openai_request(base_url, path, body, api_key)
    return resp.content


def _openai_request(base_url: str, path: str, body: dict, api_key: str) -> requests.Response:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=120)
    except requests.RequestException as exc:
        raise ExecutorError(f"openai media request failed: {_redact(str(exc), api_key)}") from exc

    if resp.status_code == 429:
        raise ExecutorError("openai media rate limited", status_code=429)
    if not resp.ok:
        raise ExecutorError(
            f"openai media returned {resp.status_code}: {_redact(resp.text[:300], api_key)}",
            retryable=resp.status_code >= 500,
            status_code=resp.status_code,
        )
    return resp
