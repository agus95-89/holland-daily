from __future__ import annotations

import logging
from datetime import date

from anthropic import Anthropic

from .summarize import Summary

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたはオランダ在住日本人向け日刊ニュースポッドキャスト「Holland Daily」の台本ライターです。
与えられた記事の要約を基に、12〜15分で読み上げ可能な自然な日本語の台本を書いてください。

【構成】
- 冒頭: 女性ナレーターが挨拶、今日の日付、今日のハイライトを1文で紹介
- 本編: カテゴリごとに区切り、切り替え時は女性ナレーターが1文で橋渡し
  - カテゴリ内の各記事は男性ナレーターが読む
  - 各記事は「自然な導入 → 要約を聴き手に伝わる話し言葉で展開 → 原題やソースへの軽い言及」で構成
- 結び: 女性ナレーターが締めの挨拶と「明日もぜひお聴きください」の一言

【出力フォーマット】
<F>女性ナレーターの台詞</F>
<M>男性ナレーターの台詞</M>

【絶対ルール】
- <F> と <M> 以外のタグ、マークダウン、絵文字、記号装飾は一切使用しない
- 書き言葉ではなく話し言葉、耳で聴いて理解しやすい構文
- 英語・オランダ語の固有名詞は原則カタカナ表記、必要に応じて短く原語を補足
- 数字は「2026年」「3億ユーロ」「25パーセント」のように自然な日本語表記
- 目標文字数: 5000〜5500字 (読み上げ約13分)
- 事実を歪めず、推測や個人的見解を加えない
"""


def build_script(summaries: list[Summary], today: date, client: Anthropic, model: str) -> str:
    grouped: dict[str, list[Summary]] = {}
    for s in sorted(summaries, key=lambda x: -x.importance):
        grouped.setdefault(s.category, []).append(s)

    parts = [f"日付: {today.strftime('%Y年%m月%d日')}", ""]
    for cat, items in grouped.items():
        parts.append(f"## {cat}")
        for i, s in enumerate(items, 1):
            parts.append(f"- [{i}] 日本語タイトル: {s.title_ja}")
            parts.append(f"    原題: {s.original_title}")
            parts.append(f"    ソース: {s.source}")
            parts.append(f"    重要度: {s.importance}/5")
            parts.append(f"    要約: {s.summary_ja}")
        parts.append("")

    user_content = "\n".join(parts)

    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    script = "".join(text_parts).strip()
    log.info("Generated script: %d chars", len(script))
    return script
