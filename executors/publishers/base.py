"""Publisher interface. Every implementation talks to a platform's official,
sanctioned API - never a browser session against a logged-in personal/brand
account. That trade means more setup (each platform requires its own app
registration and, for TikTok, an audit before public posting) in exchange
for not depending on scraping a UI that can flag, throttle, or ban the
account it runs as.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PublishMetadata:
    title: str
    description: str
    hashtags: list[str]


@dataclass
class PublishResult:
    platform: str
    remote_id: str
    url: str | None


class Publisher(ABC):
    platform: str

    @abstractmethod
    def publish(self, video_path_or_url: str, metadata: PublishMetadata) -> PublishResult:
        ...
