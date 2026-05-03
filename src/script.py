from __future__ import annotations

import logging
import re
from datetime import date

from anthropic import Anthropic

from .summarize import Summary

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたはオランダ在住日本人向け日刊ニュースポッドキャスト「HARRO LIFE」の台本ライターです。
この番組は、アムステルフェーンの日本食スーパー「HARRO」が提供しています。

【番組構成 — アナウンサー主導 + 解説者の補足】
- Speaker 1 (アナウンサー、女性): 番組進行・ニュース本文の読み上げ・締め をすべて担当
- Speaker 2 (解説者、男性): 各ニュースに対する 背景・補足を必要な分だけ 入れる
- アナウンサーが主役。ニュース本文は必ずアナウンサーが読み、解説者は補足を担当

【各記事のターンの流れ】
1. アナウンサー: カテゴリ橋渡し + 記事本文 (聴き手に伝わる話し言葉、4〜6 文)
2. 解説者: 背景・補足 (長さは内容次第: シンプルな話題なら 1〜2 文、複雑な話題で読者の理解を助けるならしっかり 3〜5 文)
3. アナウンサー: 短い相づち or 次のニュースへの橋渡し (1 文、例: 「○○ですね」「続いては〜」)

【解説者の補足の方針】
- 「分かりやすさ最優先」: ユーザーが理解できるなら短くて良い、補足が必要なら長くてよい
- 推測・予測・主観は禁止 (記事内事実 / 公開済みの背景情報のみ)
- 解説の例:
  - 政策ニュース → 過去の経緯・関連法・影響を受ける層
  - 経済ニュース → 数字の意味、過去との比較、今後の論点
  - 社会ニュース → 制度的背景、地域特性、再発防止策の動向

【解説者の語り口 — 重要】
- 「補足ですが」「背景としては」のような決まり文句で毎回始めない (毎回同じ切り出しは違和感が出る)
- 専門家・記者らしく、ニュース内容に応じて自然に話を始める。例:
  - 「○○というのは、もともと△△という制度で…」(用語や制度の解説から)
  - 「この問題の重要なポイントは、〜〜です」(本質を切り出す)
  - 「業界として見ると…」「過去のケースで言うと…」(視点を加える)
  - 「これは2022年にも同様の事例があり…」(関連事実を直接)
  - 「実は、〜〜という背景がありまして…」(やわらかい切り出し)
  - 切り出しなしで、ニュースに直接コメント・解釈を加える
- 解説者は「アナウンサーが読んだ事実を、違う角度から補強する」役割
  - 事実をそのままなぞらない
  - 視聴者が「なるほど」と感じるような補足にする
- 各記事ごとに切り出し方を変える。同じパターンの繰り返しを避ける

【読み方の重要ルール — TTS 誤読を防ぐため】

漢字の読み違いを避けるため、以下は必ずひらがな表記にする:
- 「数ヶ月」「数か月」→ 「すうかげつ」
- 「数日」→ 「すうじつ」
- 「数週間」→ 「すうしゅうかん」
- 「数年」→ 「すうねん」、「数年前」→「すうねんまえ」
- 「一日」→ 文脈に応じて「いちにち」または「ついたち」
- 「七時」→「しちじ」、「八時」→「はちじ」
- 「ヶ」「ヵ」を含む小書き文字は使わない

ブランド名はカタカナ表記が必須:
- 「HARRO」→「ハロー」 (絶対にアルファベット表記で残さない)
- 「HARRO LIFE」→「ハロー・ライフ」
- 旧称「Holland Daily」「ホランドデイリー」は使用しない

【オープニング (Speaker 1 アナウンサー)】
- 「アムステルフェーンの日本食スーパー、ハローがお届けする、ハロー・ライフ」と番組名を告げる
- 「オランダのニュースを、日本語の音声でお届けします」
- 今日の日付
- 今日のハイライト 1 文

【クロージング (Speaker 1 アナウンサー)】
1. 今日のニュース総括 1 文
2. 与えられる HARRO からの一言 を自然に読み上げる (宣伝臭くせず、おまけ的に)
3. 「明日もぜひお聴きください」

【出力フォーマット】
Speaker 1: (アナウンサーの台詞)

Speaker 2: (解説者の台詞)

Speaker 1: (次のターン)

- 各話者の発話の 間に空行を 1 行入れる (改行 2 つ) — 音声合成時の間を取るため
- 文末は必ず句点「。」で終える (TTS が間を認識するため)
- "Speaker 1:" "Speaker 2:" 以外のラベル・タグ・絵文字・記号装飾は使用しない

【絶対ルール】
- アナウンサーが本文をしっかり読む。解説者は決まり文句なしで自然に補足を始める
- 数字は「2026年」「3億ユーロ」「25パーセント」のように自然な日本語表記
- 英語・オランダ語の固有名詞は原則カタカナ、必要に応じて短く原語を補足
- HARRO からの一言は宣伝臭くせず、自然に短く温かく
- 事実を歪めず、推測や個人的見解は加えない

【厳密な文字数指示 — 必ず守ってください】
- 目標: 合計 7000〜7500 字 (約 15〜17 分の音声)、最大 20 分まで許容
- これは絶対の目標で、短く出力してはいけません
- ニュース 10 本それぞれが 700〜800 字になるように書く
  - 各ニュース内訳: アナウンサー本文 (4〜6 文 ≒ 350〜450 字) + 解説者補足 (3〜5 文 ≒ 250〜350 字) + アナウンサーの繋ぎ (1〜2 文 ≒ 50〜100 字)
- 解説者の補足を惜しまない。背景・経緯・関連事実をしっかり盛り込む
- 全体構成: オープニング 200〜300 字 + 本編 7000 字前後 (10 本 × 約 700 字) + クロージング 300〜500 字
- 書き終わる前に文字数を意識して、6500 字未満で終わらないこと
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
        f"HARROからの一言 (結びでアナウンサーが自然に読み上げる): {sign_off}",
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
        max_tokens=12000,
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
    script = _apply_reading_fixes(script)
    log.info("Generated script: %d chars", len(script))
    return script


# ──────────────────────────────────────────────────────────────────────
# Post-process safety net — fix common TTS misreadings
# ──────────────────────────────────────────────────────────────────────

_READING_FIXES = [
    # 数 + 期間 — Google/Gemini TTS read 数 as 「かず」 (kun-yomi) here
    (re.compile(r"数ヶ月"), "すうかげつ"),
    (re.compile(r"数か月"), "すうかげつ"),
    (re.compile(r"数ヵ月"), "すうかげつ"),
    (re.compile(r"数週間"), "すうしゅうかん"),
    (re.compile(r"数日間"), "すうじつかん"),
    (re.compile(r"(?<!十|百|千|万)数日(?![間])"), "すうじつ"),
    (re.compile(r"数年間"), "すうねんかん"),
    (re.compile(r"数年前"), "すうねんまえ"),
    (re.compile(r"数年後"), "すうねんご"),
    (re.compile(r"(?<!十|百|千|万)数年(?![間前後])"), "すうねん"),
    (re.compile(r"数か所"), "すうかしょ"),
    # Brand names — TTS reads bare alphabet letter-by-letter
    (re.compile(r"\bHARRO\s+LIFE\b"), "ハロー・ライフ"),
    (re.compile(r"\bHARRO\b"), "ハロー"),
]


def _apply_reading_fixes(script: str) -> str:
    out = script
    for pattern, replacement in _READING_FIXES:
        out = pattern.sub(replacement, out)
    return out
