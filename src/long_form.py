"""Generate long-form Markdown articles for harro-life-site.

Given an Article dict (with body) and an existing short Summary, calls Claude
once more to produce a 800-1,500 character editorial article in Japanese,
along with the metadata needed for the harro-life-site frontmatter.

Failure returns None; callers should fall back to the short summary.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from anthropic import Anthropic, APIError

from .summarize import Summary

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """あなたはオランダ在住の日本人向けに、現地ニュースを編集する日本語ジャーナリストです。
渡される元記事と短要約をもとに、800〜1,500字の読み物記事に仕上げてください。

トーン:
- 雑誌的で編集された読み物。冷静で事実ベース。
- 主観や憶測を避ける。引用元を明示する必要はないが、本文中でソース名や関係者の発言は自然に言及してよい。
- 「Holland Daily」「ハロー・デイリー」など旧メディア名は使わない。
- 一段落 100-200字 程度を目安に、3-5段落で構成する。
- ## による中見出しを 2-3 個使う(冒頭は見出しなしで書き出してよい)。

構造:
- 冒頭で要点と全体像を提示
- 中盤で背景・経緯・関係者の見解を整理
- 末尾で在蘭日本人にとっての意味・影響を示す(該当しなければ、より広いオランダ社会への含意でよい)

出力フィールド:
- title_ja: 既存の日本語タイトルをそのまま使うか、より読み物らしく微調整(30-40字)
- subtitle: 補助的なサブタイトル(30-50字、無理に書かなくてもよい)
- description: 記事カード用の導入文(80-120字、ですます調)
- summary_points: 記事の要点(3-5個、各30-50字)
- body_md: 本文 Markdown(800-1,500字、## 中見出し2-3個)
- image_query: Unsplash 画像検索用の英語キーワード(2-4語、例: "amsterdam parliament" "rotterdam port" "dutch supermarket")

必ず submit_long_form ツールで返してください。"""

TOOL = {
    "name": "submit_long_form",
    "description": "短要約と元記事を読んで、雑誌的な編集記事に仕上げる",
    "input_schema": {
        "type": "object",
        "properties": {
            "title_ja": {"type": "string"},
            "subtitle": {"type": "string"},
            "description": {"type": "string"},
            "summary_points": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
            },
            "body_md": {"type": "string"},
            "image_query": {"type": "string"},
        },
        "required": [
            "title_ja",
            "description",
            "summary_points",
            "body_md",
            "image_query",
        ],
    },
}


@dataclass
class LongForm:
    title_ja: str
    subtitle: str | None
    description: str
    summary_points: list[str]
    body_md: str
    image_query: str


def expand(
    article: dict,
    summary: Summary,
    client: Anthropic,
    model: str,
    max_body_chars: int = 8000,
) -> LongForm | None:
    body = (article.get("body") or article.get("summary") or "")[:max_body_chars]
    if not body.strip():
        log.warning("Empty body for %s, skipping long-form", article.get("link"))
        return None

    user_content = (
        f"[原題] {article['title']}\n"
        f"[ソース] {article['source']}\n"
        f"[URL] {article['link']}\n"
        f"[既存の日本語タイトル] {summary.title_ja}\n"
        f"[既存の短要約] {summary.summary_ja}\n"
        f"[カテゴリ] {summary.category}\n\n"
        f"[本文]\n{body}"
    )

    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[TOOL],
                tool_choice={"type": "tool", "name": "submit_long_form"},
                messages=[{"role": "user", "content": user_content}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "submit_long_form":
                    data = block.input
                    subtitle = (data.get("subtitle") or "").strip()
                    return LongForm(
                        title_ja=data["title_ja"].strip(),
                        subtitle=subtitle or None,
                        description=data["description"].strip(),
                        summary_points=[p.strip() for p in data["summary_points"] if p.strip()],
                        body_md=data["body_md"].strip(),
                        image_query=data["image_query"].strip(),
                    )
            log.warning("No tool_use in long-form response for %s", article["link"])
            return None
        except APIError as e:
            wait = 2 ** attempt
            log.warning(
                "Claude API error on long-form attempt %d for %s: %s (retrying in %ds)",
                attempt + 1, article["link"], e, wait,
            )
            time.sleep(wait)
        except Exception as e:
            log.warning("Long-form failed for %s: %s", article["link"], e)
            return None

    log.error("Exhausted retries for long-form on %s", article["link"])
    return None
