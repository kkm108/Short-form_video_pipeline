"""Two things changed here after a real run on a machine that already had
google-api-python-client installed (unlike this sandbox, where it's absent
by default): the "SDK not installed" test needs to be true regardless of
what happens to be on disk, not just true in whichever environment the
author happened to write it in - and there's a new test for the bug that
run actually caught: credentials_provider()/build() failures were leaking
out as raw, unlabeled exceptions instead of a clean ExecutorError.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

from executors.base import ExecutorError
from executors.publishers.base import PublishMetadata
from executors.publishers.youtube import YouTubePublisher, build_video_body


def _fake_youtube_client(request):
    """Duck-types the googleapiclient surface publish() touches, so the upload
    loop can be exercised with unittest.mock through the same build_client
    injection point the credential tests already use - no real SDK needed."""
    videos = type("Videos", (), {"insert": lambda self, **kw: request})
    youtube = type("Youtube", (), {"videos": lambda self: videos()})
    return youtube()


class _FakeHttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"simulated HttpError status {status}")
        self.resp = type("Resp", (), {"status": status})()


def test_upload_loop_success_for_multichunk_upload():
    calls = []
    final_response = {"id": "video_123"}

    class _FakeRequest:
        def next_chunk(self):
            calls.append(1)
            if len(calls) < 3:
                return None, None  # not complete yet - a real multi-chunk upload
            return None, final_response

    request = _FakeRequest()
    youtube = _fake_youtube_client(request)
    publisher = YouTubePublisher(credentials_provider=lambda platform: "placeholder", build_client=lambda creds: youtube)
    metadata = PublishMetadata(title="t", description="d", hashtags=[])
    with patch("googleapiclient.http.MediaFileUpload") as mfu:
        mfu.return_value = object()
        result = publisher.publish("/tmp/upload.mp4", metadata)

    assert result.remote_id == "video_123"
    assert len(calls) == 3  # polled twice, received the final response on the third call
    print("PASS test_upload_loop_success_for_multichunk_upload")


def test_upload_loop_http_error_mid_upload_is_retryable_for_5xx():
    calls = []

    class _FakeRequest:
        def next_chunk(self):
            calls.append(1)
            if len(calls) < 2:
                return None, None
            raise _FakeHttpError(500)

    request = _FakeRequest()
    youtube = _fake_youtube_client(request)
    publisher = YouTubePublisher(credentials_provider=lambda platform: "placeholder", build_client=lambda creds: youtube)
    metadata = PublishMetadata(title="t", description="d", hashtags=[])
    with patch("googleapiclient.http.MediaFileUpload") as mfu:
        mfu.return_value = object()
        try:
            publisher.publish("/tmp/upload.mp4", metadata)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.status_code == 500
            assert exc.retryable is True  # transient server error: worth a retry
    print("PASS test_upload_loop_http_error_mid_upload_is_retryable_for_5xx")


def test_upload_loop_http_error_4xx_is_not_retryable():
    class _FakeRequest:
        def next_chunk(self):
            raise _FakeHttpError(403)

    request = _FakeRequest()
    youtube = _fake_youtube_client(request)
    publisher = YouTubePublisher(credentials_provider=lambda platform: "placeholder", build_client=lambda creds: youtube)
    metadata = PublishMetadata(title="t", description="d", hashtags=[])
    with patch("googleapiclient.http.MediaFileUpload") as mfu:
        mfu.return_value = object()
        try:
            publisher.publish("/tmp/upload.mp4", metadata)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.status_code == 403
            assert exc.retryable is False  # 4xx: repeating the identical request won't help
    print("PASS test_upload_loop_http_error_4xx_is_not_retryable")


def test_build_video_body_truncates_title_to_100_chars():
    metadata = PublishMetadata(title="x" * 150, description="d", hashtags=["a"])
    body = build_video_body(metadata)
    assert len(body["snippet"]["title"]) == 100
    print("PASS test_build_video_body_truncates_title_to_100_chars")


def test_build_video_body_shape_matches_data_api_v3():
    metadata = PublishMetadata(title="A short film", description="desc here", hashtags=["shorts", "ai"])
    body = build_video_body(metadata)

    assert body["snippet"]["title"] == "A short film"
    assert body["snippet"]["description"] == "desc here"
    assert body["snippet"]["tags"] == ["shorts", "ai"]
    assert body["snippet"]["categoryId"] == "22"
    assert body["status"]["privacyStatus"] == "public"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    print("PASS test_build_video_body_shape_matches_data_api_v3")


def test_publisher_fails_fast_when_sdk_not_importable():
    """Forces the import to fail via sys.modules regardless of whether
    google-api-python-client is actually installed in this environment -
    the previous version of this test only worked by accident, because it
    happened to be written somewhere the package was absent."""
    with patch.dict(sys.modules, {"googleapiclient": None, "googleapiclient.http": None, "googleapiclient.discovery": None}):
        publisher = YouTubePublisher(credentials_provider=lambda platform: None)
        metadata = PublishMetadata(title="t", description="d", hashtags=[])
        try:
            publisher.publish("/tmp/does-not-matter.mp4", metadata)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert "google-api-python-client" in str(exc)
            assert exc.retryable is False
            print("PASS test_publisher_fails_fast_when_sdk_not_importable")


def test_publisher_wraps_credentials_provider_failure_cleanly():
    """Reproduces the vault's real behavior for youtube today: it raises
    NotImplementedError until someone wires up a stored OAuth refresh
    token. That must come out as a clean, non-retryable ExecutorError."""

    def failing_provider(platform: str):
        raise NotImplementedError("youtube credentials not wired up yet")

    publisher = YouTubePublisher(credentials_provider=failing_provider)
    metadata = PublishMetadata(title="t", description="d", hashtags=[])
    try:
        publisher.publish("/tmp/does-not-matter.mp4", metadata)
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        assert "NotImplementedError" in str(exc)
        print("PASS test_publisher_wraps_credentials_provider_failure_cleanly")


def test_publisher_wraps_build_client_failure_cleanly():
    """Reproduces exactly what happened on a real machine: googleapiclient
    IS installed, credentials_provider returns *something*, but build()
    itself fails - there, a real DefaultCredentialsError; here, a stand-in -
    because no usable credentials exist yet. Must also come out clean."""

    def failing_build_client(credentials):
        raise RuntimeError("simulated: no Application Default Credentials found")

    publisher = YouTubePublisher(credentials_provider=lambda platform: "placeholder", build_client=failing_build_client)
    metadata = PublishMetadata(title="t", description="d", hashtags=[])
    try:
        publisher.publish("/tmp/does-not-matter.mp4", metadata)
        assert False, "expected ExecutorError"
    except ExecutorError as exc:
        assert exc.retryable is False
        assert "RuntimeError" in str(exc)
        print("PASS test_publisher_wraps_build_client_failure_cleanly")


class _QuotaError(Exception):
    """Simulates googleapiclient HttpError carrying a quotaExceeded body."""

    def __init__(self, reason: str):
        super().__init__("simulated quota error")
        import json as _j
        self.resp = type("Resp", (), {"status": 403})()
        body = {"error": {"errors": [{"reason": reason}]}}
        self.content = _j.dumps(body)


def test_publisher_checks_budget_and_blocks_when_exhausted_before_upload():
    import tempfile
    from pathlib import Path
    from orchestrator.quota_tracker import QuotaTracker

    with tempfile.TemporaryDirectory() as tmp:
        tracker = QuotaTracker(Path(tmp) / "ledger.json")
        tracker.record("youtube", cost=96)  # full budget
        publisher = YouTubePublisher(
            credentials_provider=lambda platform: "placeholder",
            quota_tracker=tracker,
            quota_cost=1,
            quota_budget=96,
        )
        metadata = PublishMetadata(title="t", description="d", hashtags=[])
        with patch("googleapiclient.http.MediaFileUpload") as mfu:
            mfu.return_value = object()
            try:
                publisher.publish("/tmp/upload.mp4", metadata)
                assert False, "expected ExecutorError when budget is exhausted"
            except ExecutorError as exc:
                assert exc.retryable is False, "no budget means retrying cannot help"
                assert "quota" in str(exc)
    print("PASS test_publisher_checks_budget_and_blocks_when_exhausted_before_upload")


def test_publisher_records_cost_on_success():
    import tempfile
    from pathlib import Path
    from orchestrator.quota_tracker import QuotaTracker

    with tempfile.TemporaryDirectory() as tmp:
        tracker = QuotaTracker(Path(tmp) / "ledger.json")
        final_response = {"id": "video_quota"}

        class _FakeRequest:
            def next_chunk(self):
                return None, final_response

        request = _FakeRequest()
        youtube = _fake_youtube_client(request)
        publisher = YouTubePublisher(
            credentials_provider=lambda platform: "placeholder",
            build_client=lambda creds: youtube,
            quota_tracker=tracker,
            quota_cost=6,
            quota_budget=96,
        )
        metadata = PublishMetadata(title="t", description="d", hashtags=[])
        with patch("googleapiclient.http.MediaFileUpload") as mfu:
            mfu.return_value = object()
            result = publisher.publish("/tmp/upload.mp4", metadata)
        assert result.remote_id == "video_quota"
        assert tracker.used_today("youtube") == 6, "a successful upload must record its cost"
    print("PASS test_publisher_records_cost_on_success")


def test_publisher_marks_exhausted_and_non_retryable_on_quota_errors():
    import tempfile
    from pathlib import Path
    from orchestrator.quota_tracker import QuotaTracker

    with tempfile.TemporaryDirectory() as tmp:
        tracker = QuotaTracker(Path(tmp) / "ledger.json")

        class _FakeRequest:
            def next_chunk(self):
                raise _QuotaError("quotaExceeded")

        request = _FakeRequest()
        youtube = _fake_youtube_client(request)
        publisher = YouTubePublisher(
            credentials_provider=lambda platform: "placeholder",
            build_client=lambda creds: youtube,
            quota_tracker=tracker,
            quota_cost=6,
            quota_budget=96,
        )
        metadata = PublishMetadata(title="t", description="d", hashtags=[])
        with patch("googleapiclient.http.MediaFileUpload") as mfu:
            mfu.return_value = object()
            try:
                publisher.publish("/tmp/upload.mp4", metadata)
                assert False, "expected ExecutorError"
            except ExecutorError as exc:
                assert exc.retryable is False, "quotaExceeded must not be blindly retried"
                assert exc.status_code == 403
        assert tracker.is_exhausted("youtube") is True, "quotaExceeded must mark the day exhausted"
    print("PASS test_publisher_marks_exhausted_and_non_retryable_on_quota_errors")


if __name__ == "__main__":
    test_build_video_body_truncates_title_to_100_chars()
    test_build_video_body_shape_matches_data_api_v3()
    test_publisher_fails_fast_when_sdk_not_importable()
    test_publisher_wraps_credentials_provider_failure_cleanly()
    test_publisher_wraps_build_client_failure_cleanly()
    test_upload_loop_success_for_multichunk_upload()
    test_upload_loop_http_error_mid_upload_is_retryable_for_5xx()
    test_upload_loop_http_error_4xx_is_not_retryable()
    test_publisher_checks_budget_and_blocks_when_exhausted_before_upload()
    test_publisher_records_cost_on_success()
    test_publisher_marks_exhausted_and_non_retryable_on_quota_errors()
    print("\nall youtube publisher tests passed")
