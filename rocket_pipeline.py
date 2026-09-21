#!/usr/bin/env python3
"""Discover Instagram candidates from seed following lists, then score eligibility.

RocketAPI only. No vision model, no Playwright, no video files in the GUI.

Usage:
    python rocket_pipeline.py
    python rocket_pipeline.py --discovery-only
    python rocket_pipeline.py --eligibility-only
    python rocket_pipeline.py --gui-only
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rocketapi import InstagramAPI
from rocketapi.exceptions import BadResponseException, NotFoundException

ROOT = Path(__file__).resolve().parent
SEEDS_PATH = ROOT / "seeds.txt"
CANDIDATES_PATH = ROOT / "candidates_discovered.csv"
ELIGIBLE_PATH = ROOT / "eligible_accounts.csv"
NEEDS_JS_PATH = ROOT / "needs_js_render.csv"
MANUAL_EXCLUDES_PATH = ROOT / "excluded_manual.txt"
HTML_PATH = ROOT / "public" / "visual_library.html"
WEEKS_DIR = ROOT / "data" / "weeks"

FOLLOWING_LIMIT = 100
MEDIA_PAGE_SIZE = 12
TARGET_MEDIA_COUNT = 20
MIN_REELS = 3
TARGET_QUALIFYING = 40  # weekly selected reels, including seeds
MIN_FOLLOWERS = 10_000  # non-seeds; 1k-follower pages are not usable
MIN_REEL_VIEWS = 10_000  # non-seeds; 4k-view reels are not in-range
DELAY_SECONDS = 1.5
PAGE_DELAY_SECONDS = 0.75
EMPTY_TEXT_CHARS = 200
EMPTY_HTML_CHARS = 1500

BIO_KEYWORDS = ("highlight", "virtual", "check my", "link in", "vip")
ADULT_DOMAINS = ("onlyfans.com", "fanvue.com", "fansly.com", "loyalfans.com")
COUPLE_RE = re.compile(
    r"\b(couples?|husband|wife|boyfriend|girlfriend|fianc[eé]|hubby|wifey|"
    r"my man|my girl|our page)\b",
    re.IGNORECASE,
)
ONE_PERSON_RE = re.compile(r"\b(?:1|one)\s+person\b", re.IGNORECASE)
MULTI_PEOPLE_RE = re.compile(r"\b(?:[2-9]|[1-9]\d+)\s+people\b", re.IGNORECASE)
PEOPLE_RE = re.compile(r"\bpeople\b", re.IGNORECASE)
TEXT_SAYS_RE = re.compile(r"text that says", re.IGNORECASE)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COLLAB_LIST_FIELDS = ("coauthor_producers", "invited_coauthor_producers")
_COLLAB_FIELDS_LOGGED = False

OUTPUT_FIELDS = [
    "username",
    "followers",
    "bio",
    "external_url",
    "niche_confirmed",
    "selected_shortcode",
    "caption",
    "likesCount",
    "commentsCount",
    "viewCount",
    "accessibility_caption",
    "is_collab",
    "selection_note",
    "reels_checked_before_pass",
    "exclusion_reason",
    "profile_pic_url",
    "thumbnail_url",
    "thumbnail_path",
]

WEEK_LINK_FIELDS = [
    "username",
    "followers",
    "bio",
    "external_url",
    "niche_confirmed",
    "selected_shortcode",
    "caption",
    "likesCount",
    "commentsCount",
    "viewCount",
    "accessibility_caption",
    "is_collab",
    "selection_note",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--eligibility-only", action="store_true")
    parser.add_argument("--gui-only", action="store_true")
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS)
    parser.add_argument("--target", type=int, default=TARGET_QUALIFYING)
    parser.add_argument("--max-seeds", type=int, default=0)
    parser.add_argument(
        "--no-refresh-seeds",
        action="store_true",
        help="Do not re-fetch top reels for seed accounts already in the CSV.",
    )
    return parser.parse_args()


def load_api() -> InstagramAPI:
    load_dotenv(ROOT / ".env")
    token = os.getenv("ROCKETAPI_TOKEN", "").strip()
    if not token:
        sys.exit("Missing ROCKETAPI_TOKEN in .env")
    return InstagramAPI(token=token, max_timeout=60)


def normalize_username(raw: Any) -> str:
    text = str(raw or "").strip().lstrip("@").split("?")[0].strip()
    text = text.split("/")[0].strip()
    if not text or text.lower() in {"p", "reel", "reels", "stories", "explore", "accounts"}:
        return ""
    return text.lower()


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


def nested_get(data: Any, *paths: tuple[str, ...], default: Any = None) -> Any:
    for path in paths:
        current = data
        matched = True
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                matched = False
                break
        if matched:
            return current
    return default


def extract_user(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return {}
    user = nested_get(profile, ("data", "user"), ("user",), default=profile)
    return user if isinstance(user, dict) else {}


def describe_last_response(api: InstagramAPI) -> str:
    raw = api.last_response if isinstance(api.last_response, dict) else {}
    response = raw.get("response") if isinstance(raw.get("response"), dict) else {}
    parts = [
        f"rocket={raw.get('status')}",
        f"message={raw.get('message')}",
        f"ig={response.get('status_code')}",
    ]
    return " ".join(str(part) for part in parts if part)


def fetch_profile(api: InstagramAPI, username: str) -> dict[str, Any]:
    try:
        return api.get_web_profile_info(username)
    except (BadResponseException, NotFoundException) as first:
        first_detail = describe_last_response(api)
        time.sleep(PAGE_DELAY_SECONDS)
        try:
            return api.get_user_info_by_username(username)
        except (BadResponseException, NotFoundException) as second:
            raise BadResponseException(
                f"{second} [{describe_last_response(api)}; web: {first}: {first_detail}]"
            ) from second


def user_id_of(user: dict[str, Any]) -> int | None:
    for key in ("pk", "id", "pk_id"):
        value = user.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def profile_status_reason(user: dict[str, Any]) -> str | None:
    if user.get("is_private") is True:
        return "private"
    if user.get("is_restricted") is True:
        return "restricted"
    if user.get("is_restricted_profile") is True:
        return "restricted"
    if user.get("restricted_by_viewer") not in (None, False):
        return "restricted"
    return None


def followers_of(user: dict[str, Any]) -> Any:
    return nested_get(
        user,
        ("edge_followed_by", "count"),
        ("follower_count",),
        default="",
    )


def profile_pic_of(user: dict[str, Any]) -> str:
    for key in ("profile_pic_url_hd", "profile_pic_url", "hd_profile_pic_url_info"):
        value = user.get(key)
        if isinstance(value, dict):
            url = value.get("url")
            if url:
                return str(url)
        elif value:
            return str(value)
    return ""


def external_url_of(user: dict[str, Any]) -> str:
    url = str(user.get("external_url") or "").strip()
    if url:
        return url
    links = user.get("bio_links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("url"):
                return str(link["url"]).strip()
    return ""


def following_usernames(payload: Any) -> tuple[list[str], str | None]:
    users: list[Any] = []
    next_max_id = None
    if isinstance(payload, dict):
        raw = payload.get("users") or payload.get("items") or []
        if isinstance(raw, list):
            users = raw
        next_max_id = payload.get("next_max_id")
    found: list[str] = []
    seen: set[str] = set()
    for item in users:
        if not isinstance(item, dict):
            continue
        username = normalize_username(item.get("username") or item.get("userName"))
        if not username or username in seen:
            continue
        seen.add(username)
        found.append(username)
    if next_max_id in (None, "", 0, "0"):
        return found, None
    return found, str(next_max_id)


def fetch_following(api: InstagramAPI, user_id: int) -> list[str]:
    collected: list[str] = []
    seen: set[str] = set()
    max_id = None
    while len(collected) < FOLLOWING_LIMIT:
        remaining = FOLLOWING_LIMIT - len(collected)
        payload = api.get_user_following(user_id, count=min(remaining, FOLLOWING_LIMIT), max_id=max_id)
        names, next_max_id = following_usernames(payload)
        for username in names:
            if username in seen:
                continue
            seen.add(username)
            collected.append(username)
            if len(collected) >= FOLLOWING_LIMIT:
                break
        if not next_max_id or next_max_id == max_id:
            break
        max_id = next_max_id
        if len(collected) < FOLLOWING_LIMIT:
            time.sleep(PAGE_DELAY_SECONDS)
    return collected[:FOLLOWING_LIMIT]


def extract_media_items(media: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(media, dict):
        return [], None
    items = media.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)], media.get("next_max_id")
    return [], media.get("next_max_id")


def is_video_post(item: dict[str, Any]) -> bool:
    if item.get("product_type") == "clips":
        return True
    if item.get("media_type") == 2:
        return True
    versions = item.get("video_versions")
    return isinstance(versions, list) and bool(versions)


def caption_text(item: dict[str, Any]) -> str:
    caption = item.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "")
    if isinstance(caption, str):
        return caption
    return ""


def as_int(value: Any) -> int:
    text = str(value or "").strip().replace(",", "")
    try:
        return int(float(text)) if text else 0
    except ValueError:
        return 0


def view_count(item: dict[str, Any]) -> int:
    for key in ("play_count", "ig_play_count", "view_count", "video_view_count"):
        if item.get(key) is not None:
            return as_int(item.get(key))
    return 0


def media_code(item: dict[str, Any]) -> str:
    return str(item.get("code") or item.get("shortcode") or "").strip()


def thumbnail_url_of(item: Any) -> str:
    """Poster still from image_versions2 — not video_versions / videoUrl."""
    if not isinstance(item, dict):
        return ""
    versions = item.get("image_versions2")
    if isinstance(versions, dict):
        extra = versions.get("additional_candidates")
        if isinstance(extra, dict):
            for key in ("first_frame", "igtv_first_frame"):
                frame = extra.get(key)
                if isinstance(frame, dict) and frame.get("url"):
                    return str(frame["url"])
        candidates = versions.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("url"):
                    return str(candidate["url"])
    for wrapper in ("media", "item"):
        nested = item.get(wrapper)
        if isinstance(nested, dict):
            found = thumbnail_url_of(nested)
            if found:
                return found
    items = item.get("items")
    if isinstance(items, list) and items:
        found = thumbnail_url_of(items[0])
        if found:
            return found
    return ""


def thumbnail_for_reel(item: dict[str, Any]) -> str:
    """CDN poster URL from the media payload. No local download."""
    return thumbnail_url_of(item)


def find_accessibility_caption(item: Any) -> str:
    if isinstance(item, dict):
        for key in (
            "accessibility_caption",
            "accessibilityCaption",
            "accessibility_caption_text",
        ):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in item.values():
            found = find_accessibility_caption(value)
            if found:
                return found
    elif isinstance(item, list):
        for value in item:
            found = find_accessibility_caption(value)
            if found:
                return found
    return ""


def collect_accessibility_by_code(payload: Any) -> dict[str, str]:
    found: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            code = node.get("code") or node.get("shortcode")
            acc = None
            for key in (
                "accessibility_caption",
                "accessibilityCaption",
                "accessibility_caption_text",
            ):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    acc = value.strip()
                    break
            if code and acc:
                found[str(code)] = acc
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def caption_for_reel(
    api: InstagramAPI,
    item: dict[str, Any],
    extra_map: dict[str, str],
    delay: float,
) -> str:
    acc = find_accessibility_caption(item)
    if acc:
        return acc
    code = media_code(item)
    if code and extra_map.get(code):
        item["accessibility_caption"] = extra_map[code]
        return extra_map[code]
    if not code:
        return ""
    time.sleep(delay)
    try:
        info = api.get_media_info_by_shortcode(code)
    except (BadResponseException, NotFoundException):
        return ""
    acc = find_accessibility_caption(info)
    if acc:
        item["accessibility_caption"] = acc
        extra_map[code] = acc
    return acc


def fetch_recent_media(api: InstagramAPI, username: str) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    max_id = None
    while len(collected) < TARGET_MEDIA_COUNT:
        remaining = min(MEDIA_PAGE_SIZE, TARGET_MEDIA_COUNT - len(collected))
        media = api.get_user_media_by_username(username, count=remaining, max_id=max_id)
        items, next_max_id = extract_media_items(media)
        if not items:
            break
        collected.extend(items)
        if not next_max_id or next_max_id == max_id:
            break
        max_id = next_max_id
        if len(collected) < TARGET_MEDIA_COUNT:
            time.sleep(PAGE_DELAY_SECONDS)
    return collected[:TARGET_MEDIA_COUNT]


def bio_keyword_match(bio: str) -> bool:
    text = bio.lower()
    for keyword in BIO_KEYWORDS:
        if keyword == "vip":
            if re.search(r"\bvip\b", text):
                return True
        elif keyword in text:
            return True
    return False


def page_has_adult_domain(html: str) -> bool:
    lowered = html.lower()
    return any(domain in lowered for domain in ADULT_DOMAINS)


def visible_text(html_doc: str) -> str:
    soup = BeautifulSoup(html_doc, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def inspect_external_link(url: str) -> tuple[str, str]:
    if not url:
        return "no_match", ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        return "unknown", "invalid url"
    try:
        response = requests.get(
            parsed.geturl(),
            headers=REQUEST_HEADERS,
            timeout=20,
            allow_redirects=True,
        )
        html_doc = response.text or ""
    except requests.RequestException as exc:
        return "unknown", f"fetch error: {exc}"

    if page_has_adult_domain(html_doc) or page_has_adult_domain(response.url):
        return "match", ""

    text = visible_text(html_doc)
    if len(text) < EMPTY_TEXT_CHARS or len(html_doc.strip()) < EMPTY_HTML_CHARS:
        return "unknown", f"near-empty page ({len(text)} text chars, http {response.status_code})"
    return "no_match", ""


def nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0


def is_collab_reel(item: dict[str, Any]) -> bool:
    """True when Instagram tagged this reel as a collab / co-authored post."""
    nodes = [item]
    nested = item.get("media")
    if isinstance(nested, dict):
        nodes.append(nested)
    for node in nodes:
        for key in COLLAB_LIST_FIELDS:
            if nonempty_list(node.get(key)):
                return True
    return False


def log_collab_fields_once(item: dict[str, Any]) -> None:
    global _COLLAB_FIELDS_LOGGED
    if _COLLAB_FIELDS_LOGGED or not isinstance(item, dict):
        return
    _COLLAB_FIELDS_LOGGED = True
    keys = sorted(
        key
        for key in item
        if "coauthor" in key.lower() or "collabor" in key.lower()
    )
    dump = {key: item.get(key) for key in keys}
    print(f"  collab field dump from first reel ({media_code(item) or 'no code'}): {dump}")


def accessibility_fails(text: str) -> str | None:
    if TEXT_SAYS_RE.search(text):
        return "text_that_says"
    if MULTI_PEOPLE_RE.search(text):
        return "multiple_people"
    if PEOPLE_RE.search(text) and not ONE_PERSON_RE.search(text):
        return "people_without_one"
    return None


def join_selection_note(history: list[str], outcome: str) -> str:
    parts = [part for part in (*history, outcome) if part]
    return "|".join(parts)


def select_reel(
    api: InstagramAPI,
    reels: list[dict[str, Any]],
    extra_map: dict[str, str],
    delay: float,
) -> tuple[dict[str, Any] | None, int, str]:
    ranked = sorted(reels, key=view_count, reverse=True)
    if ranked:
        log_collab_fields_once(ranked[0])
    checked = 0
    skip_history: list[str] = []
    for item in ranked:
        checked += 1
        if is_collab_reel(item):
            skip_history.append("skipped_collab")
            continue
        acc = caption_for_reel(api, item, extra_map, delay)
        if not acc:
            return item, checked, join_selection_note(skip_history, "unverified_no_caption")
        reason = accessibility_fails(acc)
        if reason is None:
            return item, checked, join_selection_note(skip_history, "passed_caption_check")
    return None, checked, join_selection_note(skip_history, "reel_selection_failed")


def blank_result(username: str, **overrides: Any) -> dict[str, Any]:
    row = {field: "" for field in OUTPUT_FIELDS}
    row["username"] = username
    row.update(overrides)
    return row


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_row(path: Path, fieldnames: list[str], row: dict[str, Any], first: bool) -> None:
    mode = "w" if first else "a"
    with path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if first:
            writer.writeheader()
        writer.writerow(row)


def run_discovery(api: InstagramAPI, seeds: list[str], delay: float) -> list[dict[str, str]]:
    candidates: dict[str, set[str]] = {}
    seed_set = set(seeds)
    total = len(seeds)
    for index, seed in enumerate(seeds, start=1):
        print(f"Discovery {index}/{total}: {seed}")
        try:
            profile = fetch_profile(api, seed)
            user = extract_user(profile)
            uid = user_id_of(user)
            if uid is None:
                print("  skipped (no user id)")
                time.sleep(delay)
                continue
            time.sleep(PAGE_DELAY_SECONDS)
            names = fetch_following(api, uid)
            added = 0
            for username in names:
                if username in seed_set:
                    continue
                candidates.setdefault(username, set()).add(seed)
                added += 1
            print(f"  {len(names)} following, {added} new candidates")
        except NotFoundException:
            print("  skipped (not found)")
        except BadResponseException as exc:
            print(f"  skipped (bad response: {exc})")
        except Exception as exc:  # noqa: BLE001
            print(f"  skipped (error: {exc})")
        if index < total:
            time.sleep(delay)

    rows = [
        {"username": username, "discovered_from_seed": "|".join(sorted(sources))}
        for username, sources in sorted(candidates.items())
    ]
    if rows:
        write_csv(CANDIDATES_PATH, ["username", "discovered_from_seed"], rows)
        print(f"\nWrote {len(rows)} candidates to {CANDIDATES_PATH}")
    else:
        print(f"\nNo candidates discovered; {CANDIDATES_PATH.name} not written")
    return rows


def load_candidates(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"Missing {path}. Run discovery first.")
    with path.open(newline="", encoding="utf-8") as handle:
        return [row["username"] for row in csv.DictReader(handle) if row.get("username")]


def load_manual_excludes(path: Path) -> dict[str, str]:
    excludes: dict[str, str] = {}
    if not path.exists():
        return excludes
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        username, _, reason = line.partition(",")
        name = normalize_username(username)
        if name:
            excludes[name] = (reason.strip() or "excluded_manual")
    return excludes


def looks_like_couple(*texts: str) -> bool:
    blob = " ".join(str(text or "") for text in texts)
    return bool(COUPLE_RE.search(blob))


def is_selected_row(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return bool(row.get("selected_shortcode") and not row.get("exclusion_reason"))


def evaluate_candidate(
    api: InstagramAPI,
    username: str,
    delay: float,
    vetted: bool = False,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    js_row = None
    profile = fetch_profile(api, username)
    user = extract_user(profile)
    skip = profile_status_reason(user)
    bio = str(user.get("biography") or user.get("bio") or "")
    external_url = external_url_of(user)
    followers = followers_of(user)
    pic = profile_pic_of(user)
    if skip:
        return blank_result(
            username,
            followers=followers,
            bio=bio,
            external_url=external_url,
            exclusion_reason="excluded_restricted",
        ), None

    if not vetted and as_int(followers) < MIN_FOLLOWERS:
        return blank_result(
            username,
            followers=followers,
            bio=bio,
            external_url=external_url,
            exclusion_reason="excluded_low_followers",
        ), None

    if not vetted and looks_like_couple(bio):
        return blank_result(
            username,
            followers=followers,
            bio=bio,
            external_url=external_url,
            exclusion_reason="excluded_couple_account",
        ), None

    time.sleep(PAGE_DELAY_SECONDS)
    part_a = bio_keyword_match(bio)
    part_b = "no_match"
    if external_url:
        part_b, note = inspect_external_link(external_url)
        if part_b == "unknown":
            js_row = {
                "username": username,
                "external_url": external_url,
                "reason": note,
            }

    if part_b == "match":
        niche = "link_verified"
    elif part_a:
        niche = "bio_keyword_only"
    elif vetted:
        niche = "seed_vetted"
    else:
        return blank_result(
            username,
            followers=followers,
            bio=bio,
            external_url=external_url,
            exclusion_reason="excluded_off_niche",
        ), js_row

    time.sleep(delay)
    items = fetch_recent_media(api, username)
    reels = [item for item in items if is_video_post(item)]
    min_reels = 1 if vetted else MIN_REELS
    if len(reels) < min_reels:
        return blank_result(
            username,
            followers=followers,
            bio=bio,
            external_url=external_url,
            niche_confirmed=niche,
            exclusion_reason="excluded_thin_sample",
        ), js_row

    extra_map = collect_accessibility_by_code(profile)
    selected, checked, note = select_reel(api, reels, extra_map, delay)
    base = {
        "username": username,
        "followers": followers,
        "bio": bio,
        "external_url": external_url,
        "niche_confirmed": niche,
        "reels_checked_before_pass": checked,
        "selection_note": note,
        "profile_pic_url": pic,
        "is_collab": "true" if selected is not None and is_collab_reel(selected) else (
            "false" if selected is not None else ""
        ),
    }
    if selected is None:
        return blank_result(
            **base,
            exclusion_reason="reel_selection_failed",
        ), js_row

    if not vetted and looks_like_couple(bio, caption_text(selected)):
        return blank_result(
            **base,
            exclusion_reason="excluded_couple_account",
        ), js_row

    if not vetted and view_count(selected) < MIN_REEL_VIEWS:
        return blank_result(
            **base,
            selected_shortcode=selected.get("code") or selected.get("shortcode") or "",
            viewCount=view_count(selected),
            exclusion_reason="excluded_low_views",
        ), js_row

    return blank_result(
        **base,
        selected_shortcode=selected.get("code") or selected.get("shortcode") or "",
        caption=caption_text(selected),
        likesCount=selected.get("like_count", ""),
        commentsCount=selected.get("comment_count", ""),
        viewCount=view_count(selected),
        accessibility_caption=find_accessibility_caption(selected),
        thumbnail_url=thumbnail_for_reel(selected),
        exclusion_reason="",
    ), js_row


def persist_eligibility(
    by_name: dict[str, dict[str, Any]],
    original_order: list[str],
) -> None:
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in original_order:
        row = by_name.get(name)
        if row and name not in seen:
            ordered.append(row)
            seen.add(name)
    for name, row in by_name.items():
        if name not in seen:
            ordered.append(row)
            seen.add(name)
    write_csv(ELIGIBLE_PATH, OUTPUT_FIELDS, ordered)


def run_eligibility(
    api: InstagramAPI,
    usernames: list[str],
    delay: float,
    target: int,
    vetted_usernames: set[str] | None = None,
    refresh_seeds: bool = True,
) -> list[dict[str, Any]]:
    vetted_usernames = {name.lower() for name in (vetted_usernames or set())}
    manual = load_manual_excludes(MANUAL_EXCLUDES_PATH)
    existing = load_all_eligible_rows(ELIGIBLE_PATH)
    by_name: dict[str, dict[str, Any]] = {}
    original_order: list[str] = []
    for row in existing:
        name = normalize_username(row.get("username"))
        if not name:
            continue
        by_name[name] = row
        original_order.append(name)

    for name, reason in manual.items():
        if name in by_name:
            by_name[name]["exclusion_reason"] = reason
        else:
            by_name[name] = blank_result(username=name, exclusion_reason=reason)
            original_order.append(name)

    js_rows: list[dict[str, str]] = []
    if NEEDS_JS_PATH.exists():
        with NEEDS_JS_PATH.open(newline="", encoding="utf-8") as handle:
            js_rows = [row for row in csv.DictReader(handle) if row.get("username")]
    js_seen = {normalize_username(row.get("username")) for row in js_rows}

    passed = sum(1 for row in by_name.values() if is_selected_row(row))
    persist_eligibility(by_name, original_order)
    print(f"Resuming with {passed} already selected (target {target})")

    total = len(usernames)
    for index, username in enumerate(usernames, start=1):
        key = normalize_username(username)
        is_vetted = key in vetted_usernames
        if key in manual:
            print(f"Eligibility {index}/{total}: {username}  (manual skip {manual[key]})")
            continue
        already = key in by_name
        if already and not (refresh_seeds and is_vetted):
            continue
        if passed >= target and not (refresh_seeds and is_vetted):
            print(f"\nReached {target} accounts with a selected reel; stopping.")
            break

        print(f"Eligibility {index}/{total}: {username}  (selected {passed}/{target})")
        old_selected = is_selected_row(by_name.get(key))
        try:
            row, js_row = evaluate_candidate(
                api,
                username,
                delay,
                vetted=is_vetted,
            )
        except NotFoundException:
            row, js_row = blank_result(username, exclusion_reason="not found"), None
        except BadResponseException as exc:
            row, js_row = blank_result(username, exclusion_reason=f"bad response: {exc}"), None
        except Exception as exc:  # noqa: BLE001
            row, js_row = blank_result(username, exclusion_reason=f"error: {exc}"), None

        if key in manual:
            row["exclusion_reason"] = manual[key]
        if js_row and normalize_username(js_row.get("username")) not in js_seen:
            js_rows.append(js_row)
            js_seen.add(normalize_username(js_row.get("username")))
        by_name[key] = row
        if key not in original_order:
            original_order.append(key)
        new_selected = is_selected_row(row)
        if old_selected and not new_selected:
            passed -= 1
        elif not old_selected and new_selected:
            passed += 1
        persist_eligibility(by_name, original_order)
        if new_selected:
            print(
                f"  selected {row['selected_shortcode']} via {row['selection_note']} "
                f"({row['reels_checked_before_pass']} checked, {row['niche_confirmed']})"
            )
        else:
            print(f"  skipped ({row.get('exclusion_reason') or row.get('selection_note') or 'no reel'})")
        if index < total and (passed < target or (refresh_seeds and is_vetted)):
            time.sleep(delay)

    write_csv(NEEDS_JS_PATH, ["username", "external_url", "reason"], js_rows)
    gui_rows = [row for row in (by_name.get(name) for name in original_order) if is_selected_row(row)]
    print(f"\nSelected {len(gui_rows)} account(s) → {ELIGIBLE_PATH}")
    print(f"JS-render unknowns: {len(js_rows)} → {NEEDS_JS_PATH}")
    return gui_rows


def format_count(value: Any) -> str:
    return f"{as_int(value):,}" if str(value).strip() else "—"


def selection_reason_label(row: dict[str, Any]) -> str:
    niche = {
        "seed_vetted": "Seed account",
        "link_verified": "Adult link in bio",
        "bio_keyword_only": "Bio keyword match",
    }.get(row.get("niche_confirmed") or "", row.get("niche_confirmed") or "")
    note = str(row.get("selection_note") or "")
    if "passed_caption_check" in note:
        reel = "Highest-view non-collab reel passed caption check"
    elif "unverified_no_caption" in note:
        reel = "Highest-view non-collab reel (no accessibility caption)"
    else:
        reel = note.replace("|", " · ")
    return " · ".join(part for part in (niche, reel) if part)


def card_html(row: dict[str, Any]) -> str:
    username = row.get("username") or ""
    shortcode = row.get("selected_shortcode") or ""
    permalink = f"https://www.instagram.com/reel/{shortcode}/"
    reels_link = f"https://www.instagram.com/{username}/reels/"
    caption = (row.get("caption") or "").strip()
    caption_html = html.escape(caption) if caption else '<span class="muted">No caption</span>'
    views = format_count(row.get("viewCount"))
    compact = html.escape(views)
    if shortcode:
        media = (
            f'<img src="/api/thumbnail?shortcode={html.escape(shortcode)}" '
            f'alt="@{html.escape(username)}" loading="lazy" referrerpolicy="no-referrer" '
            f"onerror=\"this.outerHTML='<div class=placeholder>{compact} views</div>'\">"
            f'<span class="views-badge">{views} views</span>'
        )
    else:
        media = (
            f'<div class="placeholder">{views} views</div>'
            f'<span class="views-badge">{views} views</span>'
        )
    return f"""
    <article class="card" data-username="{html.escape(username.lower())}">
      <a class="thumb" href="{html.escape(permalink)}" target="_blank" rel="noopener noreferrer">{media}</a>
      <div class="body">
        <a class="username" href="{html.escape(permalink)}" target="_blank" rel="noopener noreferrer">@{html.escape(username)}</a>
        <p class="followers">{format_count(row.get("followers"))} followers</p>
        <p class="reason">{html.escape(selection_reason_label(row))}</p>
        <p class="caption">{caption_html}</p>
        <p class="stats">
          <span class="views-stat">{format_count(row.get("viewCount"))} views</span>
          <span>{format_count(row.get("likesCount"))} likes</span>
          <span>{format_count(row.get("commentsCount"))} comments</span>
        </p>
        <a class="reels-link" href="{html.escape(reels_link)}" target="_blank" rel="noopener noreferrer">View @{html.escape(username)}'s Reels</a>
      </div>
    </article>
    """


def write_html(rows: list[dict[str, Any]]) -> None:
    cards = "\n".join(card_html(row) for row in rows)
    names = [row.get("username") or "" for row in rows]
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Visual Library</title>
  <style>
    :root {{
      --bg: #f4f1ea;
      --ink: #161513;
      --muted: #6b6560;
      --card: #fffcf7;
      --line: #ddd6cc;
      --accent: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; background: var(--bg); color: var(--ink); }}
    body {{ font-family: Georgia, "Times New Roman", serif; }}
    header {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px 16px; }}
    h1 {{ font-size: 2rem; margin: 0 0 8px; letter-spacing: -0.03em; }}
    .lede {{ color: var(--muted); max-width: 42rem; line-height: 1.5; margin: 0 0 20px; }}
    #search {{
      font: 16px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 10px 12px;
      border: 1px solid var(--line);
      background: var(--card);
      color: var(--ink);
      width: min(420px, 100%);
    }}
    .count {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--muted);
      font-size: 0.9rem;
      margin: 12px 0 0;
    }}
    .grid {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 8px 24px 48px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 20px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      display: flex;
      flex-direction: column;
      min-width: 0;
    }}
    .card[hidden] {{ display: none; }}
    .thumb {{
      display: block;
      aspect-ratio: 1;
      background: #ece7dd;
      overflow: hidden;
      color: inherit;
      text-decoration: none;
    }}
    .thumb img, .placeholder {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .placeholder {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.95rem;
    }}
    .body {{
      padding: 12px 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex: 1;
    }}
    .username {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-weight: 600;
      color: var(--ink);
      text-decoration: none;
    }}
    .username:hover {{ text-decoration: underline; }}
    .followers, .stats {{
      margin: 0;
      color: var(--muted);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .followers {{ font-size: 0.9rem; }}
    .caption {{
      margin: 4px 0 0;
      font-size: 0.95rem;
      line-height: 1.4;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .muted {{ color: var(--muted); }}
    .stats {{ font-size: 0.8rem; display: flex; flex-wrap: wrap; gap: 8px 12px; }}
    .reels-link {{
      margin-top: auto;
      padding-top: 10px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.82rem;
      color: var(--accent);
      text-decoration: underline;
    }}
    .empty {{
      grid-column: 1 / -1;
      color: var(--muted);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Visual Library</h1>
    <p class="lede">One selected reel per eligible account. The photo/name opens that reel; the text link opens the creator's Reels tab. No video files are hosted here.</p>
    <input id="search" type="search" placeholder="Filter by username" aria-label="Filter by username">
    <p class="count" id="count"></p>
  </header>
  <main class="grid" id="grid">
    {cards or '<p class="empty">No accounts with a selected reel yet.</p>'}
    <p class="empty" id="empty" hidden>No accounts match that username.</p>
  </main>
  <script>
    const total = {len(names)};
    const search = document.getElementById("search");
    const cards = Array.from(document.querySelectorAll(".card"));
    const empty = document.getElementById("empty");
    const count = document.getElementById("count");
    function render() {{
      const query = search.value.trim().toLowerCase();
      let shown = 0;
      cards.forEach((card) => {{
        const match = !query || card.dataset.username.includes(query);
        card.hidden = !match;
        if (match) shown += 1;
      }});
      empty.hidden = shown !== 0 || total === 0;
      count.textContent = shown + " of " + total + " accounts";
    }}
    search.addEventListener("input", render);
    render();
  </script>
</body>
</html>
"""
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(page, encoding="utf-8")
    print(f"Wrote GUI → {HTML_PATH}")


