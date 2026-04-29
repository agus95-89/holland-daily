"""Weekly column auto-generator for harro-life-site.

Approach A: fully automated weekly column.
  - Category rotation by ISO week mod 4: living / food / health / procedures
  - Claude picks a topic + writes a 1500-2000 char Japanese column
  - Optional Unsplash cover via existing images.py
  - Writes to harro-life-site/src/content/columns/auto-{date}-{category}.md

Run as a one-shot script (called from a GitHub Actions weekly cron):
    python -m src.column_generator
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

from . import images

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("column-generator")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

DEFAULT_OUTPUT_DIR = ROOT.parent / "harro-life-site" / "src" / "content" / "columns"

CATEGORY_ROTATION = ["living", "food", "health", "procedures"]

CATEGORY_HINTS = {
    "living": "暮らし全般 / 住まい / 季節行事 / 自治体サービス / 地域コミュニティ / 生活ライフハック",
    "food":   "食 / グルメ / スーパーで買える食材 / レシピ / 季節の食べ物 / レストラン",
    "health": "健康 / 医療制度 / 保険 / 季節の健康管理 / 運動 / メンタルヘルス",
    "procedures": "行政手続き / ビザ / 税金 / 銀行 / 保険 / 教育 / 引越し / 住所変更",
}

SEASON_HINTS = {
    1:  "厳冬期 (1月) — 寒さ・暖房・乾燥対策、新年の手続きシーズン",
    2:  "厳冬期 (2月) — まだ寒く、暗い。屋内活動・栄養",
    3:  "早春 (3月) — 日が長くなり始め、サマータイム移行直前",
    4:  "春 (4月) — キングスデー、桜、花粉、新年度",
    5:  "晩春 (5月) — 連休、新緑、屋外シーズンの始まり",
    6:  "初夏 (6月) — 学校・会社の年度末、夏休み準備",
    7:  "夏休み (7月) — 旅行、休暇取得",
    8:  "夏休み (8月) — 観光客の多い時期、暑さ対策",
    9:  "秋の入り (9月) — 新学期、新生活のスタート",
    10: "秋深まる (10月) — 衣替え、サマータイム終了、肌寒さ",
    11: "晩秋 (11月) — シンタクラース準備、暗くなる、健康管理",
    12: "年末 (12月) — クリスマス・年越し、帰国・年末準備",
}

SYSTEM_PROMPT = """あなたはオランダ在住の日本人向けニュースメディア「HARRO LIFE」のコラム執筆者です。

コラムの位置付け:
- 速報ではない、実用的・時間が経っても価値が落ちにくい evergreen 記事
- 在蘭日本人が「読んでよかった」と感じる、具体的で行動につながる情報
- ニュースとは違い、人間味のある語り口で書く

【絶対ルール】
- 完全に新規の創作。事実を捏造しない。一般的に事実として知られている情報のみ使う。
- 個人名・固有の店舗名・電話番号・行政の URL などの「具体的すぎる事実」は出さない（誤った情報を書かないため）
- 数字や金額は「数十ユーロ程度」「2〜3 週間」のような幅で書く（変動するため）
- 「私が試した」「現地に行った」のような体験を装う表現は使わない（AI なので嘘になる）
- 「公式サイトで最新情報を確認してください」のような注意喚起を末尾に入れる

トーン:
- ですます調で親しみやすく、しかし情報量のあるテキスト
- 1,500〜2,000字 (本文のみ。frontmatter は別)
- ## 見出しを 3〜4 個で構造化
- 一段落 100-200 字、3〜5 段落

submit_column ツールで以下を返す:
- title: 30〜50 字のキャッチーな日本語タイトル
- description: 80〜120 字、ですます調の概要 (OGP / 一覧カード用)
- body_md: 本文 Markdown (1,500-2,000字、## 中見出し 3-4 個含む)
- image_query: Unsplash 検索用 英語キーワード 2-4 語 (例: "amsterdam canal autumn", "dutch supermarket vegetables")
"""

TOOL = {
    "name": "submit_column",
    "description": "週次コラムを生成して返す",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "body_md": {"type": "string"},
            "image_query": {"type": "string"},
        },
        "required": ["title", "description", "body_md", "image_query"],
    },
}


@dataclass
class ColumnDraft:
    title: str
    description: str
    body_md: str
    image_query: str


def pick_category(today_iso_year_week: tuple[int, int]) -> str:
    """ISO week number (week of year) → category."""
    _year, week = today_iso_year_week
    return CATEGORY_ROTATION[(week - 1) % 4]


def reading_time_minutes(body_md: str) -> int:
    chars = len(re.sub(r"\s+", "", body_md))
    return max(1, round(chars / 600))


def generate_column(category: str, today: datetime) -> ColumnDraft | None:
    season = SEASON_HINTS.get(today.month, "")
    user_prompt = (
        f"今週のコラムカテゴリ: **{category}**\n"
        f"カテゴリ範囲のヒント: {CATEGORY_HINTS[category]}\n"
        f"今の季節感: {season}\n"
        f"今日の日付: {today.strftime('%Y-%m-%d')}\n\n"
        "上記カテゴリ範囲の中から、季節感に合った具体的なトピックを 1 つだけ選び、コラム本文を書いてください。"
    )

    client = Anthropic()
    model = "claude-sonnet-4-6"
    log.info("Calling Claude (%s) for category=%s ...", model, category)
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "submit_column"},
        messages=[{"role": "user", "content": user_prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_column":
            d = block.input
            return ColumnDraft(
                title=d["title"],
                description=d["description"],
                body_md=d["body_md"],
                image_query=d.get("image_query", ""),
            )
    log.error("Claude did not return a submit_column tool call")
    return None


def render_markdown(draft: ColumnDraft, category: str, pub_date: str, image_url: str | None) -> str:
    fm: dict = {
        "title": draft.title,
        "description": draft.description,
        "pubDate": pub_date,
        "category": category,
    }
    if image_url:
        fm["image"] = image_url
        fm["imageAlt"] = draft.title
    fm["readingTime"] = reading_time_minutes(draft.body_md)

    yaml_text = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False, width=10000)
    return f"---\n{yaml_text}---\n\n{draft.body_md.strip()}\n"


def main() -> int:
    tz = ZoneInfo("Europe/Amsterdam")
    today = datetime.now(tz)

    # ISO week-of-year → category rotation
    iso = today.isocalendar()
    category = pick_category((iso[0], iso[1]))
    log.info("Today: %s (ISO week %d) → category=%s", today.date(), iso[1], category)

    # Idempotent: skip if a column for this week already exists
    output_dir = Path(os.environ.get("COLUMN_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR)
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    out_path = output_dir / f"auto-{week_key}-{category}.md"
    if out_path.exists():
        log.info("Column for %s already exists at %s — skipping", week_key, out_path)
        return 0

    draft = generate_column(category, today)
    if draft is None:
        log.error("Column generation failed")
        return 1

    # Image (optional, falls back to no image if Unsplash key absent or query empty)
    image_url = None
    unsplash_key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if unsplash_key and draft.image_query:
        image_url = images.search_unsplash(draft.image_query, unsplash_key)
        log.info("Unsplash for %r → %s", draft.image_query, image_url or "(none)")

    pub_date = today.strftime("%Y-%m-%d")
    rendered = render_markdown(draft, category, pub_date, image_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    log.info("Wrote %s (%d chars body)", out_path, len(draft.body_md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
