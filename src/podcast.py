from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator

log = logging.getLogger(__name__)


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

    for ep in episodes:
        date_str = ep.stem
        try:
            pub_date = datetime.fromisoformat(date_str).replace(
                hour=7, minute=0, tzinfo=timezone.utc
            )
        except ValueError:
            log.warning("Skipping non-dated episode file: %s", ep.name)
            continue

        size = ep.stat().st_size
        fe = fg.add_entry()
        fe.id(f"{base_url}/episodes/{ep.name}")
        fe.title(f"{show_name} — {date_str}")
        fe.description(f"{date_str} のオランダニュース要約 (日本語・約13分)")
        fe.link(href=f"{base_url}/episodes/{ep.name}")
        fe.enclosure(f"{base_url}/episodes/{ep.name}", str(size), "audio/mpeg")
        fe.pubDate(pub_date)
        fe.podcast.itunes_duration("13:00")

    feed_path.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(feed_path), pretty=True)
    log.info("Feed updated: %s (%d episodes)", feed_path, len(episodes))
