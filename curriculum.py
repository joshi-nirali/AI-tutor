"""Load fixed word lists (and optional image hints) per lesson topic."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CACHE: dict[str, Any] | None = None


def _data_path() -> Path:
    override = os.getenv("KID_CURRICULUM_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "word_lists.json"


def load_curriculum() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = _data_path()
    if not path.is_file():
        _CACHE = {}
        return _CACHE
    with open(path, encoding="utf-8") as f:
        _CACHE = json.load(f)
    if not isinstance(_CACHE, dict):
        _CACHE = {}
    return _CACHE


def _normalize_entries(raw: Any) -> list[dict[str, Any]]:
    """Turn topic value into [{word, image?, caption?}, ...]."""
    if raw is None:
        return []
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                w = item.strip()
                if w:
                    out.append({"word": w})
            elif isinstance(item, dict):
                w = str(item.get("word", "")).strip()
                if not w:
                    continue
                row: dict[str, Any] = {"word": w}
                if item.get("image"):
                    row["image"] = str(item["image"]).strip()
                if item.get("caption"):
                    row["caption"] = str(item["caption"]).strip()
                out.append(row)
        return out
    if isinstance(raw, dict) and "words" in raw:
        return _normalize_entries(raw["words"])
    return []


def items_for_topic(topic_slug: str) -> list[dict[str, Any]]:
    """Ordered lesson items for a topic slug (matches room name segment)."""
    slug = (topic_slug or "").strip().lower()
    raw = load_curriculum().get(slug)
    return _normalize_entries(raw)


def words_for_topic(topic_slug: str) -> list[str]:
    """Plain words only (for LLM fixed list)."""
    return [x["word"] for x in items_for_topic(topic_slug)]


def reload_curriculum() -> None:
    global _CACHE
    _CACHE = None
