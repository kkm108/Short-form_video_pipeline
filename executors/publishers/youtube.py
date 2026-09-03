"""YouTube Data API v3 upload - the sanctioned replacement for browser-driven
Studio automation.

Setup, one time, by you:
  1. Create a project in Google Cloud Console, enable "YouTube Data API v3".
  2. Create OAuth 2.0 credentials (Desktop app type is simplest for a script).
  3. Run the OAuth flow once locally to get a refresh token; store it via
     credentials.vault, never in the manifest or in plaintext.
  4. Uploads via an unverified app are capped at a low daily quota - request
     an audit from Google once you're past the testing stage.

The request-body construction below is a pure function, tested in
tests/test_youtube_publisher.py without needing google-api-python-client
installed at all. The actual `publish()` method needs that package
(`pip install google-api-python-client google-auth-oauthlib`) plus a real
OAuth credential object - install and wire that up before running this for
real; it's reviewed code, not executed code, until then.
"""
from __future__ import annotations

from executors.base import ExecutorError
from executors.publishers.base import PublishMetadata, PublishResult, Publisher


class YouTubePublisher(Publisher):
    platform = "youtube"

    def __init__(self, credentials_provider, build_client=None, quota_tracker=None, quota_cost: int = 1, quota_budget: int = 96):
        self._credentials_provider = credentials_provider
        # Injectable so tests can exercise the credential-handling logic below
        # without a real googleapiclient client or real OAuth creds - see
        # tests/test_youtube_publisher.py.
        self._build_client = build_client or _default_build_client
        # Optional quota ledger (orchestrator.quota_tracker.QuotaTracker). When
        # set, an upload checks the day's remaining budget first, records its
        # cost on success, and marks the day exhausted on a quotaExceeded/
        # dailyLimitExceeded error so the next scheduled trigger backs off
        # instead of burning the whole budget on impossible retries.
        self._quota = quota_tracker
        self._quota_cost = quota_cost
        self._quota_budget = quota_budget

    def publish(self, video_path: str, metadata: PublishMetadata) -> PublishResult:
        try:
            from googleapiclient.http import MediaFileUpload  # type: ignore[import-untyped]  # google-api-python-client ships no stubs
        except ImportError as exc:
            raise ExecutorError(
                "google-api-python-client not installed - "
                "pip install google-api-python-client google-auth-oauthlib",
                retryable=False,
            ) from exc

        if self._quota is not None:
            remaining = self._quota.remaining("youtube", self._quota_budget)
            if remaining <= 0:
                raise ExecutorError(
                    f"YouTube daily upload quota exhausted (budget {self._quota_budget}, 0 remaining) - "
                    "skip today and try tomorrow",
                    retryable=False,
                )

        try:
            creds = self._credentials_provider("youtube")
            youtube = self._build_client(creds)
        except Exception as exc:
            # Covers every way "not configured yet" actually shows up: the
            # vault's NotImplementedError/CredentialNotFound, or - as happened
            # on a real machine with the SDK installed but no OAuth creds set
            # up - googleapiclient's own DefaultCredentialsError when build()
            # falls back to looking for ambient Application Default
            # Credentials. All of it means the same thing operationally, and
            # none of it gets fixed by retrying the identical call.
            raise ExecutorError(
                f"YouTube credentials aren't ready ({type(exc).__name__}: {exc}) - "
                "see credentials/vault.py's credentials_provider() and the README's YouTube setup section",
                retryable=False,
            ) from exc

        body = build_video_body(metadata)
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")

        try:
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                _, response = request.next_chunk()
        except Exception as exc:  # googleapiclient raises HttpError here; caught broadly since it's an optional dep
            # retryable set deliberately, matching the other HTTP publishers
            # (tiktok/instagram): only 5xx and genuine no-status network errors
            # are worth a retry; a 4xx from the identical request never will be.
            status_code = getattr(getattr(exc, "resp", None), "status", None)
            retryable = status_code is None or status_code >= 500
            error_reason = _error_reason(exc)
            if error_reason in ("quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded") and self._quota is not None:
                # The day's quota is spent - mark it so later triggers back off,
                # and report non-retryable (retrying the identical upload won't
                # conjure quota).
                self._quota.mark_exhausted("youtube")
                retryable = False
            raise ExecutorError(f"YouTube upload failed: {exc}", status_code=status_code, retryable=retryable) from exc

        video_id = response["id"]
        if self._quota is not None:
            self._quota.record("youtube", self._quota_cost)
        return PublishResult(platform="youtube", remote_id=video_id, url=f"https://youtube.com/shorts/{video_id}")


def _default_build_client(credentials):
    from googleapiclient.discovery import build  # type: ignore[import-untyped]  # google-api-python-client ships no stubs
    return build("youtube", "v3", credentials=credentials)


def _error_reason(exc: Exception) -> str:
    """Belt-and-braces extraction of a Google API error reason from whatever
    shape the SDK surfaces: a plain string containing the reason, or a JSON
    error body. Returns '' when it can't tell - callers treat unknown as
    'not a quota reason'."""
    import json as _json

    body = getattr(exc, "content", None)
    if body:
        text = body if isinstance(body, str) else body.decode("utf-8", "replace")
        try:
            data = _json.loads(text)
            reasons = [e.get("reason", "") for e in data.get("error", {}).get("errors", [])]
            if reasons:
                return reasons[0]
        except (ValueError, AttributeError):
            pass
        return text
    return ""


def build_video_body(metadata: PublishMetadata) -> dict:
    """Pure function on purpose: title truncation (100-char API limit) and
    the snippet/status shape the Data API expects are worth unit testing
    without needing the SDK or a real OAuth credential."""
    return {
        "snippet": {
            "title": metadata.title[:100],
            "description": metadata.description,
            "tags": metadata.hashtags,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
