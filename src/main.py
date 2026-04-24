from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from anthropic import Anthropic

from . import article, mailer, podcast, rss, script, slack, tts
from .summarize import summarize

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

    log.info("[1/7] Fetching RSS feeds...")
    items = rss.fetch_all(cfg["sources"], window_hours=sched.get("window_hours", 26))
    log.info("  %d unique items from last %dh", len(items), sched.get("window_hours", 26))
    if not items:
        log.warning("No items fetched, exiting cleanly")
        return 0

    log.info("[2/7] Extracting article bodies...")
    articles: list[dict] = []
    for it in items[: sched.get("candidate_pool_cap", 25)]:
        body = article.fetch_body(it.link, fallback=it.summary)
        articles.append(
            {
                "title": it.title,
                "link": it.link,
                "summary": it.summary,
                "body": body,
                "source": it.source,
                "published": it.published,
            }
        )
    log.info("  %d articles with bodies prepared", len(articles))

    client = Anthropic()
    model = cfg["claude"]["model"]

    log.info("[3/7] Summarizing with Claude...")
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

    log.info("[4/7] Selecting top articles...")
    top = sorted(summaries, key=lambda s: -s.importance)[: sched["max_articles"]]
    log.info("  Top %d selected", len(top))

    log.info("[5/7] Generating podcast script...")
    script_text = script.build_script(top, today, client=client, model=model)

    log.info("[6/7] Synthesizing audio (Google TTS)...")
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

    log.info("[7/7] Updating podcast feed and sending notifications...")
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
    email_to = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]
    email_from = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
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
            )
            notified = True
        except Exception as e:
            log.error("Email notification failed: %s", e)
    elif resend_key and not email_to:
        log.warning("RESEND_API_KEY is set but EMAIL_TO is empty — skipping email")

    if not notified:
        log.error(
            "No notification channel configured. Set SLACK_WEBHOOK_URL or RESEND_API_KEY + EMAIL_TO."
        )

    log.info("=== Holland Daily pipeline completed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
