from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator

log = logging.getLogger(__name__)


def _load_episode_meta(episodes_dir: Path, date_str: str) -> dict:
    """Read docs/episodes/{date}.json sidecar with content-aware title etc.

    Returns an empty dict when the sidecar is missing or malformed — callers
    fall back to date-only defaults so older episodes keep working.
    """
    meta_path = episodes_dir / f"{date_str}.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read episode meta %s: %s", meta_path, e)
        return {}


def update_feed(
    feed_path: Path,
    episodes_dir: Path,
    base_url: str,
    show_name: str,
    show_subtitle: str,
    author: str,
    email: str,
    itunes_category: str = "News",
    itunes_subcategory: str = "Daily News",
    retention_days: int = 30,
) -> None:
    base_url = base_url.rstrip("/")

    fg = FeedGenerator()
    fg.load_extension("podcast")

    fg.title(show_name)
    fg.description(show_subtitle)
    fg.link(href=base_url, rel="alternate")
    fg.link(href=f"{base_url}/feed.xml", rel="self")
    fg.language("ja")
    fg.author({"name": author, "email": email})
    fg.logo(f"{base_url}/artwork.png")
    fg.image(f"{base_url}/artwork.png")

    fg.podcast.itunes_author(author)
    fg.podcast.itunes_category(itunes_category, itunes_subcategory)
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_summary(show_subtitle)
    fg.podcast.itunes_owner(name=author, email=email)
    fg.podcast.itunes_image(f"{base_url}/artwork.png")

    episodes = sorted(episodes_dir.glob("*.mp3"))
    if not episodes:
        log.warning("No episodes found in %s", episodes_dir)

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=retention_days)).date()
    included = 0
    for ep in episodes:
        date_str = ep.stem
        try:
            ep_dt = datetime.fromisoformat(date_str)
        except ValueError:
            log.warning("Skipping non-dated episode file: %s", ep.name)
            continue
        if ep_dt.date() < cutoff_date:
            continue  # outside the retention window — keep file but skip in feed

        pub_date = ep_dt.replace(hour=7, minute=0, tzinfo=timezone.utc)
        size = ep.stat().st_size

        # Content-aware title: lead with date + the day's top headline if a
        # sidecar JSON exists; fall back to "show — date" otherwise.
        meta = _load_episode_meta(episodes_dir, date_str)
        ep_dt_local = ep_dt
        date_short = f"{ep_dt_local.month}/{ep_dt_local.day}"
        top_headline = (meta.get("top_headline") or "").strip()
        if top_headline:
            ep_title = f"{date_short}｜{top_headline}"
        else:
            ep_title = f"{show_name} — {date_str}"
        ep_description = (
            meta.get("description")
            or f"{date_str} のオランダニュース要約 (日本語・約13分)"
        )

        fe = fg.add_entry()
        fe.id(f"{base_url}/episodes/{ep.name}")
        fe.title(ep_title)
        fe.description(ep_description)
        fe.link(href=f"{base_url}/episodes/{ep.name}")
        fe.enclosure(f"{base_url}/episodes/{ep.name}", str(size), "audio/mpeg")
        fe.pubDate(pub_date)
        fe.podcast.itunes_duration("13:00")
        # Group all episodes under Season 1 — daily news shows conventionally
        # use a single ongoing season. Without this Apple shows "不明なシーズン".
        fe.podcast.itunes_season(1)
        included += 1

    feed_path.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(feed_path), pretty=True)
    log.info(
        "Feed updated: %s (%d/%d episodes within %d-day retention)",
        feed_path, included, len(episodes), retention_days,
    )
