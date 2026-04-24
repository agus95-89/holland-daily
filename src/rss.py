from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser

log = logging.getLogger(__name__)


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    published: datetime
    source: str


def fetch_all(sources: list[dict], window_hours: int = 26) -> list[FeedItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    items: list[FeedItem] = []

    for src in sources:
        if not src.get("enabled", True):
            continue
        try:
            parsed = feedparser.parse(src["url"])
        except Exception as e:
            log.warning("Failed to parse %s: %s", src["name"], e)
            continue

        for entry in parsed.entries:
            pub = _parse_published(entry)
            if pub is None or pub < cutoff:
                continue
            items.append(
                FeedItem(
                    title=(entry.get("title") or "").strip(),
                    link=(entry.get("link") or "").strip(),
                    summary=_strip_html(entry.get("summary") or ""),
                    published=pub,
                    source=src["name"],
                )
            )

    return _dedupe(items)


def _parse_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def _dedupe(items: list[FeedItem]) -> list[FeedItem]:
    seen: set[str] = set()
    out: list[FeedItem] = []
    for it in items:
        key = it.link or it.title
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _strip_html(raw: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()
