#!/usr/bin/env python3
"""Consolidate tagged formats via OpenRouter and refresh the Next.js library data.

Usage:
    python build_visual_library.py
    python build_visual_library.py --reuse-taxonomy

Writes taxonomy.json, updates library_index.csv, extracts public/thumbnails,
and refreshes data/library.json for the Next.js app.
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
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "library_index.csv"
TAXONOMY_PATH = ROOT / "taxonomy.json"
PUBLIC_DIR = ROOT / "public"
THUMB_DIR = PUBLIC_DIR / "thumbnails"
LIBRARY_DIR = ROOT / "library"
DATA_PATH = ROOT / "data" / "library.json"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODELS = [
    "qwen/qwen2.5-vl-32b-instruct:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "google/gemma-4-31b-it:free",
    "openrouter/free",
]
BATCH_SIZE = 35
TIMEOUT_SECONDS = 180
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

CONSOLIDATION_INSTRUCTIONS = """Below is a list of short-form video content descriptions, each with a
format label assigned independently. Many of these labels describe the
same underlying content format, just worded differently. Group them into
a smaller set of consolidated format categories based on actual structural
similarity (hook style, core action mechanic, payoff type), not surface
wording.

For each consolidated format, return:
- consolidated_format_name: a clear, reusable name for this format
- original_labels: list of the original format_category values that
  belong to this group
- fixed_structure: what stays the same across all examples in this group
  (the hook style, pacing, payoff mechanic)
- variable_elements: what differs across examples in this group (the
  specific slots that can be swapped to create new videos in this format)
- example_count: how many videos fall into this group
- video_ids: list of video_filename values belonging to this group

Return ONLY valid JSON as a list of these consolidated format objects.
Aim for meaningful consolidation, don't force unrelated formats together,
but do merge formats that share the same core mechanic even if worded
differently. It's fine for some formats to remain standalone if they're
genuinely distinct with no close match.

Here is the data:"""

MERGE_INSTRUCTIONS = """You previously grouped short-form videos into format categories in
separate batches. Merge the following JSON lists into one de-duplicated
taxonomy. Combine groups that share the same core mechanic; keep genuinely
distinct formats separate. Recalculate example_count and union video_ids
and original_labels. Return ONLY valid JSON as a list of consolidated
format objects with keys: consolidated_format_name, original_labels,
fixed_structure, variable_elements, example_count, video_ids.

Here are the batch taxonomies:"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("visual_library")


def strip_fences(text: str) -> str:
    return FENCE_RE.sub("", (text or "").strip()).strip()


def shortcode_of(filename: str) -> str:
    return Path(filename).stem


def permalink_of(filename: str) -> str:
    return f"https://www.instagram.com/reel/{shortcode_of(filename)}/"


def load_index() -> tuple[list[str], list[dict[str, str]]]:
    if not INDEX_PATH.exists():
        sys.exit(f"Missing {INDEX_PATH}")
    with INDEX_PATH.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{key: (row.get(key) or "") for key in fields} for row in reader]
    return fields, rows


def tagged_payload(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    payload = []
    for row in rows:
        if row.get("tagging_status") != "ok":
            continue
        payload.append(
            {
                "video_filename": row.get("video_filename", ""),
                "username": row.get("username", ""),
                "format_category": row.get("format_category", ""),
                "hook": row.get("hook", ""),
                "core_action": row.get("core_action", ""),
                "payoff_or_cta": row.get("payoff_or_cta", ""),
                "variable_elements": row.get("variable_elements", ""),
            }
        )
    return payload


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def parse_taxonomy(text: str) -> list[dict[str, Any]]:
    cleaned = strip_fences(text)
    if not cleaned:
        raise ValueError("Empty model response")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if isinstance(parsed, dict):
        for key in ("formats", "taxonomy", "consolidated_formats", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Taxonomy JSON was not a non-empty list")
    normalized = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("consolidated_format_name")
            or item.get("name")
            or ""
        ).strip()
        if not name:
            continue
        video_ids = item.get("video_ids") or item.get("videos") or []
        if not isinstance(video_ids, list):
            video_ids = [video_ids]
        labels = item.get("original_labels") or []
        if not isinstance(labels, list):
            labels = [labels]
        ids = [str(value).strip() for value in video_ids if str(value).strip()]
        normalized.append(
            {
                "consolidated_format_name": name,
                "original_labels": [str(value).strip() for value in labels if str(value).strip()],
                "fixed_structure": str(item.get("fixed_structure") or "").strip(),
                "variable_elements": str(item.get("variable_elements") or "").strip(),
                "example_count": len(ids) or int(item.get("example_count") or 0),
                "video_ids": ids,
            }
        )
    if not normalized:
        raise ValueError("No usable format objects in model response")
    return normalized


def openrouter_chat(
    api_key: str,
    model: str,
    prompt: str,
) -> str:
    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost",
            "X-Title": "Venus Tech Visual Library",
        },
        json={
            "model": model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "You return only valid JSON. No markdown, no commentary.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:800]}")
    body = response.json()
    error = body.get("error")
    if error:
        raise RuntimeError(f"OpenRouter error: {error}")
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {body}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    else:
        text = str(content or "")
    if not text.strip():
        raise RuntimeError("OpenRouter returned empty content")
    return text


