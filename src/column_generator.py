"""Weekly column auto-generator for HARRO LIFE.

Pre-publish review flow (since 2026-04-30):
  - Category rotation by ISO week mod 4: living / food / health / procedures
  - Claude picks a topic + writes a 1500-2000 char Japanese column
  - Optional Unsplash cover via existing images.py
  - 2-3 inline body images, one per section, fetched from Unsplash
  - **Saves to netherlands-news-bot/pending-columns/auto-{week}-{category}.md**
  - **Sends a review email (Resend) with HTML preview + .md attachment** to the
    editorial team. After human review, a follow-up Claude session moves the
    file from pending-columns/ to harro-life-site/src/content/columns/ to
    publish.

Run as a one-shot script (called from .github/workflows/weekly-column.yml):
    python -m src.column_generator
"""
from __future__ import annotations

import base64
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown as md_lib
import requests
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

from . import images

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("column-generator")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

DEFAULT_PENDING_DIR = ROOT / "pending-columns"

CATEGORY_ROTATION = ["living", "food", "health", "procedures"]

CATEGORY_LABEL_JA = {
    "living": "暮らし",
    "food": "食",
    "health": "健康",
    "procedures": "手続き",
}

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


# ──────────────────────────────────────────────────────────────────────
# Review email — sends column draft to the editorial team for sign-off.
# ──────────────────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_body_to_html(body_md: str) -> str:
    """Convert the column body Markdown (with embedded HTML tags) to HTML for email preview."""
    html = md_lib.markdown(body_md, extensions=["extra", "sane_lists"])
    return html


