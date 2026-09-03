"""Real HTTP round trip for scripts/refresh_tokens.py's Instagram refresh
against a local mock Graph API, plus vault-persistence proof. Verifies the
token is actually re-issued and written back, and that a failure/5xx is
reported rather than swallowed.
"""
from __future__ import annotations

import json
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

import scripts.refresh_tokens as rt


class _MockGraphServer:
    def __init__(self, new_token: str, status: int = 200, expires_in: int = 5184000):
        outer = self
        self.received_params: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    parsed = urlparse(self.path)
                    outer.received_params.append({k: v[0] for k, v in parse_qs(parsed.query).items()})
                    body = json.dumps({"access_token": outer.new_token, "expires_in": outer.expires_in}).encode()
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                except Exception as exc:
                    print(f"handler error: {exc!r}")

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self.new_token = new_token
        self.status = status
        self.expires_in = expires_in
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._wait_ready()

    def _wait_ready(self):
        import socket
        import time
        host, port = self._server.server_address
        for _ in range(50):
            try:
                with socket.create_connection((host, port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mock server did not start listening")

    def stop(self):
        self._server.shutdown()
        try:
            self._server.server_close()
        except Exception:
            pass
        self._thread.join(timeout=2)


def test_instagram_refresh_hits_graph_and_persists_to_vault():
    server = _MockGraphServer(new_token="tok-2", expires_in=60 * 24 * 3600)
    old = rt.GRAPH_BASE
    rt.GRAPH_BASE = server.base_url
    stored: dict[str, str] = {"instagram_access_token": "tok-1", "instagram_ig_user_id": "user_123"}

    class FakeVault:
        def get(self, key):
            if key not in stored:
                raise KeyError(key)
            return stored[key]

        def set(self, key, value):
            stored[key] = value

    try:
        with mock.patch("scripts.refresh_tokens._vault", return_value=FakeVault()):
            code = rt.refresh_instagram()
    finally:
        rt.GRAPH_BASE = old
        server.stop()

    assert code == 0
    # Graph API was hit with grant_type=ig_exchange_token
    assert server.received_params[0]["grant_type"] == "ig_exchange_token"
    # new token written back to the vault
    assert stored["instagram_access_token"] == "tok-2"
    print("PASS test_instagram_refresh_hits_graph_and_persists_to_vault")


def test_instagram_refresh_reports_failure_on_5xx():
    server = _MockGraphServer(new_token="", status=500)
    old = rt.GRAPH_BASE
    rt.GRAPH_BASE = server.base_url

    class FakeVault:
        def get(self, key):
            return "tok-1" if key == "instagram_access_token" else "user_123"

        def set(self, key, value):
            raise AssertionError("should not persist on a failed refresh")

    try:
        with mock.patch("scripts.refresh_tokens._vault", return_value=FakeVault()):
            code = rt.refresh_instagram()
    finally:
        rt.GRAPH_BASE = old
        server.stop()

    assert code == 3
    print("PASS test_instagram_refresh_reports_failure_on_5xx")


if __name__ == "__main__":
    test_instagram_refresh_hits_graph_and_persists_to_vault()
    test_instagram_refresh_reports_failure_on_5xx()
    print("\nall refresh_tokens tests passed")
