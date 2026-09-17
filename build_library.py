#!/usr/bin/env python3
"""Phase 4 — build a local content-production library from existing reel scrapes.

Uses on-disk Apify exports in original_data/. No new Apify calls.

Usage:
    python build_library.py
    python build_library.py --max-videos-per-account 5
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import google.generativeai as genai
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
POOL_PATH = ROOT / "driver_video_pool.txt"
LIBRARY_DIR = ROOT / "library"
INDEX_PATH = ROOT / "library_index.csv"
FAILED_PATH = ROOT / "failed_downloads.csv"
SKIPPED_PATH = ROOT / "skipped_no_data.csv"

VIDEOS_PER_ACCOUNT = 5
VIDEO_WORKERS = 3
GEMINI_DELAY_SECONDS = 3
GEMINI_TIMEOUT_SECONDS = 300
GEMINI_MODEL = "gemini-3.6-flash"
FRAME_COUNT = 5

FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
INSTAGRAM_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([^/?#]+)", re.IGNORECASE
)

GEMINI_PROMPT = (
    "You're analyzing a short-form video (Instagram Reel) to extract its "
    "content structure for a content-production reference library. Based on "
    "the frames, transcript, and caption provided, return ONLY valid JSON:\n"
    "{\n"
    '  "hook": "what happens/is said in the first 1-3 seconds to grab attention",\n'
    '  "core_action": "what the main action or activity in the video is",\n'
    '  "payoff_or_cta": "how the video resolves or what it asks the viewer to do",\n'
    '  "format_category": "a short label for this type of video, e.g. '
    "'GRWM confession', 'POV reveal', 'day-in-the-life', 'reaction bait', "
    'invent a label if none of these fit",\n'
    '  "variable_elements": "what specific details could be swapped to make '
    "a new video in the same format (e.g. the specific hook line, the "
    'specific twist, the emotional tone)"\n'
    "}"
)

INDEX_FIELDS = [
    "username",
    "video_filename",
    "local_path",
    "caption",
    "likesCount",
    "commentsCount",
    "videoViewCount",
    "hook",
    "core_action",
    "payoff_or_cta",
    "format_category",
    "variable_elements",
    "transcript_snippet",
    "tagging_status",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("library")
INDEX_LOCK = threading.Lock()
FAILED_LOCK = threading.Lock()
SKIPPED_LOCK = threading.Lock()
GEMINI_LOCK = threading.Lock()
TAGGING_PAUSED = threading.Event()


@dataclass
class Reel:
    username: str
    video_url: str
    caption: str
    likes_count: str
    comments_count: str
    view_count: float
    view_count_raw: str
    shortcode: str
    video_id: str
    source_file: str


def normalize_username(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip().lstrip("@").strip()
    match = INSTAGRAM_URL_RE.search(text)
    if match:
        text = match.group(1)
    text = text.split("/")[0].split("?")[0].strip()
    if text.lower() in {"p", "reel", "reels", "stories", "explore", "accounts"}:
        return ""
    return text.lower()


def clean_key(key: Any) -> str:
    return str(key or "").lstrip("\ufeff").strip().strip('"')


def as_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def find_original_data_dir() -> Path:
    env_dir = os.getenv("ORIGINAL_DATA_DIR", "").strip()
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates.extend(
        [
            ROOT / "original_data",
            Path("/Users/gayatriahi/Downloads/original_data"),
            Path.home() / "Downloads" / "original_data",
        ]
    )
    for path in candidates:
        if path.is_dir():
            return path
    sys.exit(
        "Could not find original_data/. Set ORIGINAL_DATA_DIR or place the folder "
        "next to this script or in Downloads."
    )


def load_pool(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"Missing {path}")
    names: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        username = normalize_username(line.split("#")[0])
        if not username or username in seen:
            continue
        seen.add(username)
        names.append(username)
    if not names:
        sys.exit(f"No usernames in {path}")
    return names


def strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = FENCE_RE.sub("", stripped).strip()
    return stripped


def dataset_files(data_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(data_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".csv", ".json"}:
            continue
        name = path.name.lower()
        if "seed" in name or "sheet" in name:
            continue
        files.append(path)
    if not files:
        sys.exit(f"No CSV/JSON dataset files in {data_dir}")
    return files


def reel_from_mapping(item: dict[str, Any], source: str, fallback_owner: str = "") -> Reel | None:
    if item.get("error"):
        return None
    video_url = str(item.get("videoUrl") or "").strip()
    if not video_url:
        return None
    owner = normalize_username(
        item.get("ownerUsername") or item.get("username") or fallback_owner
    )
    if not owner:
        return None
    views = as_number(item.get("videoViewCount"))
    if views is None:
        views = as_number(item.get("videoPlayCount")) or 0.0
    shortcode = str(item.get("shortCode") or item.get("shortcode") or "").strip()
    video_id = str(item.get("id") or "").strip()
    if not shortcode and not video_id:
        return None
    return Reel(
        username=owner,
        video_url=video_url,
        caption=str(item.get("caption") or ""),
        likes_count="" if item.get("likesCount") is None else str(item.get("likesCount")),
        comments_count=""
        if item.get("commentsCount") is None
        else str(item.get("commentsCount")),
        view_count=float(views),
        view_count_raw=""
        if item.get("videoViewCount") in (None, "")
        else str(item.get("videoViewCount")),
        shortcode=shortcode,
        video_id=video_id,
        source_file=source,
    )


def walk_json(obj: Any, source: str, fallback_owner: str = "") -> list[Reel]:
    found: list[Reel] = []
    if isinstance(obj, list):
        for item in obj:
            found.extend(walk_json(item, source, fallback_owner))
        return found
    if not isinstance(obj, dict):
        return found
    owner = normalize_username(obj.get("username") or obj.get("ownerUsername")) or fallback_owner
    reel = reel_from_mapping(obj, source, owner)
    if reel:
        found.append(reel)
    for key in ("latestPosts", "latestIgtvVideos"):
        children = obj.get(key)
        if isinstance(children, list):
            for child in children:
                found.extend(walk_json(child, f"{source}/{key}", owner))
    return found


def load_all_reels(files: list[Path]) -> list[Reel]:
    reels: list[Reel] = []
    for path in files:
        log.info("Loading %s", path.name)
        try:
            if path.suffix.lower() == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
                reels.extend(walk_json(data, path.name))
                continue
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for raw in reader:
                    row = {clean_key(key): value for key, value in raw.items()}
                    reel = reel_from_mapping(row, path.name)
                    if reel:
                        reels.append(reel)
        except Exception:
            log.exception("Failed to parse %s — skipping file", path.name)
    log.info("Loaded %d reel rows with videoUrl", len(reels))
    return reels


def dedupe_reels(reels: list[Reel]) -> list[Reel]:
    best: dict[str, Reel] = {}
    for reel in reels:
        key = reel.shortcode or reel.video_id
        current = best.get(key)
        if current is None or reel.view_count > current.view_count:
            best[key] = reel
    return list(best.values())


def select_for_pool(reels: list[Reel], pool: list[str], limit: int) -> dict[str, list[Reel]]:
    by_user: dict[str, list[Reel]] = {name: [] for name in pool}
    for reel in reels:
        if reel.username in by_user:
            by_user[reel.username].append(reel)
    selected: dict[str, list[Reel]] = {}
    for name in pool:
        items = sorted(by_user[name], key=lambda r: r.view_count, reverse=True)
        selected[name] = items[:limit]
        log.info("%s: %d matching reels, keeping %d", name, len(items), len(selected[name]))
    return selected


def already_indexed() -> set[tuple[str, str]]:
    if not INDEX_PATH.exists():
        return set()
    done: set[tuple[str, str]] = set()
    with INDEX_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            username = normalize_username(row.get("username"))
            filename = (row.get("video_filename") or "").strip()
            if username and filename:
                done.add((username, filename))
    return done


def ensure_csv(path: Path, fields: list[str]) -> None:
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=fields).writeheader()


def append_csv(path: Path, fields: list[str], row: dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        ensure_csv(path, fields)
        with path.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=fields).writerow(
                {key: row.get(key, "") for key in fields}
            )


def log_failed_download(username: str, url: str, error: str) -> None:
    append_csv(
        FAILED_PATH,
        ["username", "url", "error"],
        {"username": username, "url": url, "error": error},
        FAILED_LOCK,
    )
    log.warning("Download failed for @%s: %s", username, error)


def already_skipped() -> set[str]:
    if not SKIPPED_PATH.exists():
        return set()
    with SKIPPED_PATH.open(newline="", encoding="utf-8") as handle:
        return {
            normalize_username(row.get("username"))
            for row in csv.DictReader(handle)
            if row.get("username")
        }


def log_skipped_no_data(username: str, seen: set[str]) -> None:
    if username in seen:
        log.warning("No video data for @%s — already in %s", username, SKIPPED_PATH.name)
        return
    append_csv(
        SKIPPED_PATH,
        ["username", "reason"],
        {"username": username, "reason": "no_video_data"},
        SKIPPED_LOCK,
    )
    seen.add(username)
    log.warning("No video data for @%s — logged to %s", username, SKIPPED_PATH.name)


def is_daily_quota_error(exc: Exception) -> bool:
    text = str(exc)
    return "PerDay" in text or "generate_content_free_tier_requests" in text


def download_video(url: str, dest: Path) -> None:
    response = requests.get(
        url,
        timeout=90,
        stream=True,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if response.status_code in {403, 404}:
        raise RuntimeError(str(response.status_code))
    response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
    if dest.stat().st_size == 0:
        raise RuntimeError("empty file")


def video_duration_seconds(video_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(float(result.stdout.strip()), 0.1)
    except (TypeError, ValueError):
        return 8.0


def extract_frames(video_path: Path) -> list[Path]:
    stem = video_path.with_suffix("")
    duration = video_duration_seconds(video_path)
    frames: list[Path] = []
    for index in range(1, FRAME_COUNT + 1):
        timestamp = duration * index / (FRAME_COUNT + 1)
        frame_path = Path(f"{stem}_frame{index}.jpg")
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(frame_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and frame_path.exists() and frame_path.stat().st_size > 0:
            frames.append(frame_path)
    if len(frames) < 4:
        raise RuntimeError(f"only extracted {len(frames)} frames")
    return frames


def tag_with_gemini(
    model: genai.GenerativeModel,
    frames: list[Path],
    caption: str,
) -> dict[str, Any]:
    parts: list[Any] = [
        GEMINI_PROMPT
        + f"\n\nCaption:\n{caption or '(none)'}\n\nTranscript:\n(skipped)"
    ]
    for path in frames:
        parts.append({"mime_type": "image/jpeg", "data": path.read_bytes()})
    with GEMINI_LOCK:
        response = model.generate_content(
            parts,
            request_options={"timeout": GEMINI_TIMEOUT_SECONDS},
        )
        time.sleep(GEMINI_DELAY_SECONDS)
    text = strip_fences(getattr(response, "text", None) or "")
    if not text:
        raise ValueError("Gemini returned empty response")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Gemini JSON was not an object")
    return parsed


def tag_with_retry(
    model: genai.GenerativeModel,
    username: str,
    frames: list[Path],
    caption: str,
) -> dict[str, Any]:
    if TAGGING_PAUSED.is_set():
        raise RuntimeError("tagging_paused: gemini_daily_quota")
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return tag_with_gemini(model, frames, caption)
        except Exception as exc:
            last_error = exc
            log.warning(
                "%s tagging attempt %d failed: %s",
                username,
                attempt + 1,
                exc,
            )
            if is_daily_quota_error(exc):
                TAGGING_PAUSED.set()
                log.warning(
                    "Gemini daily quota reached — downloading remaining videos without tagging"
                )
                raise RuntimeError(f"tagging_failed: {exc}") from exc
            if attempt == 0:
                time.sleep(GEMINI_DELAY_SECONDS)
    raise RuntimeError(f"tagging_failed: {last_error}")


def process_video(
    reel: Reel,
    gemini: genai.GenerativeModel,
    done: set[tuple[str, str]],
) -> str:
    filename = f"{reel.shortcode or reel.video_id}.mp4"
    if (reel.username, filename) in done:
        return "skipped"
    account_dir = LIBRARY_DIR / reel.username
    account_dir.mkdir(parents=True, exist_ok=True)
    video_path = account_dir / filename
    if video_path.exists() and video_path.stat().st_size > 0:
        print(f"  already have {reel.username}/{filename}", flush=True)
    else:
        print(f"  downloading {reel.username}/{filename}", flush=True)
        try:
            download_video(reel.video_url, video_path)
        except Exception as exc:
            log_failed_download(reel.username, reel.video_url, str(exc))
            if video_path.exists():
                video_path.unlink()
            return "download_failed"

    try:
        tagging_status = "ok"
        tags: dict[str, Any] = {}
        if TAGGING_PAUSED.is_set():
            tagging_status = "tagging_skipped_quota"
        else:
            frames = extract_frames(video_path)
            try:
                tags = tag_with_retry(gemini, reel.username, frames, reel.caption)
            except Exception:
                tagging_status = (
                    "tagging_skipped_quota"
                    if TAGGING_PAUSED.is_set()
                    else "tagging_failed"
                )
                log.warning("%s for %s/%s", tagging_status, reel.username, filename)
        views_out = reel.view_count_raw if reel.view_count_raw else (
            "" if reel.view_count == 0 else str(int(reel.view_count))
        )
        append_csv(
            INDEX_PATH,
            INDEX_FIELDS,
            {
                "username": reel.username,
                "video_filename": filename,
                "local_path": str(video_path.relative_to(ROOT)),
                "caption": reel.caption,
                "likesCount": reel.likes_count,
                "commentsCount": reel.comments_count,
                "videoViewCount": views_out,
                "hook": tags.get("hook", ""),
                "core_action": tags.get("core_action", ""),
                "payoff_or_cta": tags.get("payoff_or_cta", ""),
                "format_category": tags.get("format_category", ""),
                "variable_elements": tags.get("variable_elements", ""),
                "transcript_snippet": "",
                "tagging_status": tagging_status,
            },
            INDEX_LOCK,
        )
        return tagging_status
    except Exception:
        log.exception("Processing failed after download for %s/%s", reel.username, filename)
        return "process_failed"


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 4 library builder")
    parser.add_argument("--max-videos-per-account", type=int, default=VIDEOS_PER_ACCOUNT)
    parser.add_argument("--pool", type=Path, default=POOL_PATH)
    parser.add_argument(
        "--skip-tagging",
        action="store_true",
        help="Download videos only; skip Gemini structure tagging",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        sys.exit("Missing GEMINI_API_KEY in .env")

    data_dir = find_original_data_dir()
    pool = load_pool(args.pool)
    files = dataset_files(data_dir)
    log.info("Using original_data at %s (%d files)", data_dir, len(files))

    reels = dedupe_reels(load_all_reels(files))
    selected = select_for_pool(reels, pool, args.max_videos_per_account)
    done = already_indexed()
    skipped = already_skipped()
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

    genai.configure(api_key=api_key)
    gemini = genai.GenerativeModel(GEMINI_MODEL)
    if args.skip_tagging:
        TAGGING_PAUSED.set()
        log.info("Gemini tagging disabled for this run")

    total_accounts = len(pool)
    for index, username in enumerate(pool, start=1):
        videos = selected.get(username) or []
        print(
            f"Account {index}/{total_accounts}: {username} ({len(videos)} videos)",
            flush=True,
        )
        if not videos:
            log_skipped_no_data(username, skipped)
            continue
        with ThreadPoolExecutor(max_workers=VIDEO_WORKERS) as pool_exec:
            futures = [
                pool_exec.submit(process_video, reel, gemini, done) for reel in videos
            ]
            for future in as_completed(futures):
                future.result()

    log.info("Library index: %s", INDEX_PATH)
    log.info("Failed downloads: %s", FAILED_PATH)
    log.info("Skipped (no data): %s", SKIPPED_PATH)


if __name__ == "__main__":
    main()
