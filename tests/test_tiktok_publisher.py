"""End-to-end test for the TikTok publisher: init -> PUT upload, against two
local mock servers (Content Posting API commonly returns a separate signed
upload_url, so the test mirrors that two-host shape rather than assuming
they're the same server).
"""
from __future__ import annotations

import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from executors.base import ExecutorError
from executors.publishers.base import PublishMetadata
from executors.publishers.tiktok import TikTokPublisher, build_init_body


class _MockTikTokServers:
    def __init__(self, init_status: int = 200):
        self.init_requests: list[dict] = []
        self.upload_requests: list[dict] = []
        outer = self

        class InitHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.init_requests.append({"auth": self.headers.get("Authorization"), "body": body})
                self.send_response(init_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = (
                    {"data": {"upload_url": outer.upload_url, "publish_id": "pub_555"}}
                    if init_status == 200
                    else {"error": "bad request"}
                )
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        class UploadHandler(BaseHTTPRequestHandler):
            def do_PUT(self):
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                outer.upload_requests.append({"content_range": self.headers.get("Content-Range"), "bytes": len(data)})
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self._init_server = ThreadingHTTPServer(("127.0.0.1", 0), InitHandler)
        self._upload_server = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
        self.init_base_url = f"http://127.0.0.1:{self._init_server.server_port}"
        self.upload_url = f"http://127.0.0.1:{self._upload_server.server_port}/upload"
        Thread(target=self._init_server.serve_forever, daemon=True).start()
        Thread(target=self._upload_server.serve_forever, daemon=True).start()

    def stop(self):
        self._init_server.shutdown()
        self._upload_server.shutdown()


def _fake_credentials_provider(platform: str) -> dict:
    return {"access_token": "test-tt-token"}


def test_build_init_body_truncates_title_and_sizes_single_chunk():
    metadata = PublishMetadata(title="x" * 200, description="d", hashtags=[])
    body = build_init_body(metadata, video_size=4096)
    assert len(body["post_info"]["title"]) == 150
    assert body["source_info"]["video_size"] == 4096
    assert body["source_info"]["total_chunk_count"] == 1
    print("PASS test_build_init_body_truncates_title_and_sizes_single_chunk")


def test_publish_full_flow_init_then_upload():
    servers = _MockTikTokServers()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "video.mp4"
            video_bytes = b"\x00\x01fake-mp4-bytes" * 100
            video_path.write_bytes(video_bytes)

            publisher = TikTokPublisher(_fake_credentials_provider, api_base=servers.init_base_url)
            metadata = PublishMetadata(title="A short film", description="d", hashtags=[])
            result = publisher.publish(str(video_path), metadata)

            assert result.remote_id == "pub_555"
            assert servers.init_requests[0]["auth"] == "Bearer test-tt-token"
            assert servers.upload_requests[0]["bytes"] == len(video_bytes)
            assert servers.upload_requests[0]["content_range"] == f"bytes 0-{len(video_bytes) - 1}/{len(video_bytes)}"
            print("PASS test_publish_full_flow_init_then_upload")
    finally:
        servers.stop()


def test_publish_raises_on_init_failure():
    servers = _MockTikTokServers(init_status=400)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "video.mp4"
            video_path.write_bytes(b"x" * 10)
            publisher = TikTokPublisher(_fake_credentials_provider, api_base=servers.init_base_url)
            metadata = PublishMetadata(title="t", description="d", hashtags=[])
            try:
                publisher.publish(str(video_path), metadata)
                assert False, "expected ExecutorError"
            except ExecutorError as exc:
                assert exc.retryable is False  # 400-class: retrying the same bad request won't help
                print("PASS test_publish_raises_on_init_failure")
    finally:
        servers.stop()


def test_publish_wraps_credentials_provider_failure_cleanly():
    def failing_provider(platform: str):
        raise KeyError("tiktok_access_token")  # what Vault.get() raises today for a missing token

    with tempfile.TemporaryDirectory() as tmp:
        video_path = Path(tmp) / "video.mp4"
        video_path.write_bytes(b"x" * 10)
        publisher = TikTokPublisher(failing_provider, api_base="http://127.0.0.1:1")  # never reached
        metadata = PublishMetadata(title="t", description="d", hashtags=[])
        try:
            publisher.publish(str(video_path), metadata)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.retryable is False
            assert "KeyError" in str(exc)
            print("PASS test_publish_wraps_credentials_provider_failure_cleanly")


if __name__ == "__main__":
    test_build_init_body_truncates_title_and_sizes_single_chunk()
    test_publish_full_flow_init_then_upload()
    test_publish_raises_on_init_failure()
    test_publish_wraps_credentials_provider_failure_cleanly()
    print("\nall tiktok publisher tests passed")
