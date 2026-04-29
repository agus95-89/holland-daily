from __future__ import annotations

import logging
from datetime import date

import requests

from .summarize import Summary

log = logging.getLogger(__name__)

CATEGORY_EMOJI = {
    "政治・政策": ":classical_building:",
    "経済・ビジネス": ":briefcase:",
    "社会・事件": ":cityscape:",
    "EU・国際関係": ":earth_africa:",
    "テック・スタートアップ": ":rocket:",
    "生活・文化": ":art:",
}


def post(
    webhook_url: str,
    summaries: list[Summary],
    episode_url: str,
    feed_url: str,
    today: date,
    username: str = "HARRO LIFE",
    icon_emoji: str = ":sunrise:",
) -> None:
    grouped: dict[str, list[Summary]] = {}
    for s in sorted(summaries, key=lambda x: -x.importance):
        grouped.setdefault(s.category, []).append(s)

    header_text = f":flag-nl: HARRO LIFE — {today.strftime('%Y年%m月%d日')}"
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":headphones: <{episode_url}|今日のエピソードを聴く>"
                    f"  ｜  :studio_microphone: <{feed_url}|Podcast RSSで購読>"
                ),
            },
        },
        {"type": "divider"},
    ]

    for cat, items in grouped.items():
        emoji = CATEGORY_EMOJI.get(cat, ":small_blue_diamond:")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{emoji} {cat}*"},
            }
        )
        for s in items:
            stars = "★" * s.importance + "☆" * (5 - s.importance)
            body = (
                f"<{s.original_link}|*{_escape(s.title_ja)}*>  `{_escape(s.source)}`  {stars}\n"
                f"{_escape(s.summary_ja)}"
            )
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
        blocks.append({"type": "divider"})

    payload = {
        "username": username,
        "icon_emoji": icon_emoji,
        "text": f"HARRO LIFE — {today.strftime('%Y-%m-%d')}",
        "blocks": blocks,
    }

    resp = requests.post(webhook_url, json=payload, timeout=30)
    resp.raise_for_status()
    log.info("Posted to Slack (%d blocks)", len(blocks))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
