"""Phase 2 smoke test: process 1 article end-to-end (no TTS, no email).

Picks the first usable article from RSS, summarizes, expands to long-form,
and writes a single Markdown file to harro-life-site/src/content/news/.

Usage (from netherlands-news-bot/):
    .venv/bin/python -m scripts.smoke_phase2

The .env file in the project root is loaded automatically.

Env vars used:
    ANTHROPIC_API_KEY     (required)
    UNSPLASH_ACCESS_KEY   (optional, used as fallback when og:image absent)
    MARKDOWN_OUTPUT_DIR   (optional, defaults to ../harro-life-site/src/content/news)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

from src import article, images, long_form, markdown_writer, rss
from src.summarize import summarize

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("smoke")

CONFIG_PATH = ROOT / "config" / "sources.yaml"
DEFAULT_OUTPUT = ROOT.parent / "harro-life-site" / "src" / "content" / "news"


def main() -> int:
    log.info(
        "env: ANTHROPIC_API_KEY=%s UNSPLASH_ACCESS_KEY=%s",
        "set" if os.environ.get("ANTHROPIC_API_KEY") else "UNSET",
        "set" if os.environ.get("UNSPLASH_ACCESS_KEY") else "unset",
    )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY is not set in .env. Check the file and re-run.")
        return 2

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    sched = cfg["schedule"]
    today = datetime.now(ZoneInfo(sched["timezone"])).date()

    log.info("Fetching RSS (all sources, smoke uses first usable article)...")
    items = rss.fetch_all(cfg["sources"], window_hours=sched.get("window_hours", 26))
    if not items:
        log.error("No RSS items found in window")
        return 1

    client = Anthropic()
    model = cfg["claude"]["model"]
    max_body_chars = cfg["claude"]["max_body_chars"]

    article_dict = None
    for it in items[:10]:
        log.info("Trying: %s (%s)", it.title[:80], it.source)
        fetched = article.fetch_article(it.link, fallback=it.summary)
        if fetched["body"] and len(fetched["body"]) >= 200:
            article_dict = {
                "title": it.title,
                "link": it.link,
                "summary": it.summary,
                "body": fetched["body"],
                "og_image": fetched.get("image"),
                "source": it.source,
                "published": it.published,
            }
            break
    if article_dict is None:
        log.error("No article with usable body found in top 10")
        return 1

    log.info("Picked: %s", article_dict["title"])
    log.info("Body chars: %d, og:image: %s",
             len(article_dict["body"]), article_dict.get("og_image") or "(none)")

    log.info("Summarizing (Claude)...")
    summary = summarize(article_dict, client=client, model=model, max_body_chars=max_body_chars)
    if summary is None:
        log.error("Summarize failed")
        return 1
    log.info("  category=%s importance=%d", summary.category, summary.importance)

    log.info("Generating long-form (Claude)...")
    lf = long_form.expand(article_dict, summary, client=client, model=model, max_body_chars=max_body_chars)
    if lf is None:
        log.error("Long-form failed")
        return 1
    log.info("  title=%s", lf.title_ja)
    log.info("  body chars=%d, image_query=%s", len(lf.body_md), lf.image_query)

    image_url = article_dict.get("og_image")
    image_alt: str | None = None
    if image_url:
        image_alt = lf.title_ja
        log.info("  using og:image")
    else:
        unsplash_key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
        if unsplash_key and lf.image_query:
            log.info("  no og:image, querying Unsplash for '%s'", lf.image_query)
            image_url = images.search_unsplash(lf.image_query, unsplash_key)
            if image_url:
                image_alt = lf.image_query
                log.info("  Unsplash hit: %s", image_url)
            else:
                log.info("  Unsplash returned no image; will rely on CategoryPoster fallback")

    output_dir = Path(os.environ.get("MARKDOWN_OUTPUT_DIR") or DEFAULT_OUTPUT)
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{today.isoformat()}-smoke-test"
    path = output_dir / f"{slug}.md"
    md = markdown_writer.render_markdown(
        long_form=lf,
        summary=summary,
        pub_date=today,
        image_url=image_url,
        image_alt=image_alt,
        featured=False,
        breaking=False,
    )
    path.write_text(md, encoding="utf-8")

    log.info("=" * 60)
    log.info("SMOKE TEST OK")
    log.info("Wrote: %s", path)
    log.info("Next: cd ../harro-life-site && npm run build")
    log.info("After build, delete the smoke file with:")
    log.info("  rm %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
