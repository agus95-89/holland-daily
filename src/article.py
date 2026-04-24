from __future__ import annotations

import logging

import trafilatura

log = logging.getLogger(__name__)


def fetch_body(url: str, fallback: str = "") -> str:
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return fallback
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
            deduplicate=True,
        )
        if text and len(text) >= 200:
            return text
    except Exception as e:
        log.warning("Failed to fetch article %s: %s", url, e)
    return fallback
