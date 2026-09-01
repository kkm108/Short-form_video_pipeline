"""Real HTTP round trips for the media_chain executor against local mock
servers, proving per-artifact fallback actually works: if the Gemini (first)
provider fails images or voiceover, the chain falls through to OpenAI for that
artifact. One mock per vendor, since the two hit different paths/servers.

Same style as test_llm_chain.py / test_gemini_media.py: real request bodies,
real response parsing, real files written to disk.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from executors.base import ExecutorError, StepContext
from executors.media_chain import MediaChainExecutor, discover_media_providers


class _MockVendorServer:
    """One server per vendor. Gemini: JSON with inlineData for image/audio.
    OpenAI: JSON for /images/generations, RAW bytes for /audio/speech.
    `fail_paths` are substrings that cause a 429 (so the chain falls back)."""

    def __init__(self, vendor: str, image_b64: str, audio_bytes: bytes, fail_paths: list[str] | None = None):
        self.vendor = vendor
        self.image_b64 = image_b64
        self.audio_bytes = audio_bytes
        self.fail_paths = fail_paths or []
        self.requests_seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests_seen.append({"path": self.path, "headers": dict(self.headers), "body": body})

                if any(fp in self.path for fp in outer.fail_paths):
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "rate limited"}).encode())
                    return

                self.send_response(200)
                if "/audio/speech" in self.path:  # OpenAI TTS -> raw audio bytes
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(outer.audio_bytes)
                    return

                # Everything else -> JSON with that vendor's image or tts shape
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if "generateContent" in self.path:  # Gemini
                    if "tts" in self.path:
                        part = {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(outer.audio_bytes or b"GEMWAV").decode()}}
                    else:
                        part = {"inlineData": {"mimeType": "image/png", "data": outer.image_b64}}
                    payload = {"candidates": [{"content": {"parts": [part]}}]}
                else:  # OpenAI images/generations
                    payload = {"data": [{"b64_json": outer.image_b64}]}
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


def _img_bytes(tag: str) -> bytes:
    return b"\x89PNG" + tag.encode()


def _ctx(workdir: str, server_cfgs: list[dict]) -> StepContext:
    return StepContext(
        run_id="mc1", seed_topic="my topic", platforms=["youtube"],
        step_config={"provider_dir": _make_provider_dir(server_cfgs)},
        workdir=workdir,
        upstream={"script": type("O", (), {"data": {"script": "Line one.\nLine two."}})()},
    )


def _make_provider_dir(cfgs: list[dict]) -> str:
    d = tempfile.mkdtemp()
    for i, cfg in enumerate(cfgs):
        (Path(d) / f"{i:03d}-{cfg['_name']}.json").write_text(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")}))
    return d


def test_chain_uses_first_provider_when_both_succeed():
    gemi = _MockVendorServer("gemini", base64.b64encode(_img_bytes("g")).decode(), b"GEMWAV")
    oai = _MockVendorServer("openai", base64.b64encode(_img_bytes("o")).decode(), b"OAIWAV")
    os.environ["TEST_GEM_MEDIA_KEY"] = "k"
    os.environ["TEST_OPENAI_MEDIA_KEY"] = "k2"
    try:
        with tempfile.TemporaryDirectory() as workdir:
            output = MediaChainExecutor().run(_ctx(workdir, [
                {"_name": "gem", "vendor": "gemini", "api_key_ref": "test_gem_media_key", "base_url": gemi.base_url, "n_images": 2, "images": True, "tts": True},
                {"_name": "oai", "vendor": "openai", "api_key_ref": "test_openai_media_key", "base_url": oai.base_url, "n_images": 2, "images": True, "tts": True},
            ]))

            assert output.data["provider"] == "images=gemini/voiceover=gemini"
            assert len(output.data["clip_paths"]) == 2
            assert Path(output.data["voiceover_path"]).exists()
            # OpenAI never called when gemini works
            assert oai.requests_seen == []
            print("PASS test_chain_uses_first_provider_when_both_succeed")
    finally:
        os.environ.pop("TEST_GEM_MEDIA_KEY", None)
        os.environ.pop("TEST_OPENAI_MEDIA_KEY", None)
        gemi.stop()
        oai.stop()


def test_chain_falls_back_per_artifact_when_gemini_images_fail():
    gemi = _MockVendorServer("gemini", "", b"", fail_paths=["flash-image"])  # image path 429s, voiceover ok
    gemi_image_b64 = base64.b64encode(_img_bytes("o")).decode()
    oai = _MockVendorServer("openai", gemi_image_b64, b"OAIWAV")
    os.environ["TEST_GEM_MEDIA_KEY"] = "k"
    os.environ["TEST_OPENAI_MEDIA_KEY"] = "k2"
    try:
        with tempfile.TemporaryDirectory() as workdir:
            output = MediaChainExecutor().run(_ctx(workdir, [
                {"_name": "gem", "vendor": "gemini", "api_key_ref": "test_gem_media_key", "base_url": gemi.base_url, "n_images": 2, "images": True, "tts": True},
                {"_name": "oai", "vendor": "openai", "api_key_ref": "test_openai_media_key", "base_url": oai.base_url, "n_images": 2, "images": True, "tts": True},
            ]))

            # images came from openai, voiceover from gemini
            assert output.data["provider"] == "images=openai/voiceover=gemini"
            assert len(output.data["clip_paths"]) == 2
            assert Path(output.data["clip_paths"][0]).exists()
            assert Path(output.data["voiceover_path"]).exists()
            # gemini got 1 image attempt (frame 1 429s -> falls back) + succeeded voiceover
            gem_calls = [r["path"] for r in gemi.requests_seen]
            assert sum("flash-image" in p for p in gem_calls) == 1
            assert sum("tts" in p for p in gem_calls) == 1
            # openai produced the 2 fallback images
            oai_calls = [r["path"] for r in oai.requests_seen]
            assert sum("images/generations" in p for p in oai_calls) == 2
            print("PASS test_chain_falls_back_per_artifact_when_gemini_images_fail")
    finally:
        os.environ.pop("TEST_GEM_MEDIA_KEY", None)
        os.environ.pop("TEST_OPENAI_MEDIA_KEY", None)
        gemi.stop()
        oai.stop()


def test_chain_skips_provider_without_key_without_crashing():
    gemi = _MockVendorServer("gemini", base64.b64encode(_img_bytes("g")).decode(), b"GEMWAV")
    os.environ.pop("TEST_OPENAI_MEDIA_KEY", None)  # openai provider has no creds
    os.environ["TEST_GEM_MEDIA_KEY"] = "k"
    try:
        with tempfile.TemporaryDirectory() as workdir:
            output = MediaChainExecutor().run(_ctx(workdir, [
                {"_name": "gem", "vendor": "gemini", "api_key_ref": "test_gem_media_key", "base_url": gemi.base_url, "n_images": 1, "images": True, "tts": True},
                {"_name": "oai", "vendor": "openai", "api_key_ref": "test_openai_media_key", "base_url": "http://127.0.0.1:1", "n_images": 1, "images": True, "tts": True},
            ]))
            assert output.data["provider"] == "images=gemini/voiceover=gemini"
            print("PASS test_chain_skips_provider_without_key_without_crashing")
    finally:
        os.environ.pop("TEST_GEM_MEDIA_KEY", None)
        gemi.stop()


def test_chain_raises_non_retryable_when_no_provider_succeeds_images():
    gemi = _MockVendorServer("gemini", "", b"", fail_paths=["flash-image", "tts"])
    os.environ["TEST_GEM_MEDIA_KEY"] = "k"
    try:
        with tempfile.TemporaryDirectory() as workdir:
            try:
                MediaChainExecutor().run(_ctx(workdir, [
                    {"_name": "gem", "vendor": "gemini", "api_key_ref": "test_gem_media_key", "base_url": gemi.base_url, "n_images": 1, "images": True, "tts": False},
                ]))
                assert False, "expected ExecutorError"
            except ExecutorError as exc:
                assert exc.retryable is False
                assert "failed to produce images" in str(exc)
                print("PASS test_chain_raises_non_retryable_when_no_provider_succeeds_images")
    finally:
        os.environ.pop("TEST_GEM_MEDIA_KEY", None)
        gemi.stop()


def test_discover_media_providers_filters_and_sorts():
    d = tempfile.mkdtemp()
    try:
        (Path(d) / "010-openai.json").write_text(json.dumps({"vendor": "openai", "enabled": True, "api_key_ref": "x"}))
        (Path(d) / "020-cli.json").write_text(json.dumps({"vendor": "unknown", "enabled": True}))  # not media-capable
        (Path(d) / "000-gemini.json").write_text(json.dumps({"vendor": "gemini", "enabled": False}))
        (Path(d) / "005-gemini.json").write_text(json.dumps({"vendor": "gemini", "enabled": True}))
        found = discover_media_providers(d)
        assert [p["vendor"] for p in found] == ["gemini", "openai"]
        print("PASS test_discover_media_providers_filters_and_sorts")
    finally:
        pass


if __name__ == "__main__":
    test_chain_uses_first_provider_when_both_succeed()
    test_chain_falls_back_per_artifact_when_gemini_images_fail()
    test_chain_skips_provider_without_key_without_crashing()
    test_chain_raises_non_retryable_when_no_provider_succeeds_images()
    test_discover_media_providers_filters_and_sorts()
    print("\nall media chain tests passed")
