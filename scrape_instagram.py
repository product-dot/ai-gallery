#!/usr/bin/env python3
"""Scrape Instagram profile + reel data via RocketAPI.

Usage:
    python scrape_instagram.py --inspect
    python scrape_instagram.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from rocketapi import InstagramAPI
from rocketapi.exceptions import BadResponseException, NotFoundException

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "accounts.csv"
DEFAULT_OUTPUT = ROOT / "accounts_data.csv"
DEFAULT_EXCLUDED = ROOT / "excluded_accounts.csv"
SAMPLE_RESPONSE_PATH = ROOT / "sample_response.json"

MEDIA_PAGE_SIZE = 12  # RocketAPI max per request
TARGET_MEDIA_COUNT = 20
ACCOUNT_DELAY_SECONDS = 1.5
PAGE_DELAY_SECONDS = 0.75

LINK_HEADER_HINTS = ("link", "url", "profile")
RESERVED_PATHS = {
    "p",
    "reel",
    "reels",
    "stories",
    "tv",
    "explore",
    "accounts",
    "direct",
    "tagged",
    "followers",
    "following",
    "highlights",
    "share",
    "about",
    "legal",
    "developer",
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

OUTPUT_FIELDS = [
    "username",
    "followers",
    "bio",
    "shortcode",
    "caption",
    "likesCount",
    "commentsCount",
    "viewCount",
    "videoUrl",
    "timestamp",
]
EXCLUDED_FIELDS = ["username", "reason"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--excluded", type=Path, default=DEFAULT_EXCLUDED)
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Fetch the first username only and dump raw JSON, then exit.",
    )
    parser.add_argument(
        "--retry-excluded",
        action="store_true",
        help="Re-scrape usernames from excluded_accounts.csv and append new reels.",
    )
    parser.add_argument("--delay", type=float, default=ACCOUNT_DELAY_SECONDS)
    return parser.parse_args()


def load_api() -> InstagramAPI:
    load_dotenv(ROOT / ".env")
    token = os.getenv("ROCKETAPI_TOKEN", "").strip()
    if not token:
        sys.exit("Missing ROCKETAPI_TOKEN in .env")
    return InstagramAPI(token=token, max_timeout=60)


def detect_link_column(fieldnames: list[str] | None) -> str:
    if not fieldnames:
        raise ValueError("CSV has no header row")
    headers = [name.strip() for name in fieldnames if name and name.strip()]
    if not headers:
        raise ValueError("CSV has no usable column names")

    lowered = {name.lower(): name for name in headers}
    for exact in ("link", "url", "profile_url", "profile url", "account links"):
        if exact in lowered:
            return lowered[exact]

    for name in headers:
        compact = name.lower().replace(" ", "_")
        if any(hint in compact for hint in LINK_HEADER_HINTS):
            return name

    return headers[0]


def username_from_url(raw: str) -> str | None:
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        return None
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value.lstrip("/")

    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"instagram.com", "instagr.am"}:
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parts:
        return None
    candidate = parts[0]
    if candidate.lower() in RESERVED_PATHS:
        return None
    if not USERNAME_RE.match(candidate):
        return None
    return candidate


def load_usernames(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        sys.exit(f"Input CSV not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        link_column = detect_link_column(reader.fieldnames)
        print(f"Using link column: {link_column!r}")
        usernames: list[str] = []
        seen: set[str] = set()
        for row in reader:
            username = username_from_url(row.get(link_column, ""))
            if not username or username.lower() in seen:
                continue
            seen.add(username.lower())
            usernames.append(username)
    return usernames


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


def extract_user(profile: dict[str, Any]) -> dict[str, Any]:
    user = nested_get(
        profile,
        ("data", "user"),
        ("user",),
        default=profile if isinstance(profile, dict) else {},
    )
    return user if isinstance(user, dict) else {}


def fetch_profile(api: InstagramAPI, username: str) -> dict[str, Any]:
    """Web GraphQL 400s on many professional/age-gated accounts.

    RocketAPI's mobile get_user_info_by_username still returns those
    profiles, so fall back to it when web_profile_info fails.
    """
    try:
        return api.get_web_profile_info(username)
    except (BadResponseException, NotFoundException):
        time.sleep(PAGE_DELAY_SECONDS)
        return api.get_user_info_by_username(username)


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


def extract_media_items(media: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    items = media.get("items")
    if isinstance(items, list):
        return items, media.get("next_max_id") or media.get("max_id")

    edges = nested_get(
        media,
        ("items",),
        ("data", "user", "edge_owner_to_timeline_media", "edges"),
        ("user", "edge_owner_to_timeline_media", "edges"),
        default=[],
    )
    if isinstance(edges, list):
        nodes = []
        for edge in edges:
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                nodes.append(edge["node"])
            elif isinstance(edge, dict):
                nodes.append(edge)
        page_info = nested_get(
            media,
            ("data", "user", "edge_owner_to_timeline_media", "page_info"),
            ("user", "edge_owner_to_timeline_media", "page_info"),
            default={},
        )
        end_cursor = None
        if isinstance(page_info, dict) and page_info.get("has_next_page"):
            end_cursor = page_info.get("end_cursor")
        return nodes, end_cursor or media.get("next_max_id")
    return [], media.get("next_max_id")


def is_video_post(item: dict[str, Any]) -> bool:
    """Keep reels/videos; skip image-only feed posts."""
    if item.get("product_type") == "clips":
        return True
    if item.get("media_type") == 2:
        return True
    versions = item.get("video_versions")
    return isinstance(versions, list) and bool(versions)


def first_video_url(item: dict[str, Any]) -> str:
    versions = item.get("video_versions")
    if isinstance(versions, list):
        for version in versions:
            if isinstance(version, dict) and version.get("url"):
                return str(version["url"])
    video_url = item.get("video_url")
    if video_url:
        return str(video_url)
    resources = nested_get(item, ("video_resources",), default=[])
    if isinstance(resources, list):
        for resource in resources:
            if isinstance(resource, dict) and resource.get("src"):
                return str(resource["src"])
    return ""


def caption_text(item: dict[str, Any]) -> str:
    caption = item.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "")
    if isinstance(caption, str):
        return caption
    edges = nested_get(item, ("edge_media_to_caption", "edges"), default=[])
    if isinstance(edges, list) and edges:
        node = edges[0].get("node") if isinstance(edges[0], dict) else None
        if isinstance(node, dict):
            return str(node.get("text") or "")
    return ""


def count_value(item: dict[str, Any], *keys: str, nested: tuple[str, ...] | None = None) -> Any:
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    if nested:
        value = nested_get(item, nested)
        if value is not None:
            return value
    return ""


def reel_row(username: str, followers: Any, bio: str, item: dict[str, Any]) -> dict[str, Any]:
    timestamp = item.get("taken_at")
    if timestamp is None:
        timestamp = item.get("taken_at_timestamp") or item.get("timestamp")
    return {
        "username": username,
        "followers": followers,
        "bio": bio,
        "shortcode": item.get("code") or item.get("shortcode") or "",
        "caption": caption_text(item),
        "likesCount": count_value(
            item,
            "like_count",
            "likesCount",
            nested=("edge_liked_by", "count"),
        ),
        "commentsCount": count_value(
            item,
            "comment_count",
            "commentsCount",
            nested=("edge_media_to_comment", "count"),
        ),
        "viewCount": count_value(
            item,
            "play_count",
            "ig_play_count",
            "view_count",
            "video_view_count",
            "video_play_count",
        ),
        "videoUrl": first_video_url(item),
        "timestamp": timestamp if timestamp is not None else "",
    }


def fetch_recent_media(api: InstagramAPI, username: str, target: int = TARGET_MEDIA_COUNT) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    max_id = None
    while len(collected) < target:
        remaining = min(MEDIA_PAGE_SIZE, target - len(collected))
        media = api.get_user_media_by_username(username, count=remaining, max_id=max_id)
        items, next_max_id = extract_media_items(media if isinstance(media, dict) else {})
        if not items:
            break
        collected.extend(items)
        if not next_max_id or next_max_id == max_id:
            break
        max_id = next_max_id
        if len(collected) < target:
            time.sleep(PAGE_DELAY_SECONDS)
    return collected[:target]


def inspect_first(api: InstagramAPI, username: str) -> None:
    print(f"\nInspecting raw responses for: {username}")
    profile = api.get_web_profile_info(username)
    time.sleep(PAGE_DELAY_SECONDS)
    media = api.get_user_media_by_username(username, count=MEDIA_PAGE_SIZE)
    payload = {
        "username": username,
        "get_web_profile_info": profile,
        "get_user_media_by_username": media,
    }
    SAMPLE_RESPONSE_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    user = extract_user(profile if isinstance(profile, dict) else {})
    items, _ = extract_media_items(media if isinstance(media, dict) else {})
    print("Profile user keys:", ", ".join(sorted(user.keys())[:40]), "...")
    print(
        "Profile fields:",
        f"followers={nested_get(user, ('edge_followed_by', 'count'))}",
        f"is_private={user.get('is_private')}",
        f"restricted_by_viewer={user.get('restricted_by_viewer')}",
        f"is_verified={user.get('is_verified')}",
    )
    if items:
        first = items[0]
        print(
            "First media fields:",
            f"media_type={first.get('media_type')}",
            f"product_type={first.get('product_type')}",
            f"code={first.get('code')}",
            f"like_count={first.get('like_count')}",
            f"comment_count={first.get('comment_count')}",
            f"play_count={first.get('play_count')}",
            f"ig_play_count={first.get('ig_play_count')}",
            f"taken_at={first.get('taken_at')}",
            f"video_versions={bool(first.get('video_versions'))}",
        )
    print(f"\nSaved full raw JSON to {SAMPLE_RESPONSE_PATH}")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def scrape_accounts(
    api: InstagramAPI,
    usernames: list[str],
    output_path: Path,
    excluded_path: Path,
    delay: float,
    append: bool = False,
) -> None:
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    existing_keys: set[tuple[str, str]] = set()
    previous_excluded: list[dict[str, Any]] = []
    total = len(usernames)

    if append and output_path.exists():
        with output_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows.append(row)
                existing_keys.add((row.get("username", ""), row.get("shortcode", "")))
    if append and excluded_path.exists():
        retrying = set(usernames)
        with excluded_path.open(newline="", encoding="utf-8") as handle:
            previous_excluded = [
                row
                for row in csv.DictReader(handle)
                if row.get("username") not in retrying
            ]

    for index, username in enumerate(usernames, start=1):
        print(f"Processing {index}/{total}: {username}")
        try:
            profile = fetch_profile(api, username)
            user = extract_user(profile if isinstance(profile, dict) else {})
            skip_reason = profile_status_reason(user)
            if skip_reason:
                print(f"  skipped ({skip_reason})")
                excluded.append({"username": username, "reason": skip_reason})
                time.sleep(delay)
                continue

            followers = nested_get(
                user,
                ("edge_followed_by", "count"),
                ("follower_count",),
                default="",
            )
            bio = user.get("biography") or user.get("bio") or ""

            time.sleep(PAGE_DELAY_SECONDS)
            items = fetch_recent_media(api, username)
            reel_count = 0
            for item in items:
                if not isinstance(item, dict) or not is_video_post(item):
                    continue
                row = reel_row(username, followers, bio, item)
                key = (str(row["username"]), str(row["shortcode"]))
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                rows.append(row)
                reel_count += 1
            print(f"  {reel_count} reel(s) from {len(items)} recent post(s)")
        except NotFoundException:
            print("  skipped (not found)")
            excluded.append({"username": username, "reason": "not found"})
        except BadResponseException as exc:
            print(f"  skipped (bad response: {exc})")
            excluded.append({"username": username, "reason": f"bad response: {exc}"})
        except Exception as exc:  # noqa: BLE001 - keep the loop going
            print(f"  skipped (error: {exc})")
            excluded.append({"username": username, "reason": f"error: {exc}"})

        if index < total:
            time.sleep(delay)

    write_csv(output_path, OUTPUT_FIELDS, rows)
    write_csv(excluded_path, EXCLUDED_FIELDS, previous_excluded + excluded)
    print(f"\nWrote {len(rows)} reel row(s) to {output_path}")
    print(f"Wrote {len(excluded)} excluded account(s) to {excluded_path}")


def load_excluded_usernames(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"Excluded CSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return [row["username"] for row in csv.DictReader(handle) if row.get("username")]


def main() -> None:
    args = parse_args()
    if args.retry_excluded:
        usernames = load_excluded_usernames(args.excluded)
        print(f"Retrying {len(usernames)} excluded username(s):")
    else:
        usernames = load_usernames(args.input)
        print(f"\nParsed {len(usernames)} username(s):")
    for username in usernames:
        print(f"  {username}")
    if not usernames:
        sys.exit("No Instagram usernames found")

    api = load_api()
    if args.inspect:
        inspect_first(api, usernames[0])
        return
    scrape_accounts(
        api,
        usernames,
        args.output,
        args.excluded,
        args.delay,
        append=args.retry_excluded,
    )


if __name__ == "__main__":
    main()