def iso_week_id(when: date | None = None) -> str:
    when = when or date.today()
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def iso_week_bounds(when: date | None = None) -> tuple[date, date]:
    when = when or date.today()
    start = when - timedelta(days=when.isoweekday() - 1)
    return start, start + timedelta(days=6)


def week_range_label(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
    return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"


def archive_week(rows: list[dict[str, Any]], when: date | None = None) -> Path:
    """Save this week's selected reel links. No thumbnail files or CDN URLs."""
    when = when or date.today()
    week_id = iso_week_id(when)
    start, end = iso_week_bounds(when)
    accounts = [
        {field: str(row.get(field) or "") for field in WEEK_LINK_FIELDS}
        for row in rows
        if row.get("selected_shortcode") and not row.get("exclusion_reason")
    ]
    payload = {
        "id": week_id,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "label": week_range_label(start, end),
        "saved_at": datetime.now().strftime("%Y-%m-%d"),
        "accounts": accounts,
    }
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    path = WEEKS_DIR / f"{week_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Archived {len(accounts)} weekly links → {path}")
    return path


def load_gui_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            row
            for row in csv.DictReader(handle)
            if row.get("selected_shortcode") and not row.get("exclusion_reason")
        ]


def load_all_eligible_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    args = parse_args()
    if args.gui_only:
        rows = load_gui_rows(ELIGIBLE_PATH)
        archive_week(rows)
        write_html(rows)
        return

    api = load_api()
    seeds = load_seeds(SEEDS_PATH)
    if args.max_seeds:
        seeds = seeds[: args.max_seeds]
    print(f"Loaded {len(seeds)} seed(s) from {SEEDS_PATH}")

    if not args.eligibility_only:
        run_discovery(api, seeds, args.delay)
    if args.discovery_only:
        return

    discovered: list[str] = []
    if CANDIDATES_PATH.exists():
        discovered = load_candidates(CANDIDATES_PATH)
    elif not args.eligibility_only:
        print(f"No {CANDIDATES_PATH.name}; scoring seeds only")
    else:
        sys.exit(f"Missing {CANDIDATES_PATH}. Run discovery first.")

    seed_set = {name.lower() for name in seeds}
    queue = list(seeds)
    for name in discovered:
        if name.lower() not in seed_set:
            queue.append(name)
    print(f"\nEligibility on {len(queue)} account(s) ({len(seeds)} seeds first, target {args.target})")
    gui_rows = run_eligibility(
        api,
        queue,
        args.delay,
        args.target,
        seed_set,
        refresh_seeds=not args.no_refresh_seeds,
    )
    archive_week(gui_rows)
    write_html(gui_rows)


if __name__ == "__main__":
    main()
