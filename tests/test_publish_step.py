"""Proves the exact property SinglePlatformPublishExecutor exists for: with
three platforms as three separate manifest steps, a failure on the third
platform does NOT re-publish the first two on resume."""
from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

from executors.base import ExecutorError, ExecutorOutput
from executors.publish_step import SinglePlatformPublishExecutor
from executors.publishers.base import PublishMetadata, PublishResult, Publisher
from orchestrator.engine import Pipeline
from orchestrator.state import StateStore


class _FakePublisher(Publisher):
    def __init__(self, platform: str, fail_times: int = 0):
        self.platform = platform
        self.fail_times = fail_times
        self.calls = 0

    def publish(self, video_path: str, metadata: PublishMetadata) -> PublishResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ExecutorError(f"{self.platform} publish failed", status_code=503)
        return PublishResult(platform=self.platform, remote_id=f"{self.platform}_id_{self.calls}", url=None)


class _StubAssembly:
    name = "assembly_stub"

    def run(self, context):
        return ExecutorOutput(output_ref="/tmp/assembled.mp4")


def test_failed_platform_does_not_reset_succeeded_siblings_on_resume():
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.yaml"
        manifest_path.write_text(
            textwrap.dedent(
                """
                run:
                  platforms: [youtube, instagram, tiktok]
                steps:
                  - name: assembly
                    executor: assembly_stub
                  - name: publish_youtube
                    executor: publish_youtube
                  - name: publish_instagram
                    executor: publish_instagram
                  - name: publish_tiktok
                    executor: publish_tiktok
                    retry: {max_attempts: 2, backoff: none, retry_on: [503]}
                """
            )
        )
        youtube_pub = _FakePublisher("youtube")
        instagram_pub = _FakePublisher("instagram")
        tiktok_pub = _FakePublisher("tiktok", fail_times=5)

        state = StateStore(str(Path(tmp) / "state.db"))
        pipeline = Pipeline(
            state=state,
            executors={
                "assembly_stub": _StubAssembly(),
                "publish_youtube": SinglePlatformPublishExecutor(youtube_pub),
                "publish_instagram": SinglePlatformPublishExecutor(instagram_pub),
                "publish_tiktok": SinglePlatformPublishExecutor(tiktok_pub),
            },
            workdir=tmp,
        )

        run_id = pipeline.start(str(manifest_path), "seed topic")
        run = state.get_run(run_id)

        assert run.steps["publish_youtube"].status.value == "succeeded"
        assert run.steps["publish_instagram"].status.value == "succeeded"
        assert run.steps["publish_tiktok"].status.value == "failed"  # exhausted its 2 attempts
        assert youtube_pub.calls == 1
        assert instagram_pub.calls == 1

        # simulate: someone fixes whatever was wrong with tiktok, then resumes
        tiktok_pub.fail_times = 0
        pipeline.resume(run_id)

        # the fix: youtube and instagram were NOT called again
        assert youtube_pub.calls == 1
        assert instagram_pub.calls == 1
        run = state.get_run(run_id)
        assert run.steps["publish_tiktok"].status.value == "succeeded"
        print("PASS test_failed_platform_does_not_reset_succeeded_siblings_on_resume")


if __name__ == "__main__":
    test_failed_platform_does_not_reset_succeeded_siblings_on_resume()
    print("\nall publish_step tests passed")
