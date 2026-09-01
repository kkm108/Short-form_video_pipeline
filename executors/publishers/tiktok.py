"""TikTok Content Posting API (Direct Post).

Setup, one time, by you:
  1. Register an app in the TikTok for Developers portal, add the Content
     Posting API product.
  2. Public posting on accounts you don't own requires TikTok's audit/review;
     until approved, Direct Post only works for accounts added as test users
     in the developer portal. Plan the first several weeks around that.
  3. OAuth token scoped to video.publish.
"""
from __future__ import annotations

import os

import requests

from executors.base import ExecutorError
from executors.publishers.base import PublishMetadata, PublishResult, Publisher

DEFAULT_API_BASE = "https://open.tiktokapis.com/v2"


class TikTokPublisher(Publisher):
    platform = "tiktok"

    def __init__(self, credentials_provider, api_base: str = DEFAULT_API_BASE):
        self._credentials_provider = credentials_provider
        self._api_base = api_base

    def publish(self, video_path: str, metadata: PublishMetadata) -> PublishResult:
        try:
            access_token = self._credentials_provider("tiktok")["access_token"]
        except Exception as exc:
            raise ExecutorError(
                f"TikTok credentials aren't ready ({type(exc).__name__}: {exc}) - "
                "see credentials/vault.py's credentials_provider() and the README's TikTok setup section",
                retryable=False,
            ) from exc
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        video_size = os.path.getsize(video_path)

        init = requests.post(
            f"{self._api_base}/post/publish/video/init/",
            headers=headers,
            json=build_init_body(metadata, video_size),
            timeout=30,
        )
        if not init.ok:
            raise ExecutorError(
                f"TikTok init failed: {init.text[:300]}",
                status_code=init.status_code,
                retryable=init.status_code >= 500,
            )

        body = init.json()["data"]
        upload_url, publish_id = body["upload_url"], body["publish_id"]

        with open(video_path, "rb") as f:
            video_bytes = f.read()
        upload = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{len(video_bytes) - 1}/{len(video_bytes)}",
            },
            data=video_bytes,
            timeout=120,
        )
        if not upload.ok:
            raise ExecutorError(f"TikTok upload PUT failed: {upload.status_code}", status_code=upload.status_code)

        return PublishResult(platform="tiktok", remote_id=publish_id, url=None)


def build_init_body(metadata: PublishMetadata, video_size: int) -> dict:
    """Pure function on purpose - the request shape (title truncation, single-
    chunk upload sizing) is exactly the kind of detail worth unit testing
    without needing a live server."""
    return {
        "post_info": {
            "title": metadata.title[:150],
            "privacy_level": "PUBLIC_TO_EVERYONE",
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": video_size,
            "total_chunk_count": 1,
        },
    }
