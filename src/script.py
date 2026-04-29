from __future__ import annotations

import logging
import re
from datetime import date

from anthropic import Anthropic

from .summarize import Summary

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたはオランダ在住日本人向け日刊ニュースポッドキャスト「HARRO LIFE」の台本ライターです。
この番組は、アムステルフェーンの日本食スーパー「HARRO」が提供しています。
与えられた記事の要約を基に、12〜15分で読み上げ可能な自然な日本語の台本を書いてください。

【読み方の重要ルール】
- 「HARRO」は必ずカタカナで「ハロー」と書き、TTS が「ハロー」と発音するようにする
- 「HARRO」をアルファベット表記で残してはいけない (アルファベットで残すと TTS が「えいち・えー・あーる・あーる・おー」と読み上げてしまう)
- 番組名「HARRO LIFE」は台本中ではすべて「ハロー・ライフ」とカタカナで書く (アルファベットの LIFE を残すと TTS が「エル・アイ・エフ・イー」と読み上げてしまう)
- 旧称「Holland Daily」「ホランドデイリー」「ハロー・ホランド・デイリー」は使用しない

【構成】
- 冒頭: 女性ナレーターがオープニングを行う。**必ず以下の要素を含める**:
  - 「アムステルフェーンの日本食スーパー、ハローがお届けする、ハロー・ライフ」と番組名を告げる
  - 「オランダのニュースを、日本語の音声でお届けします」のような番組説明を1文で添える
  - 今日の日付
  - 今日のハイライトを1文で紹介
- 本編: カテゴリごとに区切り、切り替え時は女性ナレーターが1文で橋渡し
  - カテゴリ内の各記事は男性ナレーターが読む
  - 各記事は「自然な導入 → 要約を聴き手に伝わる話し言葉で展開 → 原題やソースへの軽い言及」で構成
- 結び: 女性ナレーターが以下の順で締める:
  1. その日のニュースを総括する一言
  2. 与えられる **HARROからの一言** をそのままナチュラルに読み上げる (宣伝色を出さず、おまけ的に挟む)
  3. 「明日もぜひお聴きください」の挨拶

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
- HARROからの一言は宣伝臭くなりすぎないよう、自然に短く、温かい口調で読み上げる
"""

# 曜日ローテーションの「HARROからの一言」(押し付けがましくない、1〜2文)
HARRO_SIGN_OFFS = [
    # 月曜
    "ハローはアムステルフェーンの店舗、そしてオランダ全国一律3.95ユーロでお届けするオンラインショップで、皆さまの食卓をお手伝いしています。",
    # 火曜
    "ハローでは、お醤油や味噌などの定番調味料から、お惣菜、職人の手による和食器まで、日本の食と暮らしを幅広くお取り扱いしています。",
    # 水曜
    "ハローのオンラインショップは、オランダ全国一律3.95ユーロ、70ユーロ以上のお買い物で送料無料です。",
    # 木曜
    "アムステルフェーンの中心、Stadshart から徒歩1分。ハローは毎日朝10時から夜8時までオープンしています。",
    # 金曜
    "週末は、ハローのお酒コーナーから、日本酒や梅酒、焼酎などをぜひのぞいてみてください。",
    # 土曜
    "週末のお買い物には、ハローの店頭で揃うおにぎりやお惣菜もおすすめです。",
    # 日曜
    "ハローは日曜日もオープンしています。お休みの日のご来店も、ぜひお気軽に。",
]


def build_script(summaries: list[Summary], today: date, client: Anthropic, model: str) -> str:
    grouped: dict[str, list[Summary]] = {}
    for s in sorted(summaries, key=lambda x: -x.importance):
        grouped.setdefault(s.category, []).append(s)

    # weekday() returns 0 = Monday ... 6 = Sunday — matches HARRO_SIGN_OFFS order
    sign_off = HARRO_SIGN_OFFS[today.weekday()]

    parts = [
        f"日付: {today.strftime('%Y年%m月%d日 (%a)')}",
        "",
        f"HARROからの一言 (結びで女性ナレーターが自然に読み上げる): {sign_off}",
        "",
    ]
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
    # Safety net: TTS reads bare alphabet letter-by-letter. Force katakana for
    # the show name and brand even if the model leaves them as letters.
    script = re.sub(r"\bHARRO\s+LIFE\b", "ハロー・ライフ", script)
    script = re.sub(r"\bHARRO\b", "ハロー", script)
    log.info("Generated script: %d chars", len(script))
    return script
