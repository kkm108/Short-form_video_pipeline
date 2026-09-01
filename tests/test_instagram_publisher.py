"""End-to-end test for the Instagram publisher: a real HTTP round trip
(container create -> poll -> publish) against a local mock Graph API, plus a
pure unit test for the caption-building logic that doesn't need HTTP at all.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

from executors.base import ExecutorError
from executors.publishers.base import PublishMetadata
from executors.publishers.instagram import InstagramPublisher, build_caption


class _MockGraphApi:
    """Routes by path shape rather than an exact match, since the mock needs
    to distinguish '/{ig_user_id}/media', '/{container_id}' (poll), and
    '/{ig_user_id}/media_publish' the same way the real Graph API's URL
    structure does."""

    def __init__(self, container_status_sequence: list[str]):
        self.container_status_sequence = list(container_status_sequence)
        self.requests_seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = parse_qs(self.rfile.read(length).decode())
                path = urlparse(self.path).path
                outer.requests_seen.append({"method": "POST", "path": path, "body": body})

                if path.endswith("/media_publish"):
                    self._respond(200, {"id": "media_999"})
                elif path.endswith("/media"):
                    self._respond(200, {"id": "container_123"})
                else:
                    self._respond(404, {"error": "unknown path"})

            def do_GET(self):
                path = urlparse(self.path).path
                outer.requests_seen.append({"method": "GET", "path": path})
                status = outer.container_status_sequence.pop(0) if outer.container_status_sequence else "FINISHED"
                self._respond(200, {"status_code": status})

            def _respond(self, code, payload):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


def _fake_credentials_provider(platform: str) -> dict:
    return {"ig_user_id": "17800000000000", "access_token": "test-ig-token"}


def test_build_caption_joins_description_and_hashtags():
    metadata = PublishMetadata(title="t", description="A short film.", hashtags=["shorts", "faceless"])
    assert build_caption(metadata) == "A short film.\n\n#shorts #faceless"
    print("PASS test_build_caption_joins_description_and_hashtags")


def test_build_caption_respects_2200_char_limit():
    metadata = PublishMetadata(title="t", description="x" * 3000, hashtags=[])
    assert len(build_caption(metadata)) == 2200
    print("PASS test_build_caption_respects_2200_char_limit")


def test_publish_full_flow_container_then_publish():
    server = _MockGraphApi(container_status_sequence=["FINISHED"])
    try:
        publisher = InstagramPublisher(_fake_credentials_provider, graph_base=server.base_url, poll_interval_s=0.01)
        metadata = PublishMetadata(title="t", description="desc", hashtags=["a", "b"])
        result = publisher.publish("https://example.com/video.mp4", metadata)

        assert result.remote_id == "media_999"
        methods_paths = [(r["method"], r["path"]) for r in server.requests_seen]
        assert ("POST", "/17800000000000/media") in methods_paths
        assert ("GET", "/container_123") in methods_paths
        assert ("POST", "/17800000000000/media_publish") in methods_paths
        print("PASS test_publish_full_flow_container_then_publish")
    finally:
        server.stop()


def test_publish_polls_until_processing_finishes():
    server = _MockGraphApi(container_status_sequence=["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    try:
        publisher = InstagramPublisher(_fake_credentials_provider, graph_base=server.base_url, poll_interval_s=0.01)
        metadata = PublishMetadata(title="t", description="desc", hashtags=[])
        publisher.publish("https://example.com/video.mp4", metadata)

        get_requests = [r for r in server.requests_seen if r["method"] == "GET"]
        assert len(get_requests) == 3  # polled twice before seeing FINISHED
        print("PASS test_publish_polls_until_processing_finishes")
    finally:
        server.stop()


def test_publish_raises_retryable_on_container_error():
    server = _MockGraphApi(container_status_sequence=["ERROR"])
    try:
        publisher = InstagramPublisher(_fake_credentials_provider, graph_base=server.base_url, poll_interval_s=0.01)
        metadata = PublishMetadata(title="t", description="desc", hashtags=[])
        try:
            publisher.publish("https://example.com/video.mp4", metadata)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.retryable is False  # a failed encode won't fix itself on retry
            print("PASS test_publish_raises_retryable_on_container_error")
    finally:
        server.stop()


def test_publish_wraps_credentials_provider_failure_cleanly():
    def failing_provider(platform: str):
        raise KeyError("instagram_access_token")  # what Vault.get() raises today for a missing token

    publisher = InstagramPublisher(failing_provider, graph_base="http://127.0.0.1:1")  # never reached
    metadata = PublishMetadata(title="t", description="desc", hashtags=[])
    try:
        publisher.publish("https://example.com/video.mp4", metadata)
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        assert "KeyError" in str(exc)
        print("PASS test_publish_wraps_credentials_provider_failure_cleanly")


if __name__ == "__main__":
    test_build_caption_joins_description_and_hashtags()
    test_build_caption_respects_2200_char_limit()
    test_publish_full_flow_container_then_publish()
    test_publish_polls_until_processing_finishes()
    test_publish_raises_retryable_on_container_error()
    test_publish_wraps_credentials_provider_failure_cleanly()
    print("\nall instagram publisher tests passed")
