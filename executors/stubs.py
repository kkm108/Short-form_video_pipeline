"""Stand-ins for the three steps that otherwise need real accounts (LLM,
media-generation tool, platform APIs), so the *shape* of a run - script ->
media -> assembly -> review_gate -> publish, with real checkpointing and a
real pause at the review gate - can be verified with zero external setup.

`assembly` and `human_checkpoint` are NOT stubbed here; the real ones are
used as-is in manifests/dry_run.yaml. They don't need external accounts
either, and running the real ffmpeg render is the whole point of a
pipeline smoke test - a stub that skips it would prove nothing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from executors.base import ExecutorOutput, StepContext

_CANNED_SCRIPT = (
    "You won't believe how short the shortest war in history actually was.\n"
    "In 1896, Zanzibar surrendered to Britain in under forty minutes.\n"
    "No treaty took longer to read than the war took to fight."
)


class StubScriptExecutor:
    name = "llm_dryrun"

    def run(self, context: StepContext) -> ExecutorOutput:
        out_path = str(Path(context.workdir) / "script.txt")
        Path(out_path).write_text(_CANNED_SCRIPT)
        return ExecutorOutput(output_ref=out_path, data={"script": _CANNED_SCRIPT})


class StubMediaGenerationExecutor:
    """Generates real files (via ffmpeg's own synthetic sources - the same
    technique tests/test_ffmpeg_assembly.py uses) rather than fake paths, so
    the real assembly step downstream has something real to render."""

    name = "media_generation_dryrun"

    def run(self, context: StepContext) -> ExecutorOutput:
        workdir = Path(context.workdir)
        clip_paths = [self._make_image(workdir / f"clip_{i}.png", color) for i, color in enumerate(["red", "blue", "green"])]
        voice_path = self._make_tone(workdir / "voice.wav", seconds=6.0)

        return ExecutorOutput(
            output_ref=str(workdir),
            data={"voiceover_path": voice_path, "clip_paths": clip_paths, "music_path": None},
        )

    @staticmethod
    def _make_image(path: Path, color: str) -> str:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=640x360", "-frames:v", "1", str(path)],
            check=True, capture_output=True,
        )
        return str(path)

    @staticmethod
    def _make_tone(path: Path, seconds: float) -> str:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(path)],
            check=True, capture_output=True,
        )
        return str(path)


class StubPublishExecutor:
    """Logs what it would have sent and returns a fake id - no network call,
    no credentials needed. One instance per platform, same as the real
    SinglePlatformPublishExecutor, so dry_run.yaml can list all three
    platforms as separate steps just like the real manifest does."""

    def __init__(self, platform: str):
        self.platform = platform
        self.name = f"publish_{platform}_dryrun"

    def run(self, context: StepContext) -> ExecutorOutput:
        assembled = context.upstream.get("assembly")
        video_path = assembled.output_ref if assembled else "(no assembled video)"
        print(f"[dry run] would publish {video_path} to {self.platform} with topic {context.seed_topic!r}")
        return ExecutorOutput(output_ref=f"dryrun_{self.platform}_id", data={"platform": self.platform})
