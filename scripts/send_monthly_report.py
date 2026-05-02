"""Monthly HARRO LIFE internal report.

Pulls the previous month's site stats, computes net subscriber growth
from the daily snapshot history, and sends an HTML email.

Usage:
    python -m scripts.send_monthly_report           # send for real
    python -m scripts.send_monthly_report --preview # dump HTML to /tmp
"""
from __future__ import annotations

import calendar
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
    build_monthly_html,
    fetch_audience_active_count,
    fetch_cloudflare_stats,
    load_snapshot,
    resolve_titles,
    send_report_email,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("monthly-report")


SNAPSHOT_PATH = ROOT / "docs" / "reports" / "snapshot.json"
SITE_CONTENT_DIR = ROOT.parent / "harro-life-site" / "src" / "content"


def previous_month_window(now_nl: datetime) -> tuple[datetime, datetime, str]:
    """Return (since, until, label) covering the previous calendar month in NL."""
    first_of_this_month = datetime(now_nl.year, now_nl.month, 1, tzinfo=NL_TZ)
    last_month_end = first_of_this_month
    last_day_prev = first_of_this_month - timedelta(days=1)
    last_month_start = datetime(last_day_prev.year, last_day_prev.month, 1, tzinfo=NL_TZ)
    label = f"{last_month_start.year}年{last_month_start.month}月"
    return last_month_start, last_month_end, label


def month_growth_from_history(history: list[dict], year: int, month: int) -> tuple[int, int]:
    """Net subscriber growth (news, marketing) within the given month.

    Approach: find the latest entry whose date is before the month start
    (= "before-month baseline"), and the latest entry within the month
    (= "end-of-month value"). Difference is the net growth.
    """
    last_day_prev_month = datetime(year, month, 1, tzinfo=NL_TZ) - timedelta(days=1)
    last_day_prev_month_iso = last_day_prev_month.date().isoformat()
    end_of_target_month = (
        datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1, tzinfo=NL_TZ)
        - timedelta(days=1)
    )
    end_iso = end_of_target_month.date().isoformat()

    pre = None
    post = None
    for h in history:
        d = h.get("date", "")
        if d <= last_day_prev_month_iso:
            pre = h
        if d <= end_iso and d >= f"{year}-{month:02d}-01":
            post = h
    if not post:
        return 0, 0
    pre_news = (pre or {}).get("news_total", 0) if pre else 0
    pre_mkt = (pre or {}).get("marketing_total", 0) if pre else 0
    return (
        post.get("news_total", 0) - pre_news,
        post.get("marketing_total", 0) - pre_mkt,
    )


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

    now_nl = datetime.now(NL_TZ)
    since, until, label = previous_month_window(now_nl)
    log.info("Period: %s (NL)  [%s → %s)", label, since.isoformat(), until.isoformat())

    log.info("Fetching Cloudflare stats...")
    stats = fetch_cloudflare_stats(
        api_token=cf_token,
        account_tag=cf_account,
        site_tag=cf_site,
        since=since,
        until=until,
        top_limit=30,
    )
    log.info("PV=%d, Uniques=%d, top=%d", stats.pageviews, stats.uniques, len(stats.top_pages))

    if SITE_CONTENT_DIR.exists():
        resolve_titles(stats.top_pages, SITE_CONTENT_DIR)

    news_total = fetch_audience_active_count(resend_key, news_audience) if resend_key else 0
    marketing_total = fetch_audience_active_count(resend_key, marketing_audience) if resend_key and marketing_audience else 0

    snap = load_snapshot(SNAPSHOT_PATH)
    news_delta, marketing_delta = month_growth_from_history(
        snap.history, since.year, since.month,
    )

    html = build_monthly_html(
        period_label=label,
        stats=stats,
        site_url=site_url,
        news_total=news_total,
        news_delta=news_delta,
        marketing_total=marketing_total,
        marketing_delta=marketing_delta,
        logo_url=logo_url,
    )

    subject = f"[HARRO LIFE 内部] {label} マンスリーレポート"

    if preview_only:
        out = Path("/tmp/harro-monthly-report-preview.html")
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
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