def call_with_retry(api_key: str, models: list[str], prompt: str) -> tuple[list[dict[str, Any]], str]:
    last_error: Exception | None = None
    for model in models:
        for attempt in range(2):
            try:
                log.info("OpenRouter %s attempt %d", model, attempt + 1)
                text = openrouter_chat(api_key, model, prompt)
                return parse_taxonomy(text), model
            except Exception as exc:
                last_error = exc
                log.warning("%s attempt %d failed: %s", model, attempt + 1, exc)
                message = str(exc)
                if "404" in message or "No endpoints found" in message or "unavailable for free" in message:
                    break
                if attempt == 0:
                    time.sleep(4)
        log.warning("Moving to next OpenRouter model after failures on %s", model)
    raise RuntimeError(f"All OpenRouter models failed: {last_error}")


def consolidate(api_key: str, models: list[str], payload: list[dict[str, str]]) -> tuple[list[dict[str, Any]], str]:
    batches = chunked(payload, BATCH_SIZE)
    log.info("Consolidating %d tagged videos in %d batch(es)", len(payload), len(batches))
    taxonomies: list[list[dict[str, Any]]] = []
    used_model = models[0]
    for index, batch in enumerate(batches, start=1):
        prompt = CONSOLIDATION_INSTRUCTIONS + "\n" + json.dumps(batch, ensure_ascii=True, indent=2)
        taxonomy, used_model = call_with_retry(api_key, models, prompt)
        log.info("Batch %d/%d returned %d formats", index, len(batches), len(taxonomy))
        taxonomies.append(taxonomy)
    if len(taxonomies) == 1:
        return taxonomies[0], used_model
    merge_prompt = MERGE_INSTRUCTIONS + "\n" + json.dumps(taxonomies, ensure_ascii=True, indent=2)
    merged, used_model = call_with_retry(api_key, models, merge_prompt)
    return merged, used_model


def filename_keys(value: str) -> set[str]:
    text = (value or "").strip()
    if not text:
        return set()
    stem = shortcode_of(text)
    return {text, stem, f"{stem}.mp4"}


