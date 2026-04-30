from __future__ import annotations

import logging
import urllib.parse
from datetime import date

import requests

from .summarize import Summary

log = logging.getLogger(__name__)

CATEGORY_COLOR = {
    "政治・政策": "#2c3e50",
    "経済・ビジネス": "#c0392b",
    "社会・事件": "#8e44ad",
    "EU・国際関係": "#16a085",
    "テック・スタートアップ": "#2980b9",
    "生活・文化": "#d35400",
}


def get_audience_contacts(api_key: str, audience_id: str) -> list[str]:
    resp = requests.get(
        f"https://api.resend.com/audiences/{audience_id}/contacts",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    emails = [
        c["email"]
        for c in data.get("data", [])
        if c.get("email") and not c.get("unsubscribed")
    ]
    log.info("Fetched %d active contacts from audience %s", len(emails), audience_id)
    return emails


def send_via_resend(
    api_key: str,
    from_email: str,
    to_emails: list[str],
    summaries: list[Summary],
    episode_url: str,
    feed_url: str,
    today: date,
    show_name: str = "HARRO LIFE",
    subtitle: str = "オランダのニュースを、日本語で。",
    presented_by: str = "HARRO",
    shop_url: str = "",
    instagram_url: str = "",
    logo_url: str = "",
    site_url: str = "",
    unsubscribe_base_url: str = "",
) -> None:
    n = len(summaries)
    date_short = f"{today.month}/{today.day}"
    subject = f"{show_name}｜オランダの今日のニュース {n}本（{date_short}）"

    sent = 0
    failed = 0
    for recipient in to_emails:
        unsub_url = (
            f"{unsubscribe_base_url.rstrip('/')}/unsubscribe?email={urllib.parse.quote(recipient)}"
            if unsubscribe_base_url
            else ""
        )
        html = _build_html(
            summaries, episode_url, feed_url, today,
            show_name, subtitle, presented_by, shop_url, instagram_url, logo_url, site_url,
            unsub_url,
        )
        payload: dict = {
            "from": from_email,
            "to": [recipient],
            "subject": subject,
            "html": html,
        }
        if unsub_url:
            # RFC 2369 + RFC 8058 — Gmail/Apple Mail surface a one-click
            # Unsubscribe button at the top of the message when these are set.
            payload["headers"] = {
                "List-Unsubscribe": f"<{unsub_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
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
            sent += 1
        except Exception as e:
            failed += 1
            log.error("Failed to send to %s: %s", recipient, e)

    log.info("Email delivery: %d sent, %d failed (of %d total)", sent, failed, len(to_emails))


def _build_html(
    summaries: list[Summary],
    episode_url: str,
    feed_url: str,
    today: date,
    show_name: str,
    subtitle: str,
    presented_by: str,
    shop_url: str,
    instagram_url: str,
    logo_url: str,
    site_url: str,
    unsubscribe_url: str,
) -> str:
    grouped: dict[str, list[Summary]] = {}
    for s in sorted(summaries, key=lambda x: -x.importance):
        grouped.setdefault(s.category, []).append(s)

    date_str = today.strftime("%Y年%m月%d日")

    sections_html = ""
    for cat, items in grouped.items():
        color = CATEGORY_COLOR.get(cat, "#333333")
        articles_html = ""
        for s in items:
            articles_html += (
                '<tr><td style="padding:16px 0;border-bottom:1px solid #eee;">'
                f'<a href="{_esc(s.original_link)}" '
                'style="color:#1a1a1a;text-decoration:none;font-weight:600;'
                'font-size:16px;line-height:1.4;">'
                f"{_esc(s.title_ja)}</a>"
                f'<div style="color:#999;font-size:12px;margin-top:4px;">'
                f"{_esc(s.source)}</div>"
                f'<div style="color:#444;font-size:14px;line-height:1.6;margin-top:8px;">'
                f"{_esc(s.summary_ja)}</div>"
                "</td></tr>"
            )
        sections_html += (
            '<tr><td style="padding-top:32px;">'
            f'<div style="color:{color};font-size:12px;font-weight:700;'
            "letter-spacing:0.15em;text-transform:uppercase;"
            f'border-bottom:2px solid {color};padding-bottom:8px;">'
            f"{_esc(cat)}</div>"
            '<table width="100%" cellpadding="0" cellspacing="0">'
            f"{articles_html}</table>"
            "</td></tr>"
        )

    cta_buttons = _build_cta_buttons(episode_url, site_url)
    site_cta_block = _build_site_cta_block(site_url)
    header_block = _build_header_block(logo_url, show_name, presented_by)
    footer_block = _build_full_footer(shop_url, instagram_url, presented_by, feed_url, unsubscribe_url)

    return f"""<!doctype html>
<html lang="ja">
<body style="margin:0;padding:0;background:#faf7f2;font-family:-apple-system,'Helvetica Neue','Hiragino Sans','Yu Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#faf7f2;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;margin:40px 20px;border-radius:8px;overflow:hidden;">
{header_block}
<tr><td style="padding:32px 32px 8px;">
<div style="font-size:11px;color:#999;letter-spacing:0.15em;text-transform:uppercase;font-weight:700;">{date_str}</div>
<div style="color:#666;font-size:15px;margin-top:8px;margin-bottom:24px;">{_esc(subtitle)}</div>
{cta_buttons}
<table width="100%" cellpadding="0" cellspacing="0">{sections_html}</table>
{site_cta_block}
</td></tr>
{footer_block}
</table>
</td></tr>
</table>
</body></html>"""


def _build_header_block(logo_url: str, show_name: str, presented_by: str) -> str:
    """Navy band at top of email with HARRO LIFE logo (on-dark variant)."""
    if logo_url:
        logo_html = (
            f'<img src="{_esc(logo_url)}" alt="{_esc(show_name)}" '
            'style="height:36px;width:auto;display:block;border:0;" />'
        )
    else:
        logo_html = (
            f'<div style="font-size:22px;font-weight:700;color:#ffffff;'
            f'letter-spacing:-0.02em;">{_esc(show_name)}</div>'
        )
    return (
        '<tr><td style="background:#09202e;padding:22px 32px;">'
        f'{logo_html}'
        '</td></tr>'
    )


def _build_cta_buttons(episode_url: str, site_url: str) -> str:
    listen_btn = (
        f'<a href="{_esc(episode_url)}" '
        'style="display:inline-block;background:#1a1a1a;color:#ffffff;'
        'padding:14px 28px;text-decoration:none;border-radius:999px;'
        'font-size:14px;font-weight:500;margin:0 6px 8px 0;">'
        '今日のエピソードを聴く</a>'
    )
    if not site_url:
        return listen_btn
    site_btn = (
        f'<a href="{_esc(site_url)}" '
        'style="display:inline-block;background:#ffffff;color:#9E3E24;'
        'padding:13px 28px;text-decoration:none;border-radius:999px;'
        'border:1.5px solid #9E3E24;font-size:14px;font-weight:600;'
        'margin:0 6px 8px 0;">'
        'サイトで読む</a>'
    )
    return listen_btn + site_btn


def _build_site_cta_block(site_url: str) -> str:
    if not site_url:
        return ""
    return (
        '<div style="margin-top:40px;padding:28px;background:#faf7f2;'
        'border-radius:12px;text-align:center;">'
        '<div style="font-size:15px;color:#1a1a1a;margin-bottom:14px;'
        'line-height:1.6;">'
        'すべての記事は HARRO LIFE のサイトでも読めます'
        '</div>'
        f'<a href="{_esc(site_url)}" '
        'style="display:inline-block;background:#9E3E24;color:#ffffff;'
        'padding:14px 32px;text-decoration:none;border-radius:999px;'
        'font-size:14px;font-weight:600;">'
        'HARRO LIFE で全記事を読む →</a>'
        '</div>'
    )


def _build_full_footer(
    shop_url: str,
    instagram_url: str,
    presented_by: str,
    feed_url: str,
    unsubscribe_url: str,
) -> str:
    """Footer cell — HARRO links, podcast feed, unsubscribe link, copyright."""
    harro_links: list[str] = []
    if shop_url:
        harro_links.append(
            f'<a href="{_esc(shop_url)}" style="color:#1a1a1a;text-decoration:none;'
            f'border-bottom:1px solid #ddd;padding-bottom:1px;">HARRO Online Shop</a>'
        )
    if instagram_url:
        harro_links.append(
            f'<a href="{_esc(instagram_url)}" style="color:#1a1a1a;text-decoration:none;'
            f'border-bottom:1px solid #ddd;padding-bottom:1px;">Instagram</a>'
        )
    sep = '<span style="color:#ccc;margin:0 12px;">·</span>'
    harro_block = (
        '<div style="text-align:center;font-size:13px;color:#555;margin-bottom:18px;">'
        f'{sep.join(harro_links)}'
        '</div>'
    ) if harro_links else ""

    feed_block = (
        f'<div style="text-align:center;font-size:12px;color:#888;margin-bottom:18px;">'
        f'<a href="{_esc(feed_url)}" style="color:#9E3E24;text-decoration:none;">'
        'Podcast を RSS で購読する'
        '</a></div>'
    ) if feed_url else ""

    unsub_block = ""
    if unsubscribe_url:
        unsub_block = (
            '<div style="text-align:center;font-size:11px;color:#999;margin-bottom:8px;">'
            f'このメールが不要な場合は<a href="{_esc(unsubscribe_url)}" '
            'style="color:#9E3E24;text-decoration:underline;">配信停止</a>'
            'してください。'
            '</div>'
        )

    return (
        '<tr><td style="padding:24px 32px 32px;border-top:1px solid #eee;background:#faf7f2;">'
        f'{harro_block}'
        f'{feed_block}'
        f'{unsub_block}'
        '<div style="text-align:center;font-size:11px;color:#bbb;letter-spacing:0.1em;margin-top:6px;">'
        f'Presented by {_esc(presented_by)}'
        '</div>'
        '</td></tr>'
    )


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
