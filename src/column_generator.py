"""Weekly column auto-generator for harro-life-site.

Approach A: fully automated weekly column.
  - Category rotation by ISO week mod 4: living / food / health / procedures
  - Claude picks a topic + writes a 1500-2000 char Japanese column
  - Optional Unsplash cover via existing images.py
  - 2-3 inline body images, one per section, fetched from Unsplash
  - Writes to harro-life-site/src/content/columns/auto-{date}-{category}.md

Run as a one-shot script (called from a GitHub Actions weekly cron):
    python -m src.column_generator
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
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

【Markdown ルール】
- 強調 (太字) は **絶対に <strong>...</strong> タグで書く**。`**強調**` の Markdown 記法は CJK で正しくレンダリングされないので使わない
- 例: `それが<strong>白アスパラガス</strong>です。` （正） / `それが**白アスパラガス**です。` （誤）
- 斜体は <em>...</em> タグ
- ## 中見出しは Markdown 通り

トーン:
- ですます調で親しみやすく、しかし情報量のあるテキスト
- 1,500〜2,000字 (本文のみ。frontmatter は別)
- ## 見出しを 3〜4 個で構造化
- 一段落 100-200 字、3〜5 段落

submit_column ツールで以下を返す:
- title: 30〜50 字のキャッチーな日本語タイトル
- description: 80〜120 字、ですます調の概要 (OGP / 一覧カード用)
- body_md: 本文 Markdown (1,500-2,000字、## 中見出し 3-4 個含む)
- image_query: カバー画像用 Unsplash 検索英語キーワード 2-4 語 (例: "amsterdam canal autumn", "dutch supermarket vegetables")
- body_images: 本文中の画像 2〜3 個。各画像はその直前の ## 見出しが何個目か (1 始まり) を after_heading で指定
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
            "image_query": {
                "type": "string",
                "description": "カバー画像用 Unsplash 検索キーワード (英語 2-4 語)",
            },
            "body_images": {
                "type": "array",
                "description": "本文中に挿入する画像 2〜3 個。各画像は after_heading で指定した ## 見出しのセクション末尾に挿入される。",
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Unsplash 検索キーワード (英語 2-4 語)",
                        },
                        "alt": {
                            "type": "string",
                            "description": "画像の alt / キャプション (日本語、20-40 字)",
                        },
                        "after_heading": {
                            "type": "integer",
                            "description": "何個目の ## 見出しのセクション末尾に挿入するか (1 始まり)",
                        },
                    },
                    "required": ["query", "alt", "after_heading"],
                },
            },
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
    body_images: list[dict] = field(default_factory=list)


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
                body_images=d.get("body_images", []) or [],
            )
    log.error("Claude did not return a submit_column tool call")
    return None


def _html_attr_esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _render_figure(url: str, alt: str) -> str:
    safe_alt = _html_attr_esc(alt)
    return (
        '<figure>\n'
        f'  <img src="{url}" alt="{safe_alt}" loading="lazy" />\n'
        f'  <figcaption>{safe_alt}</figcaption>\n'
        '</figure>'
    )


def embed_body_images(body_md: str, body_images: list[dict], unsplash_key: str) -> str:
    """Insert <figure> blocks at the end of each section keyed by `after_heading`."""
    if not body_images or not unsplash_key:
        return body_md

    by_heading: dict[int, dict] = {}
    for img in body_images:
        h = img.get("after_heading")
        q = (img.get("query") or "").strip()
        if isinstance(h, int) and h >= 1 and q and h not in by_heading:
            by_heading[h] = img
    if not by_heading:
        return body_md

    lines = body_md.split("\n")
    out: list[str] = []
    heading_count = 0

    for line in lines:
        is_heading = line.startswith("## ")
        if is_heading and heading_count in by_heading:
            img = by_heading.pop(heading_count)
            url = images.search_unsplash(img["query"], unsplash_key)
            log.info("Inline image for section %d (%r) → %s", heading_count, img["query"], url or "(none)")
            if url:
                # Trim trailing blanks before injecting figure so spacing is consistent.
                while out and out[-1] == "":
                    out.pop()
                out.append("")
                out.append(_render_figure(url, img.get("alt", "")))
                out.append("")
        if is_heading:
            heading_count += 1
        out.append(line)

    # Tail image (after the very last section).
    if heading_count in by_heading:
        img = by_heading.pop(heading_count)
        url = images.search_unsplash(img["query"], unsplash_key)
        log.info("Inline image for trailing section %d (%r) → %s", heading_count, img["query"], url or "(none)")
        if url:
            while out and out[-1] == "":
                out.pop()
            out.append("")
            out.append(_render_figure(url, img.get("alt", "")))

    return "\n".join(out)


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

    # Cover image (optional, falls back to no image if Unsplash key absent or query empty)
    image_url = None
    unsplash_key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if unsplash_key and draft.image_query:
        image_url = images.search_unsplash(draft.image_query, unsplash_key)
        log.info("Unsplash for cover %r → %s", draft.image_query, image_url or "(none)")

    # Inline body images (2-3 figures spread across sections)
    if unsplash_key and draft.body_images:
        draft.body_md = embed_body_images(draft.body_md, draft.body_images, unsplash_key)

    pub_date = today.strftime("%Y-%m-%d")
    rendered = render_markdown(draft, category, pub_date, image_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    log.info("Wrote %s (%d chars body)", out_path, len(draft.body_md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
