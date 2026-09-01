"""Real HTTP round trips for the gemini_media executor against a local mock
server - proves the request bodies (IMAGE modality for frames, AUDIO +
speechConfig for the voiceover) and the response parsing (base64 inlineData ->
files on disk) are actually correct, not just read through. Same approach as
test_llm_chain.py's mock-server tests.
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
from executors.gemini_media import GeminiMediaExecutor


class _MockMediaServer:
    """Serves generated image + audio payloads. The executor builds the model
    URL as <base>/models/<model>:generateContent, so we branch on the model
    name in the path: '*flash-image' -> image inlineData, '*tts*' -> audio."""

    def __init__(self, image_b64: str, audio_b64: str, status: int = 200):
        self.image_b64 = image_b64
        self.audio_b64 = audio_b64
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
                if outer.status == 200:
                    if "tts" in self.path:
                        payload = {"candidates": [{"content": {"parts": [{"inlineData": {
                            "mimeType": "audio/wav", "data": outer.audio_b64}}]}}]}
                    elif "flash-image" in self.path:
                        payload = {"candidates": [{"content": {"parts": [{"inlineData": {
                            "mimeType": "image/png", "data": outer.image_b64}}]}}]}
                    else:
                        payload = {"candidates": []}
                    self.wfile.write(json.dumps(payload).encode())
                else:
                    self.wfile.write(json.dumps({"error": "boom"}).encode())

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


def _set_env(key: str, value: str):
    os.environ[key] = value


def _restore_env(key: str, old):
    if old is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = old


def _context(server: "_MockMediaServer" | None, workdir: str, *, n_images: int = 2) -> StepContext:
    cfg: dict = {
        "api_key_ref": "test_gem_media_key",
        "n_images": n_images,
        "image_model": "gemini-3.1-flash-image",
        "tts_model": "gemini-3.1-flash-tts-preview",
        "voice": "Kore",
    }
    if server is not None:
        cfg["base_url"] = server.base_url
        cfg["aspect_ratio"] = "9:16"
        cfg["image_size"] = "1K"
    return StepContext(
        run_id="gm1", seed_topic="my topic", platforms=["youtube"],
        step_config=cfg, workdir=workdir,
        upstream={"script": type("O", (), {"data": {"script": "Line one.\nLine two."}})()},
    )


def test_gemini_media_writes_images_and_voiceover_from_mock():
    image_bytes = b"\x89PNG\r\n\x1a\n-fake-image-bytes-"
    audio_bytes = b"RIFF\x00fake-wav-bytes"
    server = _MockMediaServer(
        image_b64=base64.b64encode(image_bytes).decode(),
        audio_b64=base64.b64encode(audio_bytes).decode(),
    )
    old = os.environ.get("TEST_GEM_MEDIA_KEY")
    _set_env("TEST_GEM_MEDIA_KEY", "gm-key-123")
    try:
        # tempdir must stay alive across the run AND the assertions, so it lives
        # here in the test body, not in a helper that returns a path (returning
        # a path from inside `with TemporaryDirectory()` deletes the dir first).
        with tempfile.TemporaryDirectory() as workdir:
            output = GeminiMediaExecutor().run(_context(server, workdir, n_images=2))

            # Output contract the assembly step consumes
            assert output.data["voiceover_path"].endswith("voiceover.wav")
            assert len(output.data["clip_paths"]) == 2
            for p in output.data["clip_paths"]:
                assert Path(p).exists()

            # Files decoded correctly from the mock's base64
            assert Path(output.data["voiceover_path"]).read_bytes() == audio_bytes
            for p in output.data["clip_paths"]:
                assert Path(p).read_bytes() == image_bytes

            # Everything went to the workdir
            assert output.data["voiceover_path"].startswith(workdir)

            # Request bodies carry the right shape
            image_reqs = [r for r in server.requests_seen if "flash-image" in r["path"]]
            assert len(image_reqs) == 2
            for r in image_reqs:
                assert r["body"]["generationConfig"]["responseModalities"] == ["IMAGE"]
                assert r["body"]["generationConfig"]["responseFormat"]["image"]["aspectRatio"] == "9:16"
                assert r["body"]["generationConfig"]["imageSize"] == "1K"
                assert r["headers"]["x-goog-api-key"] == "gm-key-123"

            tts_reqs = [r for r in server.requests_seen if "tts" in r["path"]]
            assert len(tts_reqs) == 1
            assert tts_reqs[0]["body"]["generationConfig"]["responseModalities"] == ["AUDIO"]
            assert tts_reqs[0]["body"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
            assert tts_reqs[0]["body"]["contents"][0]["parts"][0]["text"] == "Line one.\nLine two."

            print("PASS test_gemini_media_writes_images_and_voiceover_from_mock")
    finally:
        _restore_env("TEST_GEM_MEDIA_KEY", old)
        server.stop()


def test_gemini_media_429_is_retryable():
    server = _MockMediaServer(image_b64="", audio_b64="", status=429)
    old = os.environ.get("TEST_GEM_MEDIA_KEY")
    _set_env("TEST_GEM_MEDIA_KEY", "k")
    try:
        with tempfile.TemporaryDirectory() as workdir:
            ctx = StepContext(
                run_id="gm429", seed_topic="t", platforms=["youtube"],
                step_config={"base_url": server.base_url, "api_key_ref": "test_gem_media_key", "n_images": 1, "image_model": "gemini-3.1-flash-image"},
                workdir=workdir,
                upstream={"script": type("O", (), {"data": {"script": "s"}})()},
            )
            try:
                GeminiMediaExecutor().run(ctx)
                assert False, "expected ExecutorError"
            except ExecutorError as exc:
                assert exc.status_code == 429
                assert exc.retryable is True
                print("PASS test_gemini_media_429_is_retryable")
    finally:
        _restore_env("TEST_GEM_MEDIA_KEY", old)
        server.stop()


def test_gemini_media_missing_key_is_non_retryable():
    old = os.environ.get("TEST_GEM_MEDIA_KEY")
    if old is not None:
        os.environ.pop("TEST_GEM_MEDIA_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as workdir:
            ctx = _context(None, workdir, n_images=1)
            try:
                GeminiMediaExecutor().run(ctx)
                assert False, "expected ExecutorError"
            except ExecutorError as exc:
                assert exc.retryable is False
                print("PASS test_gemini_media_missing_key_is_non_retryable")
    finally:
        _restore_env("TEST_GEM_MEDIA_KEY", old)


def test_gemini_media_requires_upstream_script():
    with tempfile.TemporaryDirectory() as workdir:
        context = StepContext(
            run_id="gmnoscript", seed_topic="t", platforms=["youtube"],
            step_config={"api_key_ref": "test_gem_media_key"}, workdir=workdir, upstream={},
        )
        try:
            GeminiMediaExecutor().run(context)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.retryable is False
            print("PASS test_gemini_media_requires_upstream_script")


if __name__ == "__main__":
    test_gemini_media_writes_images_and_voiceover_from_mock()
    test_gemini_media_429_is_retryable()
    test_gemini_media_missing_key_is_non_retryable()
    test_gemini_media_requires_upstream_script()
    print("\nall gemini media tests passed")
