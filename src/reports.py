"""HARRO LIFE internal reports — daily and monthly metrics email.

Pulls site analytics from Cloudflare Web Analytics (RUM beacon GraphQL
dataset), subscriber counts from Resend Audiences, computes deltas against
a JSON snapshot file, and renders an HTML email sent via Resend.

Phase 1 (this module):
- Article PVs (total + Top N)
- Mail subscriber count + delta
- Marketing opt-in count + delta

Phase 2 will add Spotify followers (HTML scrape); Phase 3 Apple ratings.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

CF_GRAPHQL_ENDPOINT = "https://api.cloudflare.com/client/v4/graphql/"
NL_TZ = ZoneInfo("Europe/Amsterdam")

# Brand palette (matches mailer.py / column_generator email styling)
COLOR_NAVY = "#09202E"
COLOR_BRAND = "#9E3E24"
COLOR_CREAM = "#EAE6C3"


@dataclass
class TopPage:
    path: str
    pageviews: int
    title: str = ""


@dataclass
class SiteStats:
    pageviews: int
    visits: int
    uniques: int
    top_pages: list[TopPage]


@dataclass
class Snapshot:
    """Persisted state used to compute day-over-day / month-over-month deltas."""

    last_run_at: str = ""  # ISO timestamp
    news_audience_total: int = 0
    marketing_audience_total: int = 0
    history: list[dict] = field(default_factory=list)
    # history entries: {"date": "2026-05-01", "news_total": 1, "marketing_total": 0}


# ──────────────────────────────────────────────────────────────────────
# Snapshot persistence
# ──────────────────────────────────────────────────────────────────────


def load_snapshot(path: Path) -> Snapshot:
    if not path.exists():
        log.info("No snapshot at %s — initialising fresh state.", path)
        return Snapshot()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Snapshot(
            last_run_at=d.get("last_run_at", ""),
            news_audience_total=int(d.get("news_audience_total", 0)),
            marketing_audience_total=int(d.get("marketing_audience_total", 0)),
            history=list(d.get("history", [])),
        )
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("Could not read snapshot %s: %s — starting fresh.", path, e)
        return Snapshot()


def save_snapshot(path: Path, snap: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_run_at": snap.last_run_at,
                "news_audience_total": snap.news_audience_total,
                "marketing_audience_total": snap.marketing_audience_total,
                "history": snap.history,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────────────
# Cloudflare Web Analytics (RUM beacon)
# ──────────────────────────────────────────────────────────────────────


def fetch_cloudflare_stats(
    api_token: str,
    account_tag: str,
    site_tag: str,
    since: datetime,
    until: datetime,
    top_limit: int = 30,
) -> SiteStats:
    """Hit Cloudflare GraphQL Analytics for RUM page-load events.

    `since` / `until` are timezone-aware datetimes; converted to UTC for the
    API call. Returns total PV / visits / uniques and the top N requestPaths
    by PV.
    """
    since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    query = """
    query Stats($accountTag: String!, $siteTag: String!, $since: Time!, $until: Time!, $topLimit: Int!) {
      viewer {
        accounts(filter: {accountTag: $accountTag}, limit: 1) {
          total: rumPageloadEventsAdaptiveGroups(
            limit: 1
            filter: {siteTag: $siteTag, datetime_geq: $since, datetime_lt: $until}
          ) {
            count
            sum { visits }
            uniq { uniques }
          }
          topPages: rumPageloadEventsAdaptiveGroups(
            limit: $topLimit
            filter: {siteTag: $siteTag, datetime_geq: $since, datetime_lt: $until}
            orderBy: [count_DESC]
          ) {
            count
            dimensions { metric }
          }
        }
      }
    }
    """

    resp = requests.post(
        CF_GRAPHQL_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "variables": {
                "accountTag": account_tag,
                "siteTag": site_tag,
                "since": since_iso,
                "until": until_iso,
                "topLimit": top_limit,
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("errors"):
        log.error("Cloudflare GraphQL errors: %s", payload["errors"])
        raise RuntimeError(f"Cloudflare GraphQL: {payload['errors']}")

    accounts = (payload.get("data") or {}).get("viewer", {}).get("accounts", [])
    if not accounts:
        log.warning("No account data returned for tag=%s", account_tag)
        return SiteStats(0, 0, 0, [])

    acct = accounts[0]
    total_rows = acct.get("total") or []
    if total_rows:
        first = total_rows[0]
        pv = int(first.get("count", 0))
        visits = int((first.get("sum") or {}).get("visits", 0) or 0)
        uniques = int((first.get("uniq") or {}).get("uniques", 0) or 0)
    else:
        pv = visits = uniques = 0

    top_pages: list[TopPage] = []
    for row in acct.get("topPages") or []:
        dims = row.get("dimensions") or {}
        path = dims.get("metric") or dims.get("requestPath") or ""
        if not path:
            continue
        top_pages.append(TopPage(path=path, pageviews=int(row.get("count", 0))))

    return SiteStats(pageviews=pv, visits=visits, uniques=uniques, top_pages=top_pages)


# ──────────────────────────────────────────────────────────────────────
# Resend Audience counts
# ──────────────────────────────────────────────────────────────────────


def fetch_audience_active_count(api_key: str, audience_id: str) -> int:
    """Active (not unsubscribed) contact count."""
    if not audience_id:
        return 0
    resp = requests.get(
        f"https://api.resend.com/audiences/{audience_id}/contacts",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    if not resp.ok:
        log.error("Resend audience fetch failed: %s %s", resp.status_code, resp.text[:200])
        return 0
    data = resp.json()
    return sum(1 for c in (data.get("data") or []) if not c.get("unsubscribed"))


# ──────────────────────────────────────────────────────────────────────
# Title resolution — turn /news/{slug}/ into the article's Japanese title
# ──────────────────────────────────────────────────────────────────────


def resolve_titles(top_pages: list[TopPage], site_content_dir: Path) -> None:
    """Mutate TopPage.title in-place from {site_content_dir}/{kind}/{slug}.md."""
    for tp in top_pages:
        # /news/2026-05-01-03/ or /columns/auto-2026-W18-food/
        parts = [p for p in tp.path.strip("/").split("/") if p]
        if len(parts) < 2:
            continue
        kind = parts[0]
        slug = parts[-1]
        if kind == "news":
            md = site_content_dir / "news" / f"{slug}.md"
        elif kind == "columns":
            md = site_content_dir / "columns" / f"{slug}.md"
        else:
            continue
        if not md.exists():
            continue
        try:
            head = md.read_text(encoding="utf-8")[:2000]
            m = re.search(r"^title:\s*(.+?)$", head, re.MULTILINE)
            if m:
                tp.title = m.group(1).strip().strip("'\"")
        except OSError:
            continue


# ──────────────────────────────────────────────────────────────────────
# HTML rendering
# ──────────────────────────────────────────────────────────────────────


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else f"{n}"


def _build_top_table(top_pages: list[TopPage], site_url: str, max_rows: int) -> str:
    """Render Top N article table as inline-styled HTML."""
    if not top_pages:
        return '<p style="color:#999;font-size:13px;">アクセスデータがまだ入っていません。</p>'
    rows: list[str] = []
    for i, tp in enumerate(top_pages[:max_rows], 1):
        title = tp.title or "(タイトル取得失敗)"
        url = f"{site_url.rstrip('/')}{tp.path}"
        rows.append(
            '<tr>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;font-size:13px;color:#999;width:40px;">#{i}</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;font-size:14px;color:#1a1a1a;line-height:1.5;">'
            f'<a href="{_esc(url)}" style="color:#1a1a1a;text-decoration:none;">{_esc(title)}</a>'
            f'<div style="font-size:11px;color:#999;margin-top:3px;font-family:monospace;">{_esc(tp.path)}</div>'
            '</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;font-size:14px;color:#1a1a1a;font-weight:700;text-align:right;width:80px;">{tp.pageviews:,}</td>'
            '</tr>'
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
        '<thead><tr>'
        '<th style="text-align:left;padding:8px;font-size:11px;color:#999;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;border-bottom:2px solid #1a1a1a;">#</th>'
        '<th style="text-align:left;padding:8px;font-size:11px;color:#999;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;border-bottom:2px solid #1a1a1a;">記事</th>'
        '<th style="text-align:right;padding:8px;font-size:11px;color:#999;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;border-bottom:2px solid #1a1a1a;">PV</th>'
        '</tr></thead><tbody>'
        + "".join(rows) +
        '</tbody></table>'
    )


def _build_kpi_block(label: str, total: int, delta: int | None, delta_label: str = "前日比") -> str:
    """Single KPI tile — total + delta below."""
    delta_html = ""
    if delta is not None:
        color = "#1f7c5a" if delta > 0 else ("#999" if delta == 0 else "#9E3E24")
        delta_html = (
            f'<div style="font-size:13px;color:{color};margin-top:4px;font-weight:600;">'
            f'{delta_label} {_signed(delta)}</div>'
        )
    return (
        '<td style="padding:18px 12px;background:#faf7f2;border-radius:8px;vertical-align:top;">'
        f'<div style="font-size:11px;color:#666;letter-spacing:0.1em;text-transform:uppercase;font-weight:700;margin-bottom:6px;">'
        f'{_esc(label)}</div>'
        f'<div style="font-size:28px;color:#1a1a1a;font-weight:700;line-height:1;">{total:,}</div>'
        f'{delta_html}'
        '</td>'
    )


def build_daily_html(
    *,
    report_date: datetime,
    stats: SiteStats,
    site_url: str,
    news_total: int,
    news_delta: int | None,
    marketing_total: int,
    marketing_delta: int | None,
    logo_url: str,
) -> str:
    date_str = report_date.strftime("%Y年%m月%d日 (%a)")
    period_label = "前日"

    kpi_row = (
        '<table width="100%" cellpadding="0" cellspacing="6" style="border-collapse:separate;">'
        '<tr>'
        + _build_kpi_block("総 PV", stats.pageviews, None)
        + _build_kpi_block("UU", stats.uniques, None)
        + '</tr><tr>'
        + _build_kpi_block("メルマガ累計", news_total, news_delta, f"{period_label}比")
        + _build_kpi_block("マーケ許諾累計", marketing_total, marketing_delta, f"{period_label}比")
        + '</tr></table>'
    )

    top_html = _build_top_table(stats.top_pages, site_url, max_rows=10)

    logo_html = (
        f'<img src="{_esc(logo_url)}" alt="HARRO LIFE" style="height:28px;width:auto;display:block;border:0;" />'
        if logo_url else
        '<div style="font-size:18px;font-weight:700;color:#fff;letter-spacing:-0.02em;">HARRO LIFE</div>'
    )

    return _wrap_html(
        title="デイリーレポート",
        subtitle=date_str + " · 内部レポート",
        logo_html=logo_html,
        body_html=f"""
{kpi_row}
<div style="margin-top:32px;">
  <div style="font-size:11px;color:#999;letter-spacing:0.15em;text-transform:uppercase;font-weight:700;margin-bottom:12px;">Top 10 記事 ({date_str})</div>
  {top_html}
