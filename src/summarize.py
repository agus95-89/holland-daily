from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from anthropic import Anthropic, APIError

log = logging.getLogger(__name__)

CATEGORIES = [
    "政治・政策",
    "経済・ビジネス",
    "社会・事件",
    "EU・国際関係",
    "テック・スタートアップ",
    "生活・文化",
]

SYSTEM_PROMPT = f"""あなたはオランダ在住の日本人向けニュースキュレーターです。
オランダ語または英語のニュース記事を読み、次の4項目を構造化して返します:

1. title_ja: 日本語タイトル (40字以内、簡潔に本質を伝える)
2. summary_ja: 日本語要約 (3-4文、事実ベース、主観や推測を避ける)
3. category: 次のいずれか一つ: {', '.join(CATEGORIES)}
4. importance: オランダ在住日本人にとっての関心度 (1-5、5が最重要)
   - 5: 生活に直接影響する重大事 (重要な政策変更、経済危機、災害等)
   - 4: 広く関心を持たれる重要ニュース
   - 3: 一般的に興味深いニュース
   - 2: 限定的な関心のニュース
   - 1: 軽い話題

【重要度評価のルール — オランダ国内フォーカス】
- 配信先は「オランダ在住日本人」。**オランダ国内のニュースを最優先**してください。
- 純粋な海外ニュース (米国の政局、戦争、災害、エンタメ、スポーツ国際大会など、オランダ国内の生活に直接の影響が無いもの) は importance を **1〜2 に下げてください**。
- 「EU・国際関係」カテゴリは、オランダの政策・経済・移民・税制に直接影響する場合のみ 3 以上を付けてよいです。それ以外 (ウクライナ戦況、米中関係、過去の歴史的記念など) は 1〜2 にしてください。
- 「政治・政策」「経済・ビジネス」「社会・事件」「生活・文化」のうち、オランダ国内のものは積極的に評価してください。

必ず submit_summary ツールで返してください。本文がほぼ無い場合は、可能な範囲で評価してください。"""

TOOL = {
    "name": "submit_summary",
    "description": "記事の要約・分類・重要度評価を構造化して返す",
    "input_schema": {
        "type": "object",
        "properties": {
            "title_ja": {"type": "string"},
            "summary_ja": {"type": "string"},
            "category": {"type": "string", "enum": CATEGORIES},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["title_ja", "summary_ja", "category", "importance"],
    },
}


@dataclass
class Summary:
    title_ja: str
    summary_ja: str
    category: str
    importance: int
    original_title: str
    original_link: str
    source: str


def summarize(
    article: dict,
    client: Anthropic,
    model: str,
    max_body_chars: int = 8000,
) -> Summary | None:
    body = (article.get("body") or article.get("summary") or "")[:max_body_chars]
    if not body.strip():
        log.warning("Empty body for %s, skipping", article.get("link"))
        return None

    user_content = (
        f"[原題] {article['title']}\n"
        f"[ソース] {article['source']}\n"
        f"[URL] {article['link']}\n\n"
        f"[本文]\n{body}"
    )

    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[TOOL],
                tool_choice={"type": "tool", "name": "submit_summary"},
                messages=[{"role": "user", "content": user_content}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "submit_summary":
                    data = block.input
                    return Summary(
                        title_ja=data["title_ja"],
                        summary_ja=data["summary_ja"],
                        category=data["category"],
                        importance=int(data["importance"]),
                        original_title=article["title"],
                        original_link=article["link"],
                        source=article["source"],
                    )
            log.warning("No tool_use in response for %s", article["link"])
            return None
        except APIError as e:
            wait = 2 ** attempt
            log.warning(
                "Claude API error on attempt %d for %s: %s (retrying in %ds)",
                attempt + 1, article["link"], e, wait,
            )
            time.sleep(wait)
        except Exception as e:
            log.warning("Summarize failed for %s: %s", article["link"], e)
            return None

    log.error("Exhausted retries for %s", article["link"])
    return None