def apply_taxonomy(
    rows: list[dict[str, str]],
    taxonomy: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lookup: dict[str, str] = {}
    for group in taxonomy:
        name = group["consolidated_format_name"]
        ids = []
        for video_id in group.get("video_ids") or []:
            ids.append(video_id)
            for key in filename_keys(str(video_id)):
                lookup[key] = name
        group["video_ids"] = [
            value if str(value).endswith(".mp4") else f"{shortcode_of(str(value))}.mp4"
            for value in ids
        ]
        group["example_count"] = 0

    for row in rows:
        row["consolidated_format"] = ""
        if row.get("tagging_status") != "ok":
            continue
        filename = row.get("video_filename", "")
        name = ""
        for key in filename_keys(filename):
            if key in lookup:
                name = lookup[key]
                break
        if not name:
            original = (row.get("format_category") or "").strip().lower()
            for group in taxonomy:
                labels = {str(label).strip().lower() for label in group.get("original_labels") or []}
                if original and original in labels:
                    name = group["consolidated_format_name"]
                    break
        row["consolidated_format"] = name
        if name:
            for group in taxonomy:
                if group["consolidated_format_name"] != name:
                    continue
                if filename and filename not in group["video_ids"]:
                    group["video_ids"].append(filename)
                group["example_count"] = len(group["video_ids"])
                break
    for group in taxonomy:
        group["example_count"] = len(group.get("video_ids") or [])
    return taxonomy


def write_index(fields: list[str], rows: list[dict[str, str]]) -> None:
    if "consolidated_format" not in fields:
        fields = [*fields, "consolidated_format"]
    with INDEX_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
        return 4.0


def extract_thumbnail(video_path: Path, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    timestamp = video_duration_seconds(video_path) / 2
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


def local_video_path(row: dict[str, str]) -> Path | None:
    relative = (row.get("local_path") or "").strip()
    if relative:
        path = ROOT / relative
        if path.exists():
            return path
    filename = (row.get("video_filename") or "").strip()
    username = (row.get("username") or "").strip()
    if username and filename:
        path = LIBRARY_DIR / username / filename
        if path.exists():
            return path
    return None


def build_thumbnails(rows: list[dict[str, str]]) -> dict[str, str]:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumbs: dict[str, str] = {}
    for row in rows:
        if row.get("tagging_status") != "ok":
            continue
        filename = row.get("video_filename") or ""
        if not filename:
            continue
        dest = THUMB_DIR / f"{shortcode_of(filename)}.jpg"
        source = local_video_path(row)
        if source is None:
            log.warning("No local video for thumbnail: %s", filename)
            continue
        if extract_thumbnail(source, dest):
            thumbs[filename] = f"thumbnails/{dest.name}"
        else:
            log.warning("Thumbnail extract failed for %s", filename)
    return thumbs


def library_payload(
    taxonomy: list[dict[str, Any]],
    rows: list[dict[str, str]],
    thumbs: dict[str, str],
    model_used: str,
) -> dict[str, Any]:
    library_rows = []
    for row in rows:
        if row.get("tagging_status") != "ok" or not row.get("consolidated_format"):
            continue
        filename = row.get("video_filename") or ""
        thumb = thumbs.get(filename, "")
        if thumb and not thumb.startswith("/"):
            thumb = f"/{thumb}"
        library_rows.append(
            {
                "username": row.get("username", ""),
                "caption": row.get("caption", ""),
                "hook": row.get("hook", ""),
                "core_action": row.get("core_action", ""),
                "payoff_or_cta": row.get("payoff_or_cta", ""),
                "likesCount": row.get("likesCount", ""),
                "videoViewCount": row.get("videoViewCount", ""),
                "consolidated_format": row.get("consolidated_format", ""),
                "format_category": row.get("format_category", ""),
                "permalink": permalink_of(filename),
                "thumbnail": thumb,
                "video_filename": filename,
            }
        )
    return {
        "model": model_used,
        "formats": taxonomy,
        "videos": library_rows,
    }


def write_library_data(payload: dict[str, Any]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")



def print_summary(
    original_labels: int,
    taxonomy: list[dict[str, Any]],
    rows: list[dict[str, str]],
    model_used: str,
) -> None:
    assigned = [row for row in rows if row.get("tagging_status") == "ok" and row.get("consolidated_format")]
    unassigned = [row for row in rows if row.get("tagging_status") == "ok" and not row.get("consolidated_format")]
    counts = Counter(row["consolidated_format"] for row in assigned)
    print()
    print(f"Model: {model_used}")
    print(f"Original format_category labels: {original_labels}")
    print(f"Consolidated formats: {len(taxonomy)}")
    print(f"Tagged videos assigned: {len(assigned)}")
    if unassigned:
        print(f"Tagged videos still unassigned: {len(unassigned)}")
    print("Videos per consolidated format:")
    for name, count in counts.most_common():
        note = "  ← enough examples to trust" if count >= 3 else ("  ← single-example guess" if count == 1 else "")
        print(f"  {count:3d}  {name}{note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidate formats and build the visual library")
    parser.add_argument("--reuse-taxonomy", action="store_true", help="Skip the model call and reuse taxonomy.json")
    parser.add_argument("--model", help="OpenRouter model id (defaults to env or qwen free VL)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    fields, rows = load_index()
    payload = tagged_payload(rows)
    if not payload:
        sys.exit("No rows with tagging_status == ok in library_index.csv")

    original_labels = len({item["format_category"] for item in payload if item.get("format_category")})
    model_used = "reused taxonomy.json"

    if args.reuse_taxonomy:
        if not TAXONOMY_PATH.exists():
            sys.exit("Missing taxonomy.json; run without --reuse-taxonomy first")
        taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
        if not isinstance(taxonomy, list):
            sys.exit("taxonomy.json is not a list")
    else:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            sys.exit("Missing OPENROUTER_API_KEY in .env")
        preferred = (args.model or os.getenv("OPENROUTER_MODEL") or DEFAULT_MODELS[0]).strip()
        models = [preferred] + [item for item in DEFAULT_MODELS if item != preferred]
        try:
            taxonomy, model_used = consolidate(api_key, models, payload)
        except Exception:
            log.exception("Taxonomy consolidation failed; leaving library_index.csv unchanged")
            sys.exit(1)
        TAXONOMY_PATH.write_text(json.dumps(taxonomy, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        log.info("Wrote %s", TAXONOMY_PATH)

    taxonomy = apply_taxonomy(rows, taxonomy)
    TAXONOMY_PATH.write_text(json.dumps(taxonomy, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    write_index(fields, rows)
    log.info("Updated %s with consolidated_format", INDEX_PATH)

    thumbs = build_thumbnails(rows)
    log.info("Thumbnails ready: %d", len(thumbs))
    write_library_data(library_payload(taxonomy, rows, thumbs, model_used))
    log.info("Wrote %s", DATA_PATH)
    print_summary(original_labels, taxonomy, rows, model_used)


if __name__ == "__main__":
    main()
