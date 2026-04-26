from __future__ import annotations

import logging

import trafilatura

log = logging.getLogger(__name__)


def fetch_body(url: str, fallback: str = "") -> str:
    """Backward-compatible body-only fetch. Prefer fetch_article() in new code."""
    return fetch_article(url, fallback)["body"]


def fetch_article(url: str, fallback: str = "") -> dict:
    """Fetch a page once and return both body text and og:image URL.

    Returns: {"body": str, "image": str | None}
    """
    result = {"body": fallback, "image": None}
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return result
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
            deduplicate=True,
        )
        if text and len(text) >= 200:
            result["body"] = text
        try:
            meta = trafilatura.extract_metadata(downloaded)
        except Exception:
            meta = None
        if meta:
            img = getattr(meta, "image", None)
            if isinstance(img, str) and img.startswith("http"):
                result["image"] = img
    except Exception as e:
        log.warning("Failed to fetch article %s: %s", url, e)
    return result
