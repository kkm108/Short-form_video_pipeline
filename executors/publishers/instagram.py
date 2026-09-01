"""Meta Graph API (Instagram Content Publishing API) - Reels.

Setup, one time, by you:
  1. The Instagram account must be a Professional (Business/Creator) account,
     linked to a Facebook Page.
  2. Create a Meta app, add the Instagram Graph API product, generate a
     long-lived access token for that Page/IG user.
  3. The Graph API does NOT accept a direct file upload for the container
     step - it fetches the video FROM A URL you give it. That means the
     rendered .mp4 needs a temporary public URL (e.g. a presigned S3 link)
     before this executor runs; staging that URL is a separate small step,
     not something this class does for you.
"""
from __future__ import annotations

import time

import requests

from executors.base import ExecutorError
from executors.publishers.base import PublishMetadata, PublishResult, Publisher

DEFAULT_GRAPH_BASE = "https://graph.facebook.com/v19.0"


class InstagramPublisher(Publisher):
    platform = "instagram"

    def __init__(self, credentials_provider, graph_base: str = DEFAULT_GRAPH_BASE, poll_interval_s: float = 10.0):
        self._credentials_provider = credentials_provider
        self._graph_base = graph_base
        self._poll_interval_s = poll_interval_s

    def publish(self, video_url: str, metadata: PublishMetadata) -> PublishResult:
        # NOTE: `video_url` must already be a public URL, not a local path -
        # see module docstring.
        try:
            creds = self._credentials_provider("instagram")
        except Exception as exc:
            raise ExecutorError(
                f"Instagram credentials aren't ready ({type(exc).__name__}: {exc}) - "
                "see credentials/vault.py's credentials_provider() and the README's Instagram setup section",
                retryable=False,
            ) from exc
        ig_user_id, access_token = creds["ig_user_id"], creds["access_token"]
        caption = build_caption(metadata)

        create = requests.post(
            f"{self._graph_base}/{ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": access_token,
            },
            timeout=30,
        )
        _raise_for_graph_error(create)
        container_id = create.json()["id"]

        self._wait_until_processed(container_id, access_token)

        publish = requests.post(
            f"{self._graph_base}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": access_token},
            timeout=30,
        )
        _raise_for_graph_error(publish)
        media_id = publish.json()["id"]
        return PublishResult(platform="instagram", remote_id=media_id, url=None)

    def _wait_until_processed(self, container_id: str, access_token: str, max_polls: int = 30) -> None:
        for _ in range(max_polls):
            status = requests.get(
                f"{self._graph_base}/{container_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=15,
            ).json()
            code = status.get("status_code")
            if code == "FINISHED":
                return
            if code == "ERROR":
                raise ExecutorError("Instagram container processing failed", retryable=False)
            time.sleep(self._poll_interval_s)
        raise ExecutorError("Instagram container still processing after the poll budget", status_code=429)


def build_caption(metadata: PublishMetadata) -> str:
    """Pure function on purpose: the 2200-char Graph API caption limit and
    the description+hashtags join logic are easy to get subtly wrong and
    cost nothing to unit test in isolation from any HTTP call."""
    tags = " ".join(f"#{h}" for h in metadata.hashtags)
    return f"{metadata.description}\n\n{tags}"[:2200]


def _raise_for_graph_error(resp: requests.Response) -> None:
    if resp.ok:
        return
    raise ExecutorError(
        f"Graph API error: {resp.text[:300]}",
        status_code=resp.status_code,
        retryable=resp.status_code >= 500,
    )