</div>
""",
    )


def build_monthly_html(
    *,
    period_label: str,  # e.g. "2026年4月"
    stats: SiteStats,
    site_url: str,
    news_total: int,
    news_delta: int,  # net adds during the month
    marketing_total: int,
    marketing_delta: int,
    logo_url: str,
) -> str:
    kpi_row = (
        '<table width="100%" cellpadding="0" cellspacing="6" style="border-collapse:separate;">'
        '<tr>'
        + _build_kpi_block("月間 PV", stats.pageviews, None)
        + _build_kpi_block("月間 UU", stats.uniques, None)
        + '</tr><tr>'
        + _build_kpi_block("メルマガ累計", news_total, news_delta, "月間純増")
        + _build_kpi_block("マーケ許諾累計", marketing_total, marketing_delta, "月間純増")
        + '</tr></table>'
    )

    top_html = _build_top_table(stats.top_pages, site_url, max_rows=30)

    logo_html = (
        f'<img src="{_esc(logo_url)}" alt="HARRO LIFE" style="height:28px;width:auto;display:block;border:0;" />'
        if logo_url else
        '<div style="font-size:18px;font-weight:700;color:#fff;letter-spacing:-0.02em;">HARRO LIFE</div>'
    )

    return _wrap_html(
        title="マンスリーレポート",
        subtitle=period_label + " · 内部レポート",
        logo_html=logo_html,
        body_html=f"""
{kpi_row}
<div style="margin-top:32px;">
  <div style="font-size:11px;color:#999;letter-spacing:0.15em;text-transform:uppercase;font-weight:700;margin-bottom:12px;">Top 30 記事 ({_esc(period_label)})</div>
  {top_html}
