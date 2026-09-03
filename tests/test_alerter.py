"""Real HTTP round trips for alerting.alerter against a local mock server,
proving an alert is actually dispatched (and a failed delivery reports back)
rather than just logged. Same style as the other mock-server tests here.
"""
from __future__ import annotations

import json
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from alerting.alerter import _build_payload, send_alert


class _MockAlertServer:
    def __init__(self, status: int = 200):
        outer = self
        self.received: list[str] = []
        self.status = status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                outer.received.append(raw.decode())
                self.send_response(outer.status)
                self.end_headers()
                if outer.status >= 500:
                    self.wfile.write(b'{"error":"boom"}')
                else:
                    self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


def test_alert_posted_and_payload_shape():
    server = _MockAlertServer()
    try:
        ok = send_alert("something broke at 3am", webhook_url=server.base_url)
        assert ok is True
        assert len(server.received) == 1
        payload = json.loads(server.received[0])
        assert payload == {"text": "something broke at 3am"}
        print("PASS test_alert_posted_and_payload_shape")
    finally:
        server.stop()


def test_alert_reports_failure_when_http_5xx():
    server = _MockAlertServer(status=500)
    try:
        ok = send_alert("this should not be delivered", webhook_url=server.base_url)
        assert ok is False
        assert len(server.received) == 1, "server saw the request even though it rejected it"
        print("PASS test_alert_reports_failure_when_http_5xx")
    finally:
        server.stop()


def test_alert_noops_without_webhook():
    with mock.patch("alerting.alerter._resolve_webhook_url", return_value=None):
        ok = send_alert("no webhook configured")
        assert ok is True, "no webhook is not an error - caller should not crash"
    print("PASS test_alert_noops_without_webhook")


def test_build_payload_is_plain_text():
    payload = _build_payload("hello")
    assert payload == {"text": "hello"}
    print("PASS test_build_payload_is_plain_text")


if __name__ == "__main__":
    test_alert_posted_and_payload_shape()
    test_alert_reports_failure_when_http_5xx()
    test_alert_noops_without_webhook()
    test_build_payload_is_plain_text()
    print("\nall alerter tests passed")
