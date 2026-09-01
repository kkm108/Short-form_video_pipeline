"""Smoke test: generates synthetic clips/voiceover with ffmpeg's own test
sources (lavfi color + sine) so no external assets are needed, then proves
the assembly executor produces a correctly-shaped, playable output.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from executors.base import ExecutorOutput, StepContext
from executors.ffmpeg_assembly import FfmpegAssemblyExecutor


def _make_test_image(path: str, color: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=640x360", "-frames:v", "1", path],
        check=True, capture_output=True, text=True,
    )


def _make_test_audio(path: str, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", path],
        check=True, capture_output=True, text=True,
    )


def _probe_dimensions(path: str) -> tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    w, h = result.stdout.strip().split(",")
    return int(w), int(h)


def _probe_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def test_assembly_crops_to_vertical_and_has_audio():
    with tempfile.TemporaryDirectory() as tmp:
        img1, img2 = f"{tmp}/a.png", f"{tmp}/b.png"
        voice = f"{tmp}/voice.wav"
        _make_test_image(img1, "red")
        _make_test_image(img2, "blue")
        _make_test_audio(voice, 4.0)

        context = StepContext(
            run_id="test", seed_topic="test", platforms=["youtube"],
            step_config={}, workdir=tmp,
            upstream={
                "media_generation": ExecutorOutput(
                    output_ref="",
                    data={"voiceover_path": voice, "clip_paths": [img1, img2], "music_path": None},
                )
            },
        )
        output = FfmpegAssemblyExecutor().run(context)

        assert Path(output.output_ref).exists()
        assert _probe_dimensions(output.output_ref) == (1080, 1920)  # 640x360 source correctly cropped to 9:16
        assert _probe_duration(output.output_ref) > 3.5  # roughly matches the 4s voiceover ("-shortest")
        print("PASS test_assembly_crops_to_vertical_and_has_audio")


def test_assembly_mixes_background_music():
    with tempfile.TemporaryDirectory() as tmp:
        img = f"{tmp}/a.png"
        voice = f"{tmp}/voice.wav"
        music = f"{tmp}/music.wav"
        _make_test_image(img, "green")
        _make_test_audio(voice, 3.0)
        _make_test_audio(music, 10.0)  # longer than voiceover - output should still be trimmed to "-shortest"

        context = StepContext(
            run_id="test2", seed_topic="test", platforms=["youtube"],
            step_config={}, workdir=tmp,
            upstream={
                "media_generation": ExecutorOutput(
                    output_ref="",
                    data={"voiceover_path": voice, "clip_paths": [img], "music_path": music},
                )
            },
        )
        output = FfmpegAssemblyExecutor().run(context)

        assert Path(output.output_ref).exists()
        assert _probe_dimensions(output.output_ref) == (1080, 1920)
        print("PASS test_assembly_mixes_background_music")


def test_assembly_fails_fast_without_upstream_media():
    with tempfile.TemporaryDirectory() as tmp:
        from executors.base import ExecutorError

        context = StepContext(
            run_id="test3", seed_topic="test", platforms=["youtube"],
            step_config={}, workdir=tmp, upstream={},  # no media_generation output
        )
        try:
            FfmpegAssemblyExecutor().run(context)
            assert False, "expected ExecutorError"
        except ExecutorError as exc:
            assert exc.retryable is False
            print("PASS test_assembly_fails_fast_without_upstream_media")


if __name__ == "__main__":
    test_assembly_crops_to_vertical_and_has_audio()
    test_assembly_mixes_background_music()
    test_assembly_fails_fast_without_upstream_media()
    print("\nall ffmpeg assembly tests passed")
