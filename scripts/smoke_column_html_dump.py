"""Render the column review email HTML to a local file (no network, no Resend).

Usage:
    python3 -m scripts.smoke_column_html_dump
    open /tmp/harro-column-review-preview.html  # to view in browser
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.column_generator import ColumnDraft, build_review_html  # noqa: E402


def main() -> int:
    fixture_path = ROOT.parent / "harro-life-site" / "src" / "content" / "_archive" / "columns" / "auto-2026-W18-food.md"
    raw = fixture_path.read_text(encoding="utf-8")
    parts = raw.split("---\n", 2)
    if len(parts) < 3:
        print("Cannot parse frontmatter", file=sys.stderr)
        return 1
    body_md = parts[2].strip()

    draft = ColumnDraft(
        title="オランダの春野菜を使いこなす：4月のスーパーで見つかる旬の食材ガイド",
        description="キングスデーで街が賑わう4月、オランダのスーパーには春ならではの野菜が並び始めます。白アスパラガスをはじめとした旬の食材の選び方・調理法を、在蘭日本人目線でわかりやすく紹介します。",
        body_md=body_md,
        image_query="white asparagus spring vegetables market",
    )

    html = build_review_html(
        draft=draft,
        category="food",
        week_key="2026-W18",
        md_filename="auto-2026-W18-food.md",
        image_url="https://images.unsplash.com/photo-1592681815227-2d817c2ca751?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&w=1080",
        logo_url="https://harro-life-site.pages.dev/images/brand/harro-life-on-dark.png",
        char_count=len(body_md),
    )

    out = Path("/tmp/harro-column-review-preview.html")
    out.write_text(html, encoding="utf-8")
    print(f"Wrote: {out}")
    print(f"Open with: open {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
