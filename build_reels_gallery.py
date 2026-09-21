#!/usr/bin/env python3
"""Build a static HTML gallery of the highest-view reel per account.

Usage:
    python build_reels_gallery.py
"""

from __future__ import annotations

import csv
import html
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from dotenv import load_dotenv
from rocketapi import InstagramAPI
from rocketapi.exceptions import BadResponseException, NotFoundException

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "accounts_data.csv"
PUBLIC_DIR = ROOT / "public"
THUMB_DIR = PUBLIC_DIR / "thumbnails"
HTML_PATH = PUBLIC_DIR / "index.html"
MISSING_PATH = ROOT / "missing_thumbnails.csv"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Referer": "https://www.instagram.com/",
}


def as_int(value: Any) -> int:
    text = str(value or "").strip().replace(",", "")
    try:
        return int(float(text)) if text else 0
    except ValueError:
        return 0


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        sys.exit(f"Missing {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return [{k: (v or "") for k, v in row.items()} for row in csv.DictReader(handle)]


def select_top_reels(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    best: dict[str, dict[str, str]] = {}
    for row in rows:
        username = (row.get("username") or "").strip()
        if not username:
            continue
        current = best.get(username)
        if current is None or as_int(row.get("viewCount")) > as_int(current.get("viewCount")):
            best[username] = row
    selected = list(best.values())
    selected.sort(key=lambda row: as_int(row.get("viewCount")), reverse=True)
    return selected


def format_count(value: Any) -> str:
    return f"{as_int(value):,}"


def load_api() -> InstagramAPI | None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("ROCKETAPI_TOKEN", "").strip()
    if not token:
        return None
    return InstagramAPI(token=token, max_timeout=60)


def fresh_video_url(api: InstagramAPI | None, shortcode: str) -> str:
    if api is None or not shortcode:
        return ""
    try:
        info = api.get_media_info_by_shortcode(shortcode)
    except (BadResponseException, NotFoundException, requests.RequestException):
        return ""
    item: dict[str, Any] = {}
    if isinstance(info, dict):
        items = info.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            item = items[0]
        elif isinstance(info.get("media"), dict):
            item = info["media"]
        elif isinstance(info.get("data"), dict):
            item = info["data"]
    versions = item.get("video_versions") if isinstance(item, dict) else None
    if isinstance(versions, list):
        for version in versions:
            if isinstance(version, dict) and version.get("url"):
                return str(version["url"])
    return ""


def download_video(url: str, dest: Path) -> str | None:
    try:
        with requests.get(url, headers=REQUEST_HEADERS, stream=True, timeout=45) as response:
            if response.status_code != 200:
                return f"http {response.status_code}"
            with dest.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        handle.write(chunk)
    except requests.RequestException as exc:
        return f"download error: {exc}"
    if dest.stat().st_size == 0:
        return "empty video download"
    return None


def extract_frame(video_path: Path, dest: Path, seconds: float) -> bool:
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=480:-2",
            "-q:v",
            "4",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def grab_thumbnail(row: dict[str, str], dest: Path, api: InstagramAPI | None) -> str | None:
    original = (row.get("videoUrl") or "").strip()
    shortcode = (row.get("shortcode") or "").strip()
    dest.parent.mkdir(parents=True, exist_ok=True)

    last_reason = "missing videoUrl"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "clip.mp4"
        if original:
            last_reason = download_video(original, tmp_path) or ""
            if not last_reason:
                if extract_frame(tmp_path, dest, 1.0) or extract_frame(tmp_path, dest, 0.0):
                    return None
                last_reason = "ffmpeg could not extract a frame"

        if last_reason.startswith("http ") or last_reason in {
            "missing videoUrl",
            "empty video download",
        }:
            time.sleep(0.4)
            refreshed = fresh_video_url(api, shortcode)
            if not refreshed:
                if last_reason.startswith("http "):
                    return f"{last_reason} (videoUrl expired or blocked)"
                return last_reason
            last_reason = download_video(refreshed, tmp_path) or ""
            if not last_reason:
                if extract_frame(tmp_path, dest, 1.0) or extract_frame(tmp_path, dest, 0.0):
                    return None
                last_reason = "ffmpeg could not extract a frame"
    return last_reason or "missing videoUrl"


def build_thumbnails(
    rows: list[dict[str, str]], api: InstagramAPI | None
) -> tuple[dict[str, str], list[dict[str, str]]]:
    thumbs: dict[str, str] = {}
    missing: list[dict[str, str]] = []
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        username = row["username"]
        shortcode = row.get("shortcode") or username
        dest = THUMB_DIR / f"{shortcode}.jpg"
        print(f"Thumbnail {index}/{total}: @{username} ({shortcode})")
        if dest.exists() and dest.stat().st_size > 0:
            thumbs[shortcode] = f"thumbnails/{dest.name}"
            print(f"  exists {dest.name}")
            continue
        reason = grab_thumbnail(row, dest, api)
        if reason:
            print(f"  skipped ({reason})")
            missing.append(
                {
                    "username": username,
                    "shortcode": shortcode,
                    "videoUrl": row.get("videoUrl", ""),
                    "reason": reason,
                }
            )
            continue
        thumbs[shortcode] = f"thumbnails/{dest.name}"
        print(f"  saved {dest.name}")
    return thumbs, missing


def card_html(row: dict[str, str], thumb: str) -> str:
    username = row["username"]
    shortcode = row.get("shortcode") or ""
    permalink = f"https://www.instagram.com/reel/{shortcode}/"
    reels_link = f"https://www.instagram.com/{username}/reels/"
    caption = (row.get("caption") or "").strip()
    caption_html = html.escape(caption) if caption else "<span class=\"muted\">No caption</span>"
    thumb_alt = html.escape(f"@{username} reel")
    if thumb:
        media = f'<img src="{html.escape(thumb)}" alt="{thumb_alt}">'
    else:
        media = f'<div class="placeholder">No thumbnail</div>'
    return f"""
    <article class="card" data-username="{html.escape(username.lower())}">
      <a class="thumb" href="{html.escape(permalink)}" target="_blank" rel="noopener noreferrer">
        {media}
      </a>
      <div class="body">
        <a class="username" href="{html.escape(permalink)}" target="_blank" rel="noopener noreferrer">@{html.escape(username)}</a>
        <p class="followers">{format_count(row.get("followers"))} followers</p>
        <p class="caption">{caption_html}</p>
        <p class="stats">
          <span>{format_count(row.get("likesCount"))} likes</span>
          <span>{format_count(row.get("commentsCount"))} comments</span>
          <span>{format_count(row.get("viewCount"))} views</span>
        </p>
        <a class="reels-link" href="{html.escape(reels_link)}" target="_blank" rel="noopener noreferrer">View @{html.escape(username)}'s Reels</a>
      </div>
    </article>
    """


def write_html(rows: list[dict[str, str]], thumbs: dict[str, str]) -> None:
    cards = "\n".join(card_html(row, thumbs.get(row.get("shortcode") or "", "")) for row in rows)
    usernames = json.dumps([row["username"] for row in rows])
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Selected Reels Library</title>
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
    html, body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
    }}
    body {{
      font-family: Georgia, "Times New Roman", serif;
    }}
    header {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 24px 16px;
    }}
    h1 {{
      font-size: 2rem;
      margin: 0 0 8px;
      letter-spacing: -0.03em;
    }}
    .lede {{
      color: var(--muted);
      max-width: 40rem;
      line-height: 1.5;
      margin: 0 0 20px;
    }}
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
      cursor: pointer;
    }}
    .card[hidden] {{ display: none; }}
    .thumb {{
      display: block;
      aspect-ratio: 9 / 16;
      background: #ece7dd;
      overflow: hidden;
      color: inherit;
      text-decoration: none;
    }}
    .thumb img,
    .placeholder {{
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
      font-size: 0.85rem;
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
    .followers {{
      margin: 0;
      color: var(--muted);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.9rem;
    }}
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
    .stats {{
      margin: 4px 0 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.8rem;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
    }}
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
    <h1>Selected Reels Library</h1>
    <p class="lede">Highest-view reel for each account. Thumbnail opens that reel; the text link opens the creator's Reels tab.</p>
    <input id="search" type="search" placeholder="Filter by username" aria-label="Filter by username">
    <p class="count" id="count"></p>
  </header>
  <main class="grid" id="grid">
    {cards}
    <p class="empty" id="empty" hidden>No accounts match that username.</p>
  </main>
  <script>
    const usernames = {usernames};
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
      empty.hidden = shown !== 0;
      count.textContent = shown + " of " + usernames.length + " accounts";
    }}

    search.addEventListener("input", render);
    cards.forEach((card) => {{
      card.addEventListener("click", (event) => {{
        const target = event.target;
        if (target.closest(".reels-link") || target.closest("a")) return;
        const thumb = card.querySelector(".thumb");
        if (thumb) window.open(thumb.href, "_blank", "noopener");
      }});
    }});
    render();
  </script>
</body>
</html>
"""
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(page, encoding="utf-8")


def write_missing(rows: list[dict[str, str]]) -> None:
    with MISSING_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["username", "shortcode", "videoUrl", "reason"]
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rows = load_rows(CSV_PATH)
    selected = select_top_reels(rows)
    print(f"Selected {len(selected)} reel(s) (highest viewCount per account)\n")
    for row in selected:
        print(f"  @{row['username']}: {row.get('shortcode')} — {format_count(row.get('viewCount'))} views")
    print()
    thumbs, missing = build_thumbnails(selected, load_api())
    write_missing(missing)
    write_html(selected, thumbs)
    print(f"\nWrote {HTML_PATH}")
    print(f"Thumbnails: {len(thumbs)} saved, {len(missing)} missing")
    print(f"Missing log: {MISSING_PATH}")


if __name__ == "__main__":
    main()
