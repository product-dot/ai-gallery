#!/usr/bin/env python3
"""Discover Instagram candidates from seed following lists, then score eligibility.

Stage 1 (DISCOVERY) expands already-vetted seeds into a new candidate set.
Stage 2 (ELIGIBILITY) runs only on those new candidates.

Usage:
    python pipeline.py
    python pipeline.py --discovery-only
    python pipeline.py --eligibility-only
    python pipeline.py --max-candidates 20
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import google.generativeai as genai
import requests
from apify_client import ApifyClient
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
SEEDS_PATH = ROOT / "seeds.txt"
CANDIDATES_RAW_PATH = ROOT / "candidates_raw.csv"
RESULTS_PATH = ROOT / "results.csv"
TMP_DIR = ROOT / "tmp_media"

FOLLOWING_ACTOR = "apify/instagram-followers-following-scraper"
PROFILE_ACTOR = "apify/instagram-scraper"
FOLLOWING_LIMIT = 100
REEL_LIMIT = 20
MIN_REELS = 5
SANITY_MIN_FOLLOWERS = 1_000
SANITY_MAX_FOLLOWERS = 5_000_000
SANITY_MIN_POSTS = 10
APIFY_DELAY_SECONDS = 2
GEMINI_DELAY_SECONDS = 6
GEMINI_TIMEOUT_SECONDS = 300
GEMINI_RETRY_BASE_SECONDS = 6
GEMINI_MODEL = "gemini-3.6-flash"
ELIGIBILITY_WORKERS = 10
GEMINI_CONCURRENCY = 4
HIDDEN_LIKES = -1
PASS_BAND = 0.03
LIFESTYLE_NICHE = "lifestyle_beauty"

INSTAGRAM_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([^/?#]+)", re.IGNORECASE
)
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

GEMINI_PROMPT = (
    "You're evaluating Instagram content frames for three things:\n"
    "1. Human-likeness: rate 1-10 how convincingly the person passes as a real "
    "human on a quick scroll (facial consistency, absence of AI artifacts "
    "in hands/background/lighting/skin texture).\n"
    "2. AI-origin signals: note any visible signs this content was "
    "AI-generated, separate from the human-likeness score.\n"
    "3. Content niche: classify this content into ONE of these categories: "
    "'lifestyle_beauty' (GRWM, POV, day-in-the-life, beauty/fashion, "
    "flirty/lifestyle content typically aimed at adult-platform funnels), "
    "'fitness', 'sports', 'gaming', 'comedy', 'other'. Base this on what "
    "the content actually shows, not the caption alone.\n"
    "\n"
    'Return ONLY valid JSON: {"human_likeness_score": int, '
    '"ai_origin_likely": bool, "content_niche": string, "reasoning": string}'
)

RESULTS_FIELDS = [
    "username",
    "followers",
    "tier",
    "like_ratio_percent",
    "comment_ratio_percent",
    "median_views",
    "hidden_like_count",
    "result",
    "human_likeness_score",
    "ai_origin_likely",
    "content_niche",
    "reasoning",
    "exclusion_reason",
    "discovered_from_seed",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline")
RESULTS_LOCK = threading.Lock()
GEMINI_SEMAPHORE = threading.Semaphore(GEMINI_CONCURRENCY)


@dataclass
class Config:
    apify_token: str
    gemini_key: str


@dataclass
class Candidate:
    username: str
    source_seeds: list[str] = field(default_factory=list)


@dataclass
class Reel:
    likes_count: float | None
    comments_count: float | None
    video_view_count: float | None
    video_url: str


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    token = os.getenv("APIFY_API_TOKEN", "").strip()
    gemini = os.getenv("GEMINI_API_KEY", "").strip()
    if not token:
        sys.exit("Missing APIFY_API_TOKEN in .env")
    if not gemini:
        sys.exit("Missing GEMINI_API_KEY in .env")
    return Config(apify_token=token, gemini_key=gemini)


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


def profile_url(username: str) -> str:
    return f"https://www.instagram.com/{username}/"


def discovered_from_seed(candidate: Candidate) -> str:
    return "|".join(candidate.source_seeds)


def load_seeds(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"Missing input file: {path}")
    seeds: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        username = normalize_username(line)
        if not username or username in seen:
            continue
        seen.add(username)
        seeds.append(username)
    if not seeds:
        sys.exit(f"No usernames found in {path}")
    return seeds


def extract_username_from_item(item: dict[str, Any]) -> str:
    for key in ("username", "userName", "ownerUsername", "user_name"):
        username = normalize_username(item.get(key))
        if username:
            return username
    for key in ("url", "profileUrl", "profile_url", "inputUrl"):
        username = normalize_username(item.get(key))
        if username:
            return username
    return ""


def as_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def ffmpeg_available() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            check=True,
            capture_output=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def run_actor(
    client: ApifyClient,
    actor_id: str,
    run_input: dict[str, Any],
    timeout_secs: int = 600,
) -> list[dict[str, Any]]:
    log.info("Starting actor %s", actor_id)
    run = client.actor(actor_id).call(
        run_input=run_input,
        run_timeout=timedelta(seconds=timeout_secs),
        wait_duration=timedelta(seconds=timeout_secs),
    )
    time.sleep(APIFY_DELAY_SECONDS)
    if not run:
        log.warning("Actor %s returned no run object", actor_id)
        return []
    dataset_id = getattr(run, "default_dataset_id", None)
    if not dataset_id:
        log.warning("Actor %s run had no dataset", actor_id)
        return []
    items = list(client.dataset(dataset_id).iterate_items())
    log.info("Actor %s returned %d items", actor_id, len(items))
    return items


def scrape_following(client: ApifyClient, username: str) -> list[dict[str, Any]]:
    return run_actor(
        client,
        FOLLOWING_ACTOR,
        {
            "usernames": [username],
            "dataToScrape": "following",
            "resultsLimit": FOLLOWING_LIMIT,
        },
    )


def scrape_profile(client: ApifyClient, username: str) -> dict[str, Any] | None:
    items = run_actor(
        client,
        PROFILE_ACTOR,
        {
            "directUrls": [profile_url(username)],
            "resultsType": "details",
        },
    )
    if not items:
        return None
    for item in items:
        extracted = extract_username_from_item(item)
        if extracted == username or not extracted:
            return item
    return items[0]


def scrape_reels(client: ApifyClient, username: str) -> list[dict[str, Any]]:
    return run_actor(
        client,
        PROFILE_ACTOR,
        {
            "directUrls": [profile_url(username)],
            "resultsType": "reels",
            "resultsLimit": REEL_LIMIT,
        },
    )


# ---------------------------------------------------------------------------
# Stage 1 — DISCOVERY
# ---------------------------------------------------------------------------


def discover_candidates(client: ApifyClient, seeds: list[str]) -> dict[str, Candidate]:
    seed_set = set(seeds)
    candidates: dict[str, Candidate] = {}

    for index, seed in enumerate(seeds, start=1):
        log.info("[%d/%d] Discovery: following list for @%s", index, len(seeds), seed)
        try:
            items = scrape_following(client, seed)
        except Exception:
            log.exception("Following scrape failed for @%s — skipping seed", seed)
            continue

        added = 0
        for item in items:
            if item.get("error"):
                continue
            item_type = str(item.get("type") or "").upper()
            if item_type and item_type not in {"FOLLOWING", ""}:
                continue
            username = extract_username_from_item(item)
            if not username or username in seed_set:
                continue
            candidate = candidates.get(username)
            if candidate is None:
                candidate = Candidate(username=username)
                candidates[username] = candidate
                added += 1
            if seed not in candidate.source_seeds:
                candidate.source_seeds.append(seed)
        log.info("  @%s contributed %d new unique candidates", seed, added)

    return candidates


def write_candidates_raw(candidates: dict[str, Candidate], path: Path) -> None:
    rows = sorted(candidates.values(), key=lambda c: c.username)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["username", "discovered_from_seed"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "username": row.username,
                    "discovered_from_seed": discovered_from_seed(row),
                }
            )
    log.info("Wrote %d candidates to %s", len(rows), path.name)


def read_candidates_raw(path: Path) -> dict[str, Candidate]:
    if not path.exists():
        sys.exit(f"Missing {path.name}. Run discovery first.")
    candidates: dict[str, Candidate] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            username = normalize_username(row.get("username"))
            if not username:
                continue
            seeds = [
                s
                for s in (row.get("discovered_from_seed") or "").split("|")
                if s
            ]
            candidates[username] = Candidate(username=username, source_seeds=seeds)
    return candidates


# ---------------------------------------------------------------------------
# Stage 2 — ELIGIBILITY
# ---------------------------------------------------------------------------


def is_restricted_error(error: Any) -> bool:
    return str(error or "").strip().lower() == "restricted profile"


def profile_error(profile: dict[str, Any] | None) -> Any:
    if not profile:
        return None
    return profile.get("error")


def followers_count(profile: dict[str, Any] | None) -> int | None:
    if not profile:
        return None
    value = as_number(profile.get("followersCount"))
    return None if value is None else int(value)


def posts_count(profile: dict[str, Any] | None) -> int | None:
    if not profile:
        return None
    value = as_number(profile.get("postsCount"))
    return None if value is None else int(value)


def classify_tier(followers: int) -> tuple[str, float]:
    """Map followersCount to spec tiers.

    Boundary choice (flagged below): 150,000 and 500,000 go to the later-listed
    inclusive range that starts at that number; 1,000,000 stays in large because
    mega is defined as > 1,000,000.
    """
    if followers < 150_000:
        return "small", 0.15
    if followers <= 500_000:
        return "mid", 0.10
    if followers <= 1_000_000:
        return "large", 0.15
    return "mega", 0.15


def parse_reels(items: list[dict[str, Any]]) -> list[Reel]:
    reels: list[Reel] = []
    for item in items:
        if item.get("error"):
            continue
        video_url = str(item.get("videoUrl") or "").strip()
        view_count = None
        for key in ("videoViewCount", "videoPlayCount", "playCount", "viewCount"):
            view_count = as_number(item.get(key))
            if view_count is not None:
                break
        reels.append(
            Reel(
                likes_count=as_number(item.get("likesCount")),
                comments_count=as_number(item.get("commentsCount")),
                video_view_count=view_count,
                video_url=video_url,
            )
        )
        if len(reels) >= REEL_LIMIT:
            break
    return reels


def engagement_result(like_ratio_percent: float, floor: float) -> str:
    if like_ratio_percent >= floor + PASS_BAND:
        return "pass"
    if like_ratio_percent <= floor - PASS_BAND:
        return "fail"
    return "borderline_review"


def blank_row(candidate: Candidate) -> dict[str, Any]:
    return {key: "" for key in RESULTS_FIELDS} | {
        "username": candidate.username,
        "discovered_from_seed": discovered_from_seed(candidate),
    }


def already_written_usernames(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            username = normalize_username(row.get("username"))
            if username:
                done.add(username)
    return done


def ensure_results_csv(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=RESULTS_FIELDS).writeheader()
        return
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        old_fields = list(reader.fieldnames or [])
        rows = list(reader)
    if old_fields == RESULTS_FIELDS:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULTS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in RESULTS_FIELDS})


def write_result(row: dict[str, Any]) -> None:
    with RESULTS_LOCK:
        ensure_results_csv(RESULTS_PATH)
        with RESULTS_PATH.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=RESULTS_FIELDS).writerow(
                {key: row.get(key, "") for key in RESULTS_FIELDS}
            )


def lookup_profile(client: ApifyClient, username: str) -> dict[str, Any] | None:
    profile = scrape_profile(client, username)
    if profile is not None and is_restricted_error(profile_error(profile)):
        return profile
    if followers_count(profile) is None and not profile_error(profile):
        log.info("  followersCount missing for @%s — retrying once", username)
        profile = scrape_profile(client, username)
    return profile


def download_video(url: str, dest: Path) -> None:
    response = requests.get(
        url,
        timeout=90,
        stream=True,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    with dest.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
    if dest.stat().st_size == 0:
        raise RuntimeError(f"Empty download: {url}")


def extract_frames(video_path: Path, out_dir: Path, prefix: str) -> list[Path]:
    frames: list[Path] = []
    timestamps = [2, 4, 6]
    for index, timestamp in enumerate(timestamps, start=1):
        dest = out_dir / f"{prefix}_{index}.jpg"
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dest),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            frames.append(dest)
    return frames


def strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = FENCE_RE.sub("", stripped).strip()
    return stripped


def gemini_error_label(exc: BaseException) -> str:
    error_type = type(exc).__name__
    code = getattr(exc, "code", None)
    if code is not None:
        return f"{error_type}/{code}"
    return error_type


def gemini_score_frames(model: genai.GenerativeModel, frame_paths: list[Path]) -> dict[str, Any]:
    parts: list[Any] = [GEMINI_PROMPT]
    for path in frame_paths:
        parts.append({"mime_type": "image/jpeg", "data": path.read_bytes()})
    with GEMINI_SEMAPHORE:
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


def run_human_likeness_once(
    model: genai.GenerativeModel,
    username: str,
    reels: list[Reel],
) -> dict[str, Any]:
    work_dir = TMP_DIR / username
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_urls = [reel.video_url for reel in reels if reel.video_url][:2]
        if len(video_urls) < 2:
            raise RuntimeError("Fewer than 2 reels had a videoUrl")

        frame_paths: list[Path] = []
        for index, url in enumerate(video_urls, start=1):
            video_path = work_dir / f"reel_{index}.mp4"
            download_video(url, video_path)
            frames = extract_frames(video_path, work_dir, f"reel_{index}")
            if len(frames) < 3:
                raise RuntimeError(
                    f"ffmpeg extracted {len(frames)} frames from video {index}, expected 3"
                )
            frame_paths.extend(frames[:3])

        if len(frame_paths) != 6:
            raise RuntimeError(f"Expected 6 frames, got {len(frame_paths)}")
        return gemini_score_frames(model, frame_paths)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_human_likeness_check(
    model: genai.GenerativeModel,
    username: str,
    reels: list[Reel],
) -> dict[str, Any]:
    last_error: Exception | None = None
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            return run_human_likeness_once(model, username, reels)
        except Exception as exc:
            last_error = exc
            error_type = gemini_error_label(exc)
            print(
                f"{username}: Gemini error — {error_type}: {exc}",
                flush=True,
            )
            if attempt + 1 < max_attempts:
                wait_seconds = GEMINI_RETRY_BASE_SECONDS * (2**attempt)
                log.warning(
                    "Step 6 attempt %d failed for @%s (%s); retrying in %ss",
                    attempt + 1,
                    username,
                    error_type,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
    raise RuntimeError(f"scoring_failed: {last_error}")


def score_engagement(
    username: str, followers: int, reels: list[Reel]
) -> dict[str, Any]:
    hidden_like_count = sum(1 for reel in reels if reel.likes_count == HIDDEN_LIKES)
    comment_values = [
        reel.comments_count for reel in reels if reel.comments_count is not None
    ]
    view_values = [
        reel.video_view_count
        for reel in reels
        if reel.video_view_count is not None
    ]
    comment_median = median(comment_values)
    median_views = median(view_values)
    print(f"{username}: median_views = {median_views}", flush=True)
    comment_ratio = (
        (comment_median / followers) * 100 if comment_median is not None else None
    )

    payload = {
        "hidden_like_count": hidden_like_count,
        "like_ratio_percent": None,
        "comment_ratio_percent": comment_ratio,
        "median_views": median_views,
        "result": None,
    }

    if hidden_like_count > len(reels) / 2:
        payload["result"] = "flagged_review_hidden_likes"
        return payload

    like_values = [
        reel.likes_count
        for reel in reels
        if reel.likes_count is not None and reel.likes_count != HIDDEN_LIKES
    ]
    like_median = median(like_values)
    payload["like_ratio_percent"] = (
        (like_median / followers) * 100 if like_median is not None else None
    )
    return payload


def apply_niche_filter(row: dict[str, Any]) -> None:
    niche = (
        str(row.get("content_niche") or "")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )
    row["content_niche"] = niche
    if niche != LIFESTYLE_NICHE:
        row["exclusion_reason"] = "excluded_off_niche"


def process_candidate(
    client: ApifyClient,
    model: genai.GenerativeModel,
    candidate: Candidate,
) -> dict[str, Any]:
    username = candidate.username
    row = blank_row(candidate)

    profile = lookup_profile(client, username)
    error = profile_error(profile)
    if is_restricted_error(error):
        row["exclusion_reason"] = "excluded_restricted"
        return row

    followers = followers_count(profile)
    if followers is None and not error:
        row["exclusion_reason"] = "excluded_empty"
        return row
    if followers is None:
        row["exclusion_reason"] = "excluded_empty"
        return row

    posts = posts_count(profile)
    row["followers"] = followers
    if (
        followers < SANITY_MIN_FOLLOWERS
        or followers > SANITY_MAX_FOLLOWERS
        or posts is None
        or posts < SANITY_MIN_POSTS
    ):
        row["exclusion_reason"] = "excluded_out_of_range"
        return row

    biography = (profile or {}).get("biography") or ""
    log.info("  stored followersCount=%s biography_chars=%d", followers, len(str(biography)))

    tier, floor = classify_tier(followers)
    row["tier"] = tier

    reel_items = scrape_reels(client, username)
    reels = parse_reels(reel_items)
    if len(reels) < MIN_REELS:
        row["exclusion_reason"] = "excluded_thin_sample"
        return row

    engagement = score_engagement(username, followers, reels)
    row["hidden_like_count"] = engagement["hidden_like_count"]
    row["comment_ratio_percent"] = (
        "" if engagement["comment_ratio_percent"] is None else engagement["comment_ratio_percent"]
    )
    row["median_views"] = (
        "" if engagement["median_views"] is None else engagement["median_views"]
    )

    if engagement["result"] == "flagged_review_hidden_likes":
        row["result"] = "flagged_review_hidden_likes"
        return row

    like_ratio = engagement["like_ratio_percent"]
    if like_ratio is None:
        row["result"] = "flagged_review_hidden_likes"
        return row

    row["like_ratio_percent"] = like_ratio
    result = engagement_result(like_ratio, floor)
    row["result"] = result

    if result != "pass":
        return row

    try:
        gemini = run_human_likeness_check(model, username, reels)
        row["human_likeness_score"] = gemini.get("human_likeness_score", "")
        row["ai_origin_likely"] = gemini.get("ai_origin_likely", "")
        row["content_niche"] = gemini.get("content_niche", "")
        row["reasoning"] = gemini.get("reasoning", "")
        apply_niche_filter(row)
    except Exception as exc:
        log.warning("scoring_failed for @%s: %s", username, exc)
        row["reasoning"] = "scoring_failed"
    return row


def run_eligibility(
    token: str,
    model: genai.GenerativeModel,
    candidates: dict[str, Candidate],
    max_candidates: int | None,
) -> None:
    if not ffmpeg_available():
        sys.exit("ffmpeg is required for Step 6 frame extraction but was not found on PATH")

    pending = [
        name
        for name in sorted(candidates)
        if name not in already_written_usernames(RESULTS_PATH)
    ]
    if max_candidates is not None:
        pending = pending[:max_candidates]
    if not pending:
        log.info("No new candidates left to score.")
        return

    total = len(pending)
    with RESULTS_LOCK:
        ensure_results_csv(RESULTS_PATH)
    log.info(
        "Eligibility: scoring %d new candidates (%d Apify workers, %d Gemini slots)",
        total,
        ELIGIBILITY_WORKERS,
        GEMINI_CONCURRENCY,
    )
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    progress_lock = threading.Lock()
    completed = 0
    in_progress = 0

    def worker(username: str) -> str:
        nonlocal completed, in_progress
        with progress_lock:
            in_progress += 1
        client = ApifyClient(token)
        try:
            row = process_candidate(client, model, candidates[username])
        except Exception:
            log.exception("Unhandled error for @%s", username)
            row = blank_row(candidates[username])
            row["reasoning"] = "processing_failed"
        write_result(row)
        with progress_lock:
            in_progress -= 1
            completed += 1
            print(
                f"Completed {completed}/{total} ({in_progress} in progress)",
                flush=True,
            )
        return username

    try:
        with ThreadPoolExecutor(max_workers=ELIGIBILITY_WORKERS) as pool:
            futures = [pool.submit(worker, username) for username in pending]
            for future in as_completed(futures):
                future.result()
    finally:
        if TMP_DIR.exists() and not any(TMP_DIR.iterdir()):
            TMP_DIR.rmdir()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and score Instagram candidates"
    )
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--eligibility-only", action="store_true")
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.discovery_only and args.eligibility_only:
        sys.exit("Use only one of --discovery-only / --eligibility-only")

    cfg = load_config()
    apify = ApifyClient(cfg.apify_token)
    seeds = load_seeds(args.seeds)
    log.info("Loaded %d seeds from %s", len(seeds), args.seeds.name)

    if not args.eligibility_only:
        candidates = discover_candidates(apify, seeds)
        write_candidates_raw(candidates, CANDIDATES_RAW_PATH)
        log.info("Discovery complete: %d new candidates", len(candidates))
    else:
        candidates = read_candidates_raw(CANDIDATES_RAW_PATH)
        log.info(
            "Loaded %d candidates from %s",
            len(candidates),
            CANDIDATES_RAW_PATH.name,
        )

    if args.discovery_only:
        return

    genai.configure(api_key=cfg.gemini_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    run_eligibility(cfg.apify_token, model, candidates, args.max_candidates)
    log.info("Eligibility complete. See %s", RESULTS_PATH.name)


if __name__ == "__main__":
    main()
