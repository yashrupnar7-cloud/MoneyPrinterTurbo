"""Publish one generated Modern Facts short to YouTube through Upload-Post.

Reuses the repository's existing ``app/services/upload_post.py`` integration
unchanged: a multipart ``POST https://api.upload-post.com/api/upload`` with the
``Authorization: Apikey`` header, followed by the service's own background
status polling (``GET /api/uploadposts/status``). Nothing about the API contract
is invented here.

The target channel is the YouTube account already authorized inside the
Upload-Post dashboard for ``UPLOAD_POST_USERNAME``. Credentials come only from
the environment (GitHub Actions secrets) and are never printed; YouTube
visibility comes from ``--privacy`` (default: private).

Exit codes:
    0 - Upload-Post confirmed the publish succeeded on YouTube.
    1 - Missing secrets, missing video file, or the upload API reported an
        error (annotated with ``::error::`` when running in GitHub Actions).
    2 - Invalid arguments (argparse).

Usage:
    UPLOAD_POST_API_KEY=... UPLOAD_POST_USERNAME=... \
    uv run python scripts/upload_short_youtube.py --privacy private
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fail(message: str) -> int:
    """Report a hard failure; fails the GitHub Actions step clearly."""
    log(message)
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::error::{message}", flush=True)
    return 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    # Direct script execution puts scripts/ (not the repo root) on sys.path.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.run_modern_facts_shorts import (
        DEFAULT_OUTPUT_DIR,
        DEFAULT_YOUTUBE_PRIVACY,
        YOUTUBE_PRIVACY_VALUES,
    )

    parser = argparse.ArgumentParser(
        description=(
            "Upload a generated Modern Facts MP4 to the authorized YouTube "
            "channel through Upload-Post's existing API integration."
        )
    )
    parser.add_argument(
        "--video",
        default="",
        help="exact video file to upload; skips newest-file discovery",
    )
    parser.add_argument(
        "--video-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=(
            "directory scanned for the newest generated MP4 when --video is "
            "not given (default: storage/modern_facts)"
        ),
    )
    parser.add_argument(
        "--privacy",
        choices=list(YOUTUBE_PRIVACY_VALUES),
        default=os.getenv("SHORTS_YOUTUBE_PRIVACY") or DEFAULT_YOUTUBE_PRIVACY,
        help=(
            "YouTube visibility passed as privacyStatus "
            f"(default: {DEFAULT_YOUTUBE_PRIVACY}; env: SHORTS_YOUTUBE_PRIVACY)"
        ),
    )
    return parser.parse_args(argv)


def load_upload_account() -> tuple[dict | None, str | None]:
    """Read the Upload-Post credentials from the environment.

    Only the NAMES of missing variables are ever reported; values are never
    logged, formatted, or written anywhere.
    """
    api_key = os.getenv("UPLOAD_POST_API_KEY", "").strip()
    username = os.getenv("UPLOAD_POST_USERNAME", "").strip()
    missing = [
        name
        for name, value in (
            ("UPLOAD_POST_API_KEY", api_key),
            ("UPLOAD_POST_USERNAME", username),
        )
        if not value
    ]
    if missing:
        return None, (
            "missing required environment variable(s): "
            + ", ".join(missing)
            + "; set them as GitHub Actions repository secrets "
            "(their values are never printed)"
        )
    return {
        "upload_post_api_key": api_key,
        "upload_post_username": username,
        "upload_post_enabled": True,
    }, None


def find_latest_video(video_dir: Path) -> Path | None:
    """Return the newest generated MP4 (the generator's output naming)."""
    try:
        videos = [path for path in video_dir.glob("*.mp4") if path.is_file()]
    except OSError:
        return None
    if not videos:
        return None
    return max(videos, key=lambda path: (path.stat().st_mtime, path.name))


def read_video_metadata(video_path: Path) -> dict:
    """Load the sibling ``*-meta.json`` written by ``collect_outputs``."""
    meta_path = video_path.with_name(video_path.stem + "-meta.json")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def upload_video(
    video_path: Path,
    *,
    title: str,
    description: str,
    privacy: str,
    account: dict,
) -> dict:
    """Call the existing Upload-Post service for a YouTube-only publish."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from app.services import upload_post

    youtube_extra: dict = {
        # Always explicit: the service's config fallback would otherwise
        # publish with the config file's visibility instead of the input.
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
        "youtube_title": title[:100],
    }
    if description:
        youtube_extra["youtube_description"] = description[:4900]

    return upload_post.cross_post_video(
        video_path=str(video_path),
        title=title,
        platforms=["youtube"],
        youtube_extra=youtube_extra,
        account=account,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    account, error = load_upload_account()
    if error:
        return fail(error)

    video_path: Path | None
    if args.video:
        video_path = Path(args.video)
        if not video_path.is_file():
            return fail(f"video file not found: {video_path}")
    else:
        video_dir = Path(args.video_dir)
        video_path = find_latest_video(video_dir)
        if video_path is None:
            return fail(
                f"no generated video (.mp4) found in {video_dir}; "
                "run scripts/run_modern_facts_shorts.py first"
            )

    metadata = read_video_metadata(video_path)
    title = str(metadata.get("subject") or video_path.stem).strip()
    title = title or "Modern Facts short"
    description = str(metadata.get("script") or "")

    log(
        f"uploading {video_path.name} to YouTube via Upload-Post "
        f"(privacy: {args.privacy})"
    )
    result = upload_video(
        video_path,
        title=title,
        description=description,
        privacy=args.privacy,
        account=account,
    )

    if not isinstance(result, dict) or result.get("success") is not True:
        detail = "Upload-Post returned an invalid response"
        request_id = None
        if isinstance(result, dict):
            request_id = result.get("request_id")
            detail = str(
                result.get("error") or result.get("message") or detail
            )
            if request_id:
                detail = f"{detail} (request_id {request_id})"
        return fail(f"YouTube upload failed: {detail}")

    request_id = result.get("request_id")
    print(
        json.dumps(
            {
                "status": "uploaded",
                "video": str(video_path),
                "privacy": args.privacy,
                "title": title,
                "request_id": request_id,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    log(f"YouTube upload succeeded: {video_path.name} (privacy: {args.privacy})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
