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

from . import article, images, long_form, mailer, markdown_writer, podcast, rss, script, slack, tts
from .summarize import summarize

# Load .env if present (no-op on CI where env vars come from secrets)
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("holland-daily")

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sources.yaml"
DOCS_DIR = ROOT / "docs"
EPISODES_DIR = DOCS_DIR / "episodes"
FEED_PATH = DOCS_DIR / "feed.xml"

# Phase 2: where Markdown news articles are written for harro-life-site.
# Override via MARKDOWN_OUTPUT_DIR env var (CI sets this to a workspace path).
DEFAULT_MARKDOWN_DIR = ROOT.parent / "harro-life-site" / "src" / "content" / "news"


def should_run(target_hour: int, tz: str) -> bool:
    now = datetime.now(ZoneInfo(tz))
    return now.hour == target_hour


def main() -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    sched = cfg["schedule"]

    if os.environ.get("FORCE_RUN") != "1":
        if not should_run(sched["target_hour_nl"], sched["timezone"]):
            now_local = datetime.now(ZoneInfo(sched["timezone"]))
            log.info(
                "Not target hour (local=%s, target=%d). Skipping.",
                now_local.strftime("%H:%M %Z"), sched["target_hour_nl"],
            )
            return 0

    today = datetime.now(ZoneInfo(sched["timezone"])).date()
    log.info("=== Holland Daily pipeline run for %s ===", today)

    log.info("[1/8] Fetching RSS feeds...")
    items = rss.fetch_all(cfg["sources"], window_hours=sched.get("window_hours", 26))
    log.info("  %d unique items from last %dh", len(items), sched.get("window_hours", 26))
    if not items:
        log.warning("No items fetched, exiting cleanly")
        return 0

    log.info("[2/8] Extracting article bodies and og:image...")
    articles: list[dict] = []
    for it in items[: sched.get("candidate_pool_cap", 25)]:
        fetched = article.fetch_article(it.link, fallback=it.summary)
        articles.append(
            {
                "title": it.title,
                "link": it.link,
                "summary": it.summary,
                "body": fetched["body"],
                "og_image": fetched.get("image"),
                "source": it.source,
                "published": it.published,
            }
        )
    log.info("  %d articles with bodies prepared", len(articles))

    client = Anthropic()
    model = cfg["claude"]["model"]

    log.info("[3/8] Summarizing with Claude...")
    summaries = []
    for i, a in enumerate(articles, 1):
        s = summarize(a, client=client, model=model,
                      max_body_chars=cfg["claude"]["max_body_chars"])
        if s is not None:
            summaries.append(s)
        if i % 5 == 0:
            log.info("  %d/%d summarized", i, len(articles))
    log.info("  %d / %d articles summarized", len(summaries), len(articles))
    if not summaries:
        log.warning("No summaries produced, exiting")
        return 0

    log.info("[4/8] Selecting top articles...")
    top = sorted(summaries, key=lambda s: -s.importance)[: sched["max_articles"]]
    log.info("  Top %d selected", len(top))

    log.info("[5/8] Generating long-form Markdown articles for harro-life-site...")
    markdown_dir = Path(os.environ.get("MARKDOWN_OUTPUT_DIR") or DEFAULT_MARKDOWN_DIR)
    unsplash_key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    article_by_link = {a["link"]: a for a in articles}
    written_paths: list[Path] = []
    for i, summary_obj in enumerate(top, 1):
        article_dict = article_by_link.get(summary_obj.original_link)
        if article_dict is None:
            log.warning("Article body not found for %s, skipping Markdown", summary_obj.original_link)
            continue
        long_form_obj = long_form.expand(
            article_dict,
            summary_obj,
            client=client,
            model=model,
            max_body_chars=cfg["claude"]["max_body_chars"],
        )
        if long_form_obj is None:
            log.warning("Long-form failed for %s, skipping Markdown", summary_obj.original_link)
            continue
        image_url = article_dict.get("og_image")
        image_alt: str | None = None
        if image_url:
            image_alt = long_form_obj.title_ja
        elif unsplash_key and long_form_obj.image_query:
            image_url = images.search_unsplash(long_form_obj.image_query, unsplash_key)
            if image_url:
                image_alt = long_form_obj.image_query
        path = markdown_writer.write_news_markdown(
            long_form=long_form_obj,
            summary=summary_obj,
            pub_date=today,
            index=i,
            output_dir=markdown_dir,
            image_url=image_url,
            image_alt=image_alt,
            featured=(i == 1),
            breaking=(summary_obj.importance >= 5),
        )
        written_paths.append(path)
        if i % 3 == 0:
            log.info("  %d/%d Markdown files written", i, len(top))
    log.info("  %d Markdown files written to %s", len(written_paths), markdown_dir)

    log.info("[6/8] Generating podcast script...")
    script_text = script.build_script(top, today, client=client, model=model)

    log.info("[7/8] Synthesizing audio (Google TTS)...")
    mp3_path = EPISODES_DIR / f"{today.isoformat()}.mp3"
    tts.script_to_mp3(
        script_text,
        mp3_path,
        intro_voice=cfg["tts"]["intro_voice"],
        body_voice=cfg["tts"]["body_voice"],
        speaking_rate=cfg["tts"]["speaking_rate"],
    )

    base_url = os.environ.get("PODCAST_BASE_URL") or cfg["podcast"]["base_url"]
    base_url = base_url.rstrip("/")

    log.info("[8/8] Updating podcast feed and sending notifications...")
    podcast.update_feed(
        feed_path=FEED_PATH,
        episodes_dir=EPISODES_DIR,
        base_url=base_url,
        show_name=cfg["podcast"]["show_name"],
        show_subtitle=cfg["podcast"]["show_subtitle"],
        author=cfg["podcast"]["author"],
        email=cfg["podcast"]["email"],
        itunes_category=cfg["podcast"]["itunes_category"],
        itunes_subcategory=cfg["podcast"]["itunes_subcategory"],
    )

    episode_url = f"{base_url}/episodes/{mp3_path.name}"
    feed_url = f"{base_url}/feed.xml"

    notified = False

    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            slack.post(
                webhook_url=slack_url,
                summaries=top,
                episode_url=episode_url,
                feed_url=feed_url,
                today=today,
                username=cfg["slack"]["username"],
                icon_emoji=cfg["slack"]["icon_emoji"],
            )
            notified = True
        except Exception as e:
            log.error("Slack notification failed: %s", e)

    resend_key = os.environ.get("RESEND_API_KEY")
    audience_id = os.environ.get("RESEND_AUDIENCE_ID", "").strip()
    email_from = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")

    email_to: list[str] = []
    if resend_key and audience_id:
        try:
            email_to = mailer.get_audience_contacts(resend_key, audience_id)
        except Exception as e:
            log.error("Failed to fetch audience contacts: %s", e)

    if not email_to:
        email_to = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

    if resend_key and email_to:
        try:
            mailer.send_via_resend(
                api_key=resend_key,
                from_email=email_from,
                to_emails=email_to,
                summaries=top,
                episode_url=episode_url,
                feed_url=feed_url,
                today=today,
                show_name=cfg["podcast"]["show_name"],
                subtitle=cfg["podcast"]["show_subtitle"],
                presented_by=cfg["podcast"].get("presented_by", "HARRO"),
                shop_url=cfg.get("links", {}).get("harro_shop", ""),
                instagram_url=cfg.get("links", {}).get("harro_instagram", ""),
                logo_url=cfg.get("links", {}).get("harro_logo", ""),
            )
            notified = True
        except Exception as e:
            log.error("Email notification failed: %s", e)
    elif resend_key and not email_to:
        log.warning("RESEND_API_KEY is set but no recipients (Audience empty and EMAIL_TO empty)")

    if not notified:
        log.error(
            "No notification channel configured. Set SLACK_WEBHOOK_URL or RESEND_API_KEY + EMAIL_TO."
        )

    log.info("=== Holland Daily pipeline completed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
