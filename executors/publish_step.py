"""Wraps a single platform's Publisher as an Executor. Each platform gets its
own manifest step (publish_youtube, publish_instagram, publish_tiktok)
rather than one grouped 'publish' step - that matters because the engine's
idempotency is per-step: with three platforms behind one step, a failure on
platform 3 after 1 and 2 already succeeded would re-publish all three on
retry. Separate steps mean a retry only ever touches the platform that
actually failed.
"""
from __future__ import annotations

from executors.base import ExecutorError, ExecutorOutput, StepContext
from executors.publishers.base import PublishMetadata, Publisher


class SinglePlatformPublishExecutor:
    def __init__(self, publisher: Publisher):
        self._publisher = publisher
        self.name = f"publish_{publisher.platform}"

    def run(self, context: StepContext) -> ExecutorOutput:
        assembled = context.upstream.get("assembly")
        if assembled is None:
            raise ExecutorError("publish requires a preceding assembly step", retryable=False)

        metadata = self._build_metadata(context)
        result = self._publisher.publish(assembled.output_ref, metadata)
        return ExecutorOutput(
            output_ref=result.remote_id,
            data={"url": result.url, "platform": result.platform, "remote_id": result.remote_id},
        )

    def _build_metadata(self, context: StepContext) -> PublishMetadata:
        script_step = context.upstream.get("script")
        script = script_step.data.get("script", "") if script_step else ""
        return PublishMetadata(
            title=context.seed_topic[:100],
            description=script or context.seed_topic,
            hashtags=context.step_config.get("hashtags", []),
        )
