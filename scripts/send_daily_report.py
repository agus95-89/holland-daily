"""Daily HARRO LIFE internal report.

Pulls yesterday's site stats + audience counts, computes deltas vs. the
last-known snapshot, sends an HTML email to REPORT_TO via Resend.

Usage:
    python -m scripts.send_daily_report           # send for real
    python -m scripts.send_daily_report --preview # dump HTML to /tmp, don't send

Env vars (loaded from .env or CI secrets):
    CF_API_TOKEN          Cloudflare API token (Account.Analytics:Read)
    CF_ACCOUNT_ID         Cloudflare account tag
    CF_SITE_TAG           Web Analytics site tag (the beacon token)
    RESEND_API_KEY        Resend
    RESEND_AUDIENCE_ID    News audience UUID
    MARKETING_AUDIENCE_ID HARRO Marketing audience UUID (optional)
    EMAIL_FROM            Sender (defaults to onboarding@resend.dev sandbox)
    REPORT_TO             Recipient (defaults to suga@harrojp.com)
    SITE_URL              For absolute links (default https://harro-life-site.pages.dev)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

from src.reports import (  # noqa: E402
    NL_TZ,
    Snapshot,
    build_daily_html,
    fetch_audience_active_count,
    fetch_cloudflare_stats,
    load_snapshot,
    resolve_titles,
    save_snapshot,
    send_report_email,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("daily-report")


SNAPSHOT_PATH = ROOT / "docs" / "reports" / "snapshot.json"
SITE_CONTENT_DIR = ROOT.parent / "harro-life-site" / "src" / "content"


def main() -> int:
    preview_only = "--preview" in sys.argv

    cf_token = os.environ.get("CF_API_TOKEN", "").strip()
    cf_account = os.environ.get("CF_ACCOUNT_ID", "").strip()
    cf_site = os.environ.get("CF_SITE_TAG", "").strip()
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    news_audience = os.environ.get("RESEND_AUDIENCE_ID", "").strip()
    marketing_audience = os.environ.get("MARKETING_AUDIENCE_ID", "").strip()
    from_email = os.environ.get("EMAIL_FROM", "onboarding@resend.dev").strip()
    report_to = os.environ.get("REPORT_TO", "suga@harrojp.com").strip()
    site_url = os.environ.get("SITE_URL", "https://harro-life-site.pages.dev").strip()
    logo_url = os.environ.get(
        "HARRO_LOGO_URL",
        "https://harro-life-site.pages.dev/images/brand/harro-life-on-dark.png",
    ).strip()

    missing = [k for k, v in {
        "CF_API_TOKEN": cf_token,
        "CF_ACCOUNT_ID": cf_account,
        "CF_SITE_TAG": cf_site,
    }.items() if not v]
    if missing:
        log.error("Missing env vars: %s", missing)
        return 1

    # Yesterday in NL local — daily report covers the day that just ended.
    now_nl = datetime.now(NL_TZ)
    today = now_nl.date()
    yesterday = today - timedelta(days=1)
    since = datetime.combine(yesterday, datetime.min.time(), tzinfo=NL_TZ)
    until = datetime.combine(today, datetime.min.time(), tzinfo=NL_TZ)
    log.info("Window: %s → %s (NL)", since.isoformat(), until.isoformat())

    log.info("Fetching Cloudflare stats...")
    stats = fetch_cloudflare_stats(
        api_token=cf_token,
        account_tag=cf_account,
        site_tag=cf_site,
        since=since,
        until=until,
        top_limit=30,
    )
    log.info("PV=%d, Visits=%d, Uniques=%d, top=%d",
             stats.pageviews, stats.visits, stats.uniques, len(stats.top_pages))

    if SITE_CONTENT_DIR.exists():
        resolve_titles(stats.top_pages, SITE_CONTENT_DIR)

    log.info("Fetching audience counts...")
    news_total = fetch_audience_active_count(resend_key, news_audience) if resend_key else 0
    marketing_total = fetch_audience_active_count(resend_key, marketing_audience) if resend_key and marketing_audience else 0
    log.info("News audience=%d, Marketing audience=%d", news_total, marketing_total)

    # Delta vs. last snapshot.
    snap = load_snapshot(SNAPSHOT_PATH)
    has_baseline = bool(snap.last_run_at)
    news_delta = (news_total - snap.news_audience_total) if has_baseline else None
    marketing_delta = (marketing_total - snap.marketing_audience_total) if has_baseline else None

    html = build_daily_html(
        report_date=since,  # represents yesterday
        stats=stats,
        site_url=site_url,
        news_total=news_total,
        news_delta=news_delta,
        marketing_total=marketing_total,
        marketing_delta=marketing_delta,
        logo_url=logo_url,
    )

    subject = f"[HARRO LIFE 内部] {since.month}/{since.day} デイリーレポート"

    if preview_only:
        out = Path("/tmp/harro-daily-report-preview.html")
        out.write_text(html, encoding="utf-8")
        log.info("Preview written to %s", out)
        log.info("Subject would be: %s", subject)
        return 0

    if not resend_key:
        log.error("RESEND_API_KEY not set — cannot send")
        return 1

    ok = send_report_email(
        api_key=resend_key,
        from_email=from_email,
        to_email=report_to,
        subject=subject,
        html=html,
    )
    if not ok:
        return 1

    # Update snapshot AFTER successful send so a failed send leaves the
    # baseline unchanged for retry.
    snap.last_run_at = now_nl.isoformat()
    snap.news_audience_total = news_total
    snap.marketing_audience_total = marketing_total
    snap.history.append({
        "date": yesterday.isoformat(),
        "news_total": news_total,
        "marketing_total": marketing_total,
        "pv": stats.pageviews,
        "uniques": stats.uniques,
    })
    # Keep history bounded — last 400 days is plenty for trend tracking.
    snap.history = snap.history[-400:]
    save_snapshot(SNAPSHOT_PATH, snap)
    log.info("Snapshot updated: %s", SNAPSHOT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
