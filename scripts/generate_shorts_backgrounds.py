"""Generate abstract 9:16 background clips locally with FFmpeg.

This is the keyless visual fallback for the Modern Facts Shorts pipeline. It
uses only FFmpeg's built-in ``gradients`` source plus cheap video filters, so
it runs on GitHub Actions without any stock-footage API key.

The generated clips are consumed by MoneyPrinterTurbo through its existing
``--video-source local`` material path (see ``scripts/run_modern_facts_shorts``).

Usage:
    uv run python scripts/generate_shorts_backgrounds.py \
        --output-dir storage/local_videos/modern-facts --count 8 --duration 12
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

DEFAULT_SIZE = "1080x1920"
DEFAULT_FPS = 30
DEFAULT_CRF = 23
OUTPUT_GLOB = "background-*.mp4"

# Every preset renders a slowly moving abstract gradient that fits an
# AI/future-technology channel. ``source`` and ``filters`` are str.format
# templates; ``seed`` keeps each generated file deterministic per run.
BACKGROUND_PRESETS: tuple[dict[str, str], ...] = (
    {
        "name": "deep-space-blue",
        "source": (
            "gradients=s={size}:c0=0x05070f:c1=0x1b6ef3:c2=0x0a2f5e:n=3"
            ":speed=0.02:r={fps}:d={duration}:seed={seed}:type=linear"
        ),
        "filters": (
            "hue=h='25*sin(2*PI*t/{duration})'",
            "noise=alls=6:allf=t",
            "vignette=PI/5",
        ),
    },
    {
        "name": "plasma-violet",
        "source": (
            "gradients=s={size}:c0=0x120024:c1=0x7b2ff7:c2=0xf107a5:n=3"
            ":speed=0.03:r={fps}:d={duration}:seed={seed}:type=radial"
        ),
        "filters": ("vignette=PI/4",),
    },
    {
        "name": "circuit-teal",
        "source": (
            "gradients=s={size}:c0=0x001a1a:c1=0x00d4a0:c2=0x0b7285:n=3"
            ":speed=0.015:r={fps}:d={duration}:seed={seed}:type=spiral"
        ),
        "filters": ("noise=alls=5:allf=t", "vignette=PI/5"),
    },
    {
        "name": "ember-orange",
        "source": (
            "gradients=s={size}:c0=0x1a0505:c1=0xff7a18:c2=0x992800:n=3"
            ":speed=0.025:r={fps}:d={duration}:seed={seed}:type=circular"
        ),
        "filters": (
            "hue=h='20*sin(2*PI*t/{duration})'",
            "vignette=PI/5",
        ),
    },
    {
        "name": "quantum-indigo",
        "source": (
            "gradients=s={size}:c0=0x020212:c1=0x3d5afe:c2=0x7c4dff:n=3"
            ":speed=0.018:r={fps}:d={duration}:seed={seed}:type=square"
        ),
        "filters": ("hue=h='15*sin(2*PI*t/{duration})'", "vignette=PI/4"),
    },
    {
        "name": "signal-magenta",
        "source": (
            "gradients=s={size}:c0=0x180018:c1=0xff2e97:c2=0x4a00e0:n=3"
            ":speed=0.022:r={fps}:d={duration}:seed={seed}:type=linear"
        ),
        "filters": ("noise=alls=5:allf=t", "vignette=PI/5"),
    },
)


class BackgroundGenerationError(RuntimeError):
    """Raised when FFmpeg is unavailable or a clip fails to render."""


def find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise BackgroundGenerationError(
            "ffmpeg was not found on PATH; install ffmpeg before generating "
            "backgrounds"
        )
    return ffmpeg


def build_ffmpeg_command(
    preset: dict[str, str],
    output_path: Path,
    *,
    ffmpeg: str,
    size: str = DEFAULT_SIZE,
    duration: float = 12.0,
    fps: int = DEFAULT_FPS,
    seed: int = 1,
    crf: int = DEFAULT_CRF,
) -> list[str]:
    """Build the argument vector for one background clip."""
    source = preset["source"].format(
        size=size,
        fps=fps,
        duration=f"{duration:g}",
        seed=seed,
    )
    filter_chain = ",".join(
        item.format(size=size, fps=fps, duration=f"{duration:g}", seed=seed)
        for item in preset["filters"]
    )
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        source,
    ]
    if filter_chain:
        command.extend(["-vf", filter_chain])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-t",
            f"{duration:g}",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


def existing_backgrounds(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    return sorted(path for path in output_dir.glob(OUTPUT_GLOB) if path.is_file())


def generate_backgrounds(
    output_dir: Path,
    *,
    count: int = 8,
    duration: float = 12.0,
    size: str = DEFAULT_SIZE,
    fps: int = DEFAULT_FPS,
    force: bool = False,
    log=print,
) -> list[Path]:
    """Render ``count`` clips into ``output_dir`` and return their paths.

    Existing clips are reused unless ``force`` is set, so re-running the
    pipeline in a fresh CI job regenerates them while local runs stay fast.
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    if duration <= 0:
        raise ValueError(f"duration must be > 0, got {duration}")

    output_dir.mkdir(parents=True, exist_ok=True)
    existing = existing_backgrounds(output_dir)
    if force:
        for path in existing:
            path.unlink(missing_ok=True)
        existing = []
    if len(existing) >= count:
        log(f"reusing {len(existing)} background clip(s) in {output_dir}")
        return existing[:count]

    ffmpeg = find_ffmpeg()
    produced = list(existing)
    index = len(existing)
    while len(produced) < count:
        preset = BACKGROUND_PRESETS[index % len(BACKGROUND_PRESETS)]
        output_path = output_dir / f"background-{index + 1:02d}-{preset['name']}.mp4"
        seed = index + 1
        command = build_ffmpeg_command(
            preset,
            output_path,
            ffmpeg=ffmpeg,
            size=size,
            duration=duration,
            fps=fps,
            seed=seed,
        )
        log(
            f"rendering background {len(produced) + 1}/{count}: "
            f"{output_path.name} ({size}, {duration:g}s)"
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise BackgroundGenerationError(
                f"failed to launch ffmpeg: {exc}"
            ) from exc
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise BackgroundGenerationError(
                f"ffmpeg failed for preset {preset['name']} "
                f"(exit {completed.returncode}): {detail[:800]}"
            )
        produced.append(output_path)
        index += 1
    return produced


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate abstract vertical background clips with FFmpeg."
    )
    parser.add_argument(
        "--output-dir",
        default="storage/local_videos/modern-facts",
        help="directory that receives the generated mp4 files",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=8,
        help="number of clips to generate (default: 8)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=12.0,
        help="duration of each clip in seconds (default: 12)",
    )
    parser.add_argument(
        "--size",
        default=DEFAULT_SIZE,
        help=f"resolution of each clip, WIDTHxHEIGHT (default: {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_FPS,
        help=f"frame rate (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate clips even when outputs already exist",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        paths = generate_backgrounds(
            Path(args.output_dir),
            count=args.count,
            duration=args.duration,
            size=args.size,
            fps=args.fps,
            force=args.force,
        )
    except (BackgroundGenerationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
