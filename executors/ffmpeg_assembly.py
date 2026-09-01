"""Deterministic assembly: images/clips + voiceover + music -> one rendered,
platform-shaped video. No AI calls happen here - this step is pure ffmpeg,
which is what makes it fast, cheap, and easy to unit test (see
tests/test_ffmpeg_assembly.py).
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from executors.base import ExecutorError, ExecutorOutput, StepContext

# Shorts / Reels / TikTok are all 9:16 at 1080x1920 as of writing this - the
# one constant most likely to drift is platform-specific caption-length
# limits, which stay in per-publisher config, not here.
TARGET_W, TARGET_H = 1080, 1920
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class MediaBundle:
    voiceover_path: str
    clip_paths: list[str]  # images and/or video clips, shown in order
    music_path: Optional[str] = None
    captions_srt_path: Optional[str] = None


class FfmpegAssemblyExecutor:
    name = "ffmpeg"

    def run(self, context: StepContext) -> ExecutorOutput:
        if shutil.which("ffmpeg") is None:
            raise ExecutorError("ffmpeg not found on PATH", retryable=False)

        bundle = self._load_bundle(context)
        out_path = str(Path(context.workdir) / "assembled.mp4")

        try:
            self._render(bundle, out_path, context.workdir)
        except subprocess.CalledProcessError as exc:
            stderr_tail = (exc.stderr or "")[-800:]
            raise ExecutorError(f"ffmpeg failed: {stderr_tail}", retryable=False) from exc

        return ExecutorOutput(output_ref=out_path)

    def _load_bundle(self, context: StepContext) -> MediaBundle:
        media_step = context.upstream.get("media_generation")
        if media_step is None:
            raise ExecutorError("assembly requires a preceding media_generation step", retryable=False)
        d = media_step.data
        if "voiceover_path" not in d or "clip_paths" not in d:
            raise ExecutorError(
                "media_generation output is missing voiceover_path/clip_paths - "
                "check the upstream executor's output schema",
                retryable=False,
            )
        return MediaBundle(
            voiceover_path=d["voiceover_path"],
            clip_paths=d["clip_paths"],
            music_path=d.get("music_path"),
            captions_srt_path=d.get("captions_srt_path"),
        )

    def _render(self, bundle: MediaBundle, out_path: str, workdir: str) -> None:
        silent_track = self._build_silent_track(bundle, workdir)

        cmd = ["ffmpeg", "-y", "-i", silent_track, "-i", bundle.voiceover_path]
        filter_parts = [
            f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H}[v0]"
        ]
        video_label = "[v0]"

        if bundle.captions_srt_path and Path(bundle.captions_srt_path).exists():
            filter_parts.append(f"{video_label}subtitles={_escape_for_filter(bundle.captions_srt_path)}[v1]")
            video_label = "[v1]"

        audio_labels = ["[1:a]"]
        if bundle.music_path:
            cmd += ["-i", bundle.music_path]
            filter_parts.append("[2:a]volume=0.15[music]")
            audio_labels.append("[music]")

        if len(audio_labels) > 1:
            filter_parts.append(f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=first[aout]")
            audio_label = "[aout]"  # a genuine filter_complex output pad - brackets required
        else:
            audio_label = "1:a"  # a raw input stream never touched by the filter graph - -map wants it
            # WITHOUT brackets here; brackets in -map specifically mean "a filter_complex pad,"
            # and ffmpeg errors out looking for a pad literally named "1:a" if you bracket it.

        cmd += [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            video_label,
            "-map",
            audio_label,
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    def _build_silent_track(self, bundle: MediaBundle, workdir: str, seconds_per_image: float = 3.0) -> str:
        """Normalize every clip (image or video) into a same-codec silent .ts
        segment, then concat with a stream copy. Stream-copy concat only
        works reliably when every input already shares codec/resolution/
        timebase - normalizing first avoids the classic 'concat silently
        drops half the clips' failure mode.
        """
        segments = []
        for i, clip in enumerate(bundle.clip_paths):
            seg_path = str(Path(workdir) / f"seg_{i:03d}.ts")
            vf = f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,crop={TARGET_W}:{TARGET_H}"
            if Path(clip).suffix.lower() in _IMAGE_EXTS:
                cmd = [
                    "ffmpeg", "-y", "-loop", "1", "-t", str(seconds_per_image), "-i", clip,
                    "-vf", vf, "-c:v", "mpeg2video", "-q:v", "2", "-f", "mpegts", seg_path,
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-i", clip, "-an",
                    "-vf", vf, "-c:v", "mpeg2video", "-q:v", "2", "-f", "mpegts", seg_path,
                ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            segments.append(seg_path)

        concat_out = str(Path(workdir) / "clips_concat.mp4")
        concat_input = "concat:" + "|".join(segments)
        subprocess.run(
            ["ffmpeg", "-y", "-i", concat_input, "-c", "copy", concat_out],
            check=True, capture_output=True, text=True,
        )
        return concat_out


def _escape_for_filter(path: str) -> str:
    return path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