</div>
""",
    )


def _wrap_html(*, title: str, subtitle: str, logo_html: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="ja">
<body style="margin:0;padding:0;background:#faf7f2;font-family:-apple-system,'Helvetica Neue','Hiragino Sans','Yu Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#faf7f2;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;margin:40px 20px;border-radius:8px;overflow:hidden;">
<tr><td style="background:{COLOR_NAVY};padding:22px 32px;">{logo_html}</td></tr>
<tr><td style="padding:32px 36px 40px;">
<div style="font-size:11px;color:#888;letter-spacing:0.15em;text-transform:uppercase;font-weight:700;margin-bottom:6px;">INTERNAL REPORT</div>
<h1 style="font-size:24px;color:{COLOR_NAVY};margin:0 0 6px;font-weight:700;">{_esc(title)}</h1>
<div style="font-size:13px;color:#666;margin-bottom:28px;">{_esc(subtitle)}</div>
{body_html}
</td></tr>
<tr><td style="padding:18px 36px 24px;border-top:1px solid #eee;background:#faf7f2;text-align:center;font-size:11px;color:#bbb;letter-spacing:0.1em;">
HARRO LIFE Editorial · このメールは内部レポートです (転送禁止)
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


# ──────────────────────────────────────────────────────────────────────
# Email send
# ──────────────────────────────────────────────────────────────────────


def send_report_email(
    *,
    api_key: str,
    from_email: str,
    to_email: str,
    subject: str,
    html: str,
) -> bool:
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Report email sent — id=%s", resp.json().get("id", "?"))
        return True
    except Exception as e:
        log.error("Resend send failed: %s", e)
        return False
