"""Unsplash image search for news article fallback hero images.

Used when an article has no og:image. Free Demo plan = 50 req/h, plenty for
10 articles/day.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"


def search_unsplash(
    query: str,
    access_key: str,
    timeout: int = 10,
) -> str | None:
    """Return the regular-size URL of the top match, or None on failure."""
    if not query.strip() or not access_key:
        return None
    try:
        resp = requests.get(
            UNSPLASH_SEARCH_URL,
            params={
                "query": query,
                "per_page": 1,
                "orientation": "landscape",
                "content_filter": "high",
            },
            headers={"Authorization": f"Client-ID {access_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if not results:
            log.info("Unsplash returned no results for query '%s'", query)
            return None
        url = results[0].get("urls", {}).get("regular")
        if not url:
            return None
        return url
    except Exception as e:
        log.warning("Unsplash search failed for '%s': %s", query, e)
        return None
