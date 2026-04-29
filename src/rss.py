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


def fetch_all(
    sources: list[dict],
    window_hours: int = 26,
    per_source_cap: int = 8,
) -> list[FeedItem]:
    """Fetch RSS feeds from each source, keeping at most `per_source_cap`
    most-recent in-window items per source so a single high-volume feed
    (e.g. NOS) cannot starve the candidate pool of other sources.
    """
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

        # Sort entries newest-first then take up to per_source_cap.
        in_window: list[tuple[datetime, FeedItem]] = []
        for entry in parsed.entries:
            pub = _parse_published(entry)
            if pub is None or pub < cutoff:
                continue
            in_window.append((
                pub,
                FeedItem(
                    title=(entry.get("title") or "").strip(),
                    link=(entry.get("link") or "").strip(),
                    summary=_strip_html(entry.get("summary") or ""),
                    published=pub,
                    source=src["name"],
                ),
            ))
        in_window.sort(key=lambda x: x[0], reverse=True)
        kept = [fi for _, fi in in_window[:per_source_cap]]
        log.info("  %s: %d in-window, kept %d", src["name"], len(in_window), len(kept))
        items.extend(kept)

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
