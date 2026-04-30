"""Smoke test for the weekly-column review email.

Builds a fake ColumnDraft from the archived W18 column and sends a single
review email to REVIEW_TO (defaulting to suga@harrojp.com) — no Anthropic
or Unsplash API calls.

Usage:
    REVIEW_TO=suga@harrojp.com python -m scripts.smoke_column_email
    REVIEW_TO=suga@harrojp.com REVIEW_CC=ayano@harrojp.com,karen.yoshida@harrojp.com \\
        python -m scripts.smoke_column_email
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

from src.column_generator import ColumnDraft, send_review_email  # noqa: E402


def main() -> int:
    fixture_path = ROOT.parent / "harro-life-site" / "src" / "content" / "_archive" / "columns" / "auto-2026-W18-food.md"
    if not fixture_path.exists():
        print(f"Fixture not found: {fixture_path}", file=sys.stderr)
        return 1

    raw = fixture_path.read_text(encoding="utf-8")
    # Strip frontmatter to extract body for the draft.
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

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        print("RESEND_API_KEY not set", file=sys.stderr)
        return 1

    review_to = os.environ.get("REVIEW_TO", "suga@harrojp.com").strip()
    review_cc = [
        e.strip()
        for e in os.environ.get("REVIEW_CC", "").split(",")
        if e.strip()
    ]
    from_email = os.environ.get("EMAIL_FROM", "onboarding@resend.dev").strip()

    print(f"Sending smoke test review email:")
    print(f"  from   = {from_email}")
    print(f"  to     = {review_to}")
    print(f"  cc     = {review_cc or '(none)'}")
    print(f"  body   = {len(body_md)} chars")

    ok = send_review_email(
        api_key=api_key,
        from_email=from_email,
        to_email=review_to,
        cc_emails=review_cc,
        draft=draft,
        category="food",
        week_key="2026-W18",
        md_filename="auto-2026-W18-food.md",
        md_content=raw,
        image_url="https://images.unsplash.com/photo-1592681815227-2d817c2ca751?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&w=1080",
        logo_url="https://harro-life-site.pages.dev/images/brand/harro-life-on-dark.png",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
