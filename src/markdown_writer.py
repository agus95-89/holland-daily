"""Write a single news article as a frontmatter Markdown file for harro-life-site.

The output schema matches `harro-life-site/src/content.config.ts` `news` collection.
Pipeline categories (6) are mapped to site categories (4) via CATEGORY_MAP.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .long_form import LongForm
from .summarize import Summary

log = logging.getLogger(__name__)

# Pipeline -> harro-life-site category. Keep in sync with HANDOVER table.
CATEGORY_MAP: dict[str, str] = {
    "政治・政策": "politics",
    "EU・国際関係": "politics",
    "経済・ビジネス": "economy",
    "社会・事件": "society",
    "生活・文化": "society",
    "テック・スタートアップ": "tech",
}

SITE_CATEGORIES = {"politics", "economy", "society", "tech"}


def map_category(pipeline_category: str) -> str:
    mapped = CATEGORY_MAP.get(pipeline_category)
    if mapped is None:
        log.warning("Unknown pipeline category '%s', falling back to 'society'", pipeline_category)
        return "society"
    return mapped


def reading_time_minutes(body_md: str) -> int:
    """Approximate Japanese reading time. ~600 chars/min, minimum 1 minute."""
    chars = len(re.sub(r"\s+", "", body_md))
    return max(1, round(chars / 600))


def make_slug(pub_date: date, index: int) -> str:
    return f"{pub_date.isoformat()}-{index:02d}"


def build_frontmatter(
    long_form: LongForm,
    summary: Summary,
    pub_date: date,
    image_url: str | None,
    image_alt: str | None,
    featured: bool,
    breaking: bool,
) -> dict:
    fm: dict = {
        "title": long_form.title_ja,
    }
    if long_form.subtitle:
        fm["subtitle"] = long_form.subtitle
    fm["description"] = long_form.description
    fm["pubDate"] = pub_date.isoformat()
    fm["category"] = map_category(summary.category)
    if image_url:
        fm["image"] = image_url
        fm["imageAlt"] = image_alt or long_form.title_ja
    fm["summary"] = long_form.summary_points[:5]
    fm["readingTime"] = reading_time_minutes(long_form.body_md)
    if summary.original_link:
        fm["sourceUrl"] = summary.original_link
    if summary.source:
        fm["sourceName"] = summary.source
    fm["featured"] = bool(featured)
    fm["breaking"] = bool(breaking)
    return fm


def render_markdown(
    long_form: LongForm,
    summary: Summary,
    pub_date: date,
    image_url: str | None = None,
    image_alt: str | None = None,
    featured: bool = False,
    breaking: bool = False,
) -> str:
    fm = build_frontmatter(
        long_form=long_form,
        summary=summary,
        pub_date=pub_date,
        image_url=image_url,
        image_alt=image_alt,
        featured=featured,
        breaking=breaking,
    )
    yaml_text = yaml.dump(
        fm,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10000,
    )
    return f"---\n{yaml_text}---\n\n{long_form.body_md.strip()}\n"


def write_news_markdown(
    long_form: LongForm,
    summary: Summary,
    pub_date: date,
    index: int,
    output_dir: Path,
    image_url: str | None = None,
    image_alt: str | None = None,
    featured: bool = False,
    breaking: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = make_slug(pub_date, index)
    path = output_dir / f"{slug}.md"
    content = render_markdown(
        long_form=long_form,
        summary=summary,
        pub_date=pub_date,
        image_url=image_url,
        image_alt=image_alt,
        featured=featured,
        breaking=breaking,
    )
    path.write_text(content, encoding="utf-8")
    log.info("Wrote %s (%d chars)", path, len(long_form.body_md))
    return path
