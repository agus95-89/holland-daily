from __future__ import annotations

import logging
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


def send_via_resend(
    api_key: str,
    from_email: str,
    to_emails: list[str],
    summaries: list[Summary],
    episode_url: str,
    feed_url: str,
    today: date,
    show_name: str = "Holland Daily",
    subtitle: str = "オランダの朝ニュース、日本語で。",
) -> None:
    subject = f"{show_name} — {today.strftime('%Y年%m月%d日')}"
    html = _build_html(summaries, episode_url, feed_url, today, show_name, subtitle)

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": to_emails,
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )
    resp.raise_for_status()
    msg_id = resp.json().get("id", "unknown")
    log.info("Email sent to %s (id=%s)", to_emails, msg_id)


def _build_html(
    summaries: list[Summary],
    episode_url: str,
    feed_url: str,
    today: date,
    show_name: str,
    subtitle: str,
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
            stars = "★" * s.importance + "☆" * (5 - s.importance)
            articles_html += (
                '<tr><td style="padding:16px 0;border-bottom:1px solid #eee;">'
                f'<a href="{_esc(s.original_link)}" '
                'style="color:#1a1a1a;text-decoration:none;font-weight:600;'
                'font-size:16px;line-height:1.4;">'
                f"{_esc(s.title_ja)}</a>"
                f'<div style="color:#999;font-size:12px;margin-top:4px;">'
                f"{_esc(s.source)} · {stars}</div>"
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

    return f"""<!doctype html>
<html lang="ja">
<body style="margin:0;padding:0;background:#faf7f2;font-family:-apple-system,'Helvetica Neue','Hiragino Sans','Yu Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#faf7f2;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;margin:40px 20px;">
<tr><td style="padding:40px 32px;">
<div style="font-size:11px;color:#999;letter-spacing:0.15em;text-transform:uppercase;">{date_str}</div>
<h1 style="font-size:40px;font-weight:200;letter-spacing:-0.04em;margin:4px 0 8px;color:#1a1a1a;">{_esc(show_name)}<span style="color:#ff6b35;">.</span></h1>
<div style="color:#666;font-size:15px;margin-bottom:28px;">{_esc(subtitle)}</div>
<a href="{_esc(episode_url)}" style="display:inline-block;background:#1a1a1a;color:#ffffff;padding:14px 28px;text-decoration:none;border-radius:999px;font-size:14px;font-weight:500;">今日のエピソードを聴く</a>
<table width="100%" cellpadding="0" cellspacing="0">{sections_html}</table>
<div style="margin-top:48px;padding-top:24px;border-top:1px solid #eee;color:#999;font-size:12px;">
<a href="{_esc(feed_url)}" style="color:#ff6b35;text-decoration:none;">Podcast RSSで購読</a>
</div>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