def build_review_html(
    draft: ColumnDraft,
    category: str,
    week_key: str,
    md_filename: str,
    image_url: str | None,
    logo_url: str,
    char_count: int,
) -> str:
    cat_label = CATEGORY_LABEL_JA.get(category, category)
    body_html = render_body_to_html(draft.body_md)

    cover_block = ""
    if image_url:
        cover_block = (
            '<div style="margin:24px 0;border-radius:8px;overflow:hidden;">'
            f'<img src="{_esc(image_url)}" alt="{_esc(draft.title)}" '
            'style="display:block;width:100%;height:auto;border:0;" />'
            '</div>'
        )

    if logo_url:
        logo_html = (
            f'<img src="{_esc(logo_url)}" alt="HARRO LIFE" '
            'style="height:32px;width:auto;display:block;border:0;" />'
        )
    else:
        logo_html = (
            '<div style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-0.02em;">'
            'HARRO LIFE</div>'
        )

    meta_block = (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#faf7f2;border-radius:8px;margin-bottom:24px;">'
        '<tr><td style="padding:16px 20px;">'
        '<div style="font-size:11px;color:#888;letter-spacing:0.15em;text-transform:uppercase;font-weight:700;margin-bottom:6px;">'
        'COLUMN DRAFT — REVIEW REQUESTED'
        '</div>'
        '<table width="100%" cellpadding="0" cellspacing="0">'
        f'<tr><td style="font-size:13px;color:#666;width:90px;padding:2px 0;">週</td>'
        f'<td style="font-size:13px;color:#1a1a1a;padding:2px 0;"><strong>{_esc(week_key)}</strong></td></tr>'
        f'<tr><td style="font-size:13px;color:#666;padding:2px 0;">カテゴリ</td>'
        f'<td style="font-size:13px;color:#1a1a1a;padding:2px 0;">{_esc(cat_label)}</td></tr>'
        f'<tr><td style="font-size:13px;color:#666;padding:2px 0;">本文文字数</td>'
        f'<td style="font-size:13px;color:#1a1a1a;padding:2px 0;">{char_count:,} 字</td></tr>'
        f'<tr><td style="font-size:13px;color:#666;padding:2px 0;">ファイル</td>'
        f'<td style="font-size:13px;color:#1a1a1a;padding:2px 0;font-family:monospace;">{_esc(md_filename)}</td></tr>'
        '</table>'
        '</td></tr></table>'
    )

    approval_box = (
        '<div style="margin-top:32px;padding:20px;background:#09202e;border-radius:8px;color:#ffffff;">'
        '<div style="font-size:13px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:10px;color:#EAE6C3;">'
        '次のアクション'
        '</div>'
        '<div style="font-size:14px;line-height:1.7;color:#ffffff;">'
        '<strong style="color:#EAE6C3;">公開する場合:</strong> Claude Code に '
        f'「<span style="font-family:monospace;background:#1A3346;padding:2px 6px;border-radius:3px;">{_esc(week_key)} のコラム公開して</span>」と伝えてください。'
        '<br/><br/>'
        '<strong style="color:#EAE6C3;">修正したい場合:</strong> 添付の '
        f'<span style="font-family:monospace;">{_esc(md_filename)}</span> を編集して suga@harrojp.com まで返信してください。'
        '<br/><br/>'
        '<strong style="color:#EAE6C3;">公開しない場合:</strong> Claude Code に '
        f'「<span style="font-family:monospace;background:#1A3346;padding:2px 6px;border-radius:3px;">{_esc(week_key)} のコラム見送り</span>」と伝えてください。'
        '</div></div>'
    )

    return f"""<!doctype html>
<html lang="ja">
<body style="margin:0;padding:0;background:#faf7f2;font-family:-apple-system,'Helvetica Neue','Hiragino Sans','Yu Gothic',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#faf7f2;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;margin:40px 20px;border-radius:8px;overflow:hidden;">
<tr><td style="background:#09202e;padding:22px 32px;">{logo_html}</td></tr>
<tr><td style="padding:32px 36px 40px;">
{meta_block}
<h1 style="font-size:26px;line-height:1.4;color:#09202e;margin:0 0 12px;font-weight:700;">{_esc(draft.title)}</h1>
<div style="font-size:15px;color:#555;line-height:1.7;margin-bottom:8px;">{_esc(draft.description)}</div>
{cover_block}
<div style="font-size:11px;color:#999;letter-spacing:0.15em;text-transform:uppercase;font-weight:700;margin:32px 0 8px;border-top:1px solid #eee;padding-top:24px;">
本文プレビュー
</div>
<div style="font-size:16px;line-height:1.85;color:#1a1a1a;">
{body_html}
</div>
{approval_box}
</td></tr>
<tr><td style="padding:18px 36px 24px;border-top:1px solid #eee;background:#faf7f2;text-align:center;font-size:11px;color:#bbb;letter-spacing:0.1em;">
HARRO LIFE Editorial · Auto-generated weekly column
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def send_review_email(
    api_key: str,
    from_email: str,
    to_email: str,
    cc_emails: list[str],
    draft: ColumnDraft,
    category: str,
    week_key: str,
    md_filename: str,
    md_content: str,
    image_url: str | None,
    logo_url: str,
) -> bool:
    """Send the column draft to editorial team via Resend.

    Returns True on success.
    """
    char_count = len(re.sub(r"\s+", "", draft.body_md))
    subject = f"[HARRO LIFE] {week_key} コラム下書き「{draft.title}」公開前確認"
    html = build_review_html(
        draft=draft,
        category=category,
        week_key=week_key,
        md_filename=md_filename,
        image_url=image_url,
        logo_url=logo_url,
        char_count=char_count,
    )

    payload: dict = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html,
        "attachments": [
            {
                "filename": md_filename,
                "content": base64.b64encode(md_content.encode("utf-8")).decode("ascii"),
            }
        ],
    }
    if cc_emails:
        payload["cc"] = cc_emails

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
        log.info(
            "Review email sent to %s (cc=%s) — id=%s",
            to_email,
            ",".join(cc_emails) if cc_emails else "(none)",
            resp.json().get("id", "?"),
        )
        return True
    except requests.HTTPError as e:
        log.error("Resend HTTP error: %s — body=%s", e, resp.text if resp is not None else "(no body)")
        return False
    except Exception as e:
        log.error("Failed to send review email: %s", e)
        return False


def main() -> int:
    tz = ZoneInfo("Europe/Amsterdam")
    today = datetime.now(tz)

    # ISO week-of-year → category rotation
    iso = today.isocalendar()
    category = pick_category((iso[0], iso[1]))
    log.info("Today: %s (ISO week %d) → category=%s", today.date(), iso[1], category)

    # Pending dir lives in this repo (netherlands-news-bot/pending-columns/).
    # The CI workflow can override via COLUMN_PENDING_DIR.
    pending_dir = Path(os.environ.get("COLUMN_PENDING_DIR") or DEFAULT_PENDING_DIR)
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    out_path = pending_dir / f"auto-{week_key}-{category}.md"

    # Idempotent: skip if a column for this week already exists in pending.
    if out_path.exists():
        log.info("Pending column for %s already exists at %s — skipping", week_key, out_path)
        return 0

    # Idempotent: skip if a column for this week is already published in the
    # site (we look at side-by-side harro-life-site/src/content/columns/ when
    # we can find it; in CI this dir is not checked out so the lookup is a no-op).
    published_dir = ROOT.parent / "harro-life-site" / "src" / "content" / "columns"
    if published_dir.exists():
        for existing in published_dir.glob(f"auto-{week_key}-*.md"):
            log.info("Column for %s already published at %s — skipping", week_key, existing)
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
    pending_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    log.info("Wrote pending column to %s (%d chars body)", out_path, len(draft.body_md))

    # Send review email to the editorial team.
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not resend_key:
        log.warning("RESEND_API_KEY not set — pending column saved but no review email sent")
        return 0

    review_to = os.environ.get("REVIEW_TO", "suga@harrojp.com").strip()
    review_cc = [
        e.strip()
        for e in os.environ.get("REVIEW_CC", "").split(",")
        if e.strip()
    ]
    from_email = os.environ.get("EMAIL_FROM", "onboarding@resend.dev").strip()
    logo_url = os.environ.get(
        "HARRO_LOGO_URL",
        "https://harro-life-site.pages.dev/images/brand/harro-life-on-dark.png",
    ).strip()

    ok = send_review_email(
        api_key=resend_key,
        from_email=from_email,
        to_email=review_to,
        cc_emails=review_cc,
        draft=draft,
        category=category,
        week_key=week_key,
        md_filename=out_path.name,
        md_content=rendered,
        image_url=image_url,
        logo_url=logo_url,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
