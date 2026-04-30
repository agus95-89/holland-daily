"""Migrate old HARRO LIFE (Studio.design) -> harro-life-site (Astro).

One-shot migration tool. Each subcommand is idempotent and writes intermediate
artifacts to scripts/migrate_data/, so we can re-run any stage without
re-scraping the whole site.

Usage:
    python -m scripts.migrate urls       # Step 1: sitemap -> urls.json
    python -m scripts.migrate scrape     # Step 2: HTML -> scraped/*.json
    python -m scripts.migrate scrape --limit 5    # validate on a sample
    python -m scripts.migrate images     # Step 3: download covers
    python -m scripts.migrate classify   # Step 4: Claude category mapping
    python -m scripts.migrate markdown   # Step 5: write .md to harro-life-site
    python -m scripts.migrate redirects  # Step 6: _redirects file
    python -m scripts.migrate all        # All steps in order
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

# --- Paths ---------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "scripts" / "migrate_data"
DATA.mkdir(parents=True, exist_ok=True)
SITE_ROOT = ROOT.parent / "harro-life-site"
SITE_NEWS = SITE_ROOT / "src" / "content" / "news"
SITE_COLUMNS = SITE_ROOT / "src" / "content" / "columns"
SITE_LEGACY_IMG = SITE_ROOT / "public" / "images" / "legacy"
SITE_REDIRECTS = SITE_ROOT / "public" / "_redirects"

URLS_JSON = DATA / "urls.json"
SCRAPED_DIR = DATA / "scraped"
CLASSIFIED_JSON = DATA / "classified.json"
IMAGE_MAP_JSON = DATA / "image_map.json"

UA = "Mozilla/5.0 (compatible; HARRO-LIFE-Migration/1.0)"
SITEMAP_INDEX = "https://harrojp.com/sitemap.xml"

# --- Helpers -------------------------------------------------------------


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_studio_slug_to_iso(slug: str) -> Optional[str]:
    """`290426-3` -> `2026-04-29`. Studio slugs use reverse YYMMDD with optional -N suffix."""
    m = re.match(r"^(\d{2})(\d{2})(\d{2})(?:-\d+)?$", slug)
    if not m:
        return None
    dd, mm, yy = m.group(1), m.group(2), m.group(3)
    return f"20{yy}-{mm}-{dd}"


def parse_studio_seq(slug: str) -> int:
    m = re.search(r"-(\d+)$", slug)
    return int(m.group(1)) if m else 1


# --- Step 1: URL list ----------------------------------------------------


def step_urls() -> dict:
    """Fetch sitemap index + sub-sitemaps, build full URL list."""
    log.info("[1/6] Fetching sitemap index: %s", SITEMAP_INDEX)
    idx_xml = http_get(SITEMAP_INDEX).decode()
    sub_sitemaps = re.findall(r"<loc>([^<]+)</loc>", idx_xml)
    log.info("  found %d sub-sitemaps", len(sub_sitemaps))

    url_buckets: dict[str, list[str]] = {"articles": [], "columns": [], "static": [], "tags": [], "other": []}
    for sm_url in sub_sitemaps:
        log.info("  fetching %s", sm_url)
        sm_xml = http_get(sm_url).decode()
        urls = re.findall(r"<loc>([^<]+)</loc>", sm_xml)
        for u in urls:
            path = urllib.parse.urlparse(u).path
            if path.startswith("/articles/"):
                url_buckets["articles"].append(u)
            elif path.startswith("/life/column/"):
                url_buckets["columns"].append(u)
            elif path.startswith("/tags/"):
                url_buckets["tags"].append(u)
            elif path == "/" or path.count("/") <= 2:
                url_buckets["static"].append(u)
            else:
                url_buckets["other"].append(u)

    summary = {k: len(v) for k, v in url_buckets.items()}
    log.info("  Distribution: %s", summary)

    URLS_JSON.write_text(json.dumps(url_buckets, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("  wrote %s", URLS_JSON)
    return url_buckets


# --- Step 2: Scrape ------------------------------------------------------

# Nuxt 3 / devalue wrappers that just point to a value at a different index.
PASSTHROUGH_MARKERS = {
    "ShallowReactive", "Reactive", "Ref", "ShallowRef", "Readonly",
    "EmptyShallowRef", "EmptyRef", "Date",  # Date wraps an ISO string
}


def resolve_nuxt_payload(arr: list, idx: int, depth: int = 0):
    """Recursively resolve Nuxt 3 payload references."""
    if depth > 200:
        return None
    if not isinstance(idx, int):
        return idx
    if not (0 <= idx < len(arr)):
        return None
    val = arr[idx]
    if (isinstance(val, list) and len(val) == 2
            and isinstance(val[0], str) and val[0] in PASSTHROUGH_MARKERS):
        return resolve_nuxt_payload(arr, val[1], depth + 1)
    if isinstance(val, dict):
        return {k: resolve_nuxt_payload(arr, v, depth + 1) for k, v in val.items()}
    if isinstance(val, list):
        return [resolve_nuxt_payload(arr, v, depth + 1) for v in val]
    return val


def html_body_to_markdown(html: str) -> str:
    """Convert Studio body HTML to clean Markdown.

    Studio body uses <h3>, <p>, <a>, <br>, <img>, <strong>, <em>, <u>, <ul>, <li>.
    We do conservative substitutions, then clean up whitespace.
    """
    s = html
    # Remove Studio-specific data attributes
    s = re.sub(r' data-(uid|time|has-link|index)="[^"]*"', "", s)
    # Strip id attributes that Studio injects on every heading
    s = re.sub(r' id="index_[A-Za-z0-9_-]+"', "", s)
    # Headings
    s = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n\n## \1\n\n", s, flags=re.S)
    s = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n\n### \1\n\n", s, flags=re.S)
    s = re.sub(r"<h4[^>]*>(.*?)</h4>", r"\n\n#### \1\n\n", s, flags=re.S)
    # Paragraphs
    s = re.sub(r"<p[^>]*>(.*?)</p>", r"\n\n\1\n\n", s, flags=re.S)
    # Line breaks
    s = re.sub(r"<br\s*/?>", "  \n", s)
    # Bold / italic / underline
    s = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", s, flags=re.S)
    s = re.sub(r"<b[^>]*>(.*?)</b>", r"**\1**", s, flags=re.S)
    s = re.sub(r"<em[^>]*>(.*?)</em>", r"*\1*", s, flags=re.S)
    s = re.sub(r"<i[^>]*>(.*?)</i>", r"*\1*", s, flags=re.S)
    s = re.sub(r"<u[^>]*>(.*?)</u>", r"\1", s, flags=re.S)  # underline -> plain
    # Lists
    s = re.sub(r"<ul[^>]*>", "\n", s)
    s = re.sub(r"</ul>", "\n", s)
    s = re.sub(r"<ol[^>]*>", "\n", s)
    s = re.sub(r"</ol>", "\n", s)
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", s, flags=re.S)
    # Links
    s = re.sub(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", s, flags=re.S)
    # Images: preserve src (we will rewrite to /images/legacy/... later)
    s = re.sub(r'<img[^>]*src="([^"]+)"[^>]*alt="([^"]*)"[^>]*/?>', r"![\2](\1)", s)
    s = re.sub(r'<img[^>]*src="([^"]+)"[^>]*/?>', r"![](\1)", s)
    # Strip remaining tags
    s = re.sub(r"<[^>]+>", "", s)
    # Decode HTML entities (basic)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(ent, ch)
    # Collapse 3+ newlines to 2
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


@dataclass
class ScrapedArticle:
    url: str
    slug: str  # original Studio slug
    kind: str  # "news" or "column"
    title: str
    description: str = ""
    summary_bullets: str = ""  # AI summary text from Studio
    body_html: str = ""
    body_md: str = ""
    cover: str = ""  # Studio CDN URL
    pub_date: str = ""  # ISO YYYY-MM-DD
    studio_id: str = ""
    extra_image_urls: list = field(default_factory=list)


def _coerce_str(val) -> str:
    """Coerce arbitrary Nuxt-resolved value to a string when sensible."""
    if isinstance(val, str):
        return val
    if isinstance(val, list) and val and isinstance(val[0], str):
        return val[0]
    return ""


def parse_one_article(url: str) -> Optional[ScrapedArticle]:
    """Fetch and parse a single Studio article URL."""
    try:
        html = http_get(url).decode()
    except Exception as e:
        log.warning("  fetch failed %s: %s", url, e)
        return None

    # Find the Nuxt payload script
    m = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        log.warning("  no payload script in %s", url)
        return None
    try:
        arr = json.loads(m.group(1))
    except Exception as e:
        log.warning("  payload JSON parse failed %s: %s", url, e)
        return None

    try:
        root = resolve_nuxt_payload(arr, 0)
        data = (root or {}).get("data", {}) if isinstance(root, dict) else {}
        if not data:
            log.warning("  no data field in payload %s", url)
            return None
        article_key = next(iter(data.keys()))
        article = data[article_key]
        if not isinstance(article, dict):
            log.warning("  article not a dict in %s", url)
            return None

        path = urllib.parse.urlparse(url).path
        slug = _coerce_str(article.get("slug")) or path.rstrip("/").split("/")[-1]
        kind = "column" if "/life/column/" in path else "news"

        # Pub date: prefer _meta.publishedAt (ISO 8601 string), fall back to slug-derived ISO date
        meta = article.get("_meta") if isinstance(article.get("_meta"), dict) else {}
        pub_raw = _coerce_str(meta.get("publishedAt")) or _coerce_str(meta.get("createdAt"))
        pub_date = pub_raw.split("T")[0] if pub_raw else (parse_studio_slug_to_iso(slug) or "")

        # Body: use canonical 'body' field for HTML, fall back to longest HTML-bearing string field.
        body_html = _coerce_str(article.get("body"))
        if not body_html:
            candidate_bodies = [(k, v) for k, v in article.items()
                                if isinstance(v, str) and len(v) > 200 and "<" in v]
            if candidate_bodies:
                body_html = max(candidate_bodies, key=lambda x: len(x[1]))[1]

        # Description: largest medium-length plain text field (often the lead paragraph).
        # Exclude URL-like strings, the cover URL, and the body itself.
        description = ""
        short_fields = [
            (k, v) for k, v in article.items()
            if isinstance(v, str)
            and 50 <= len(v) <= 600
            and k not in ("title", "slug", "id", "cover", "body")
            and not v.startswith(("http://", "https://"))
        ]
        if short_fields:
            description = max(short_fields, key=lambda x: len(x[1]))[1]
        # Strip <br> and any leftover HTML for description (it's used as plain text in frontmatter).
        description = re.sub(r"<br\s*/?>", " ", description)
        description = re.sub(r"<[^>]+>", "", description)
        description = re.sub(r"\s+", " ", description).strip()

        # Bullet-point AI summary if present (uses '・' bullets and <br> separators).
        summary_bullets = ""
        for k, v in article.items():
            if isinstance(v, str) and "・" in v and "<br>" in v and len(v) < 500:
                summary_bullets = v
                break

        extra_imgs = re.findall(r'<img[^>]*src="(https://storage\.googleapis\.com/[^"]+)"', body_html)

        return ScrapedArticle(
            url=url,
            slug=slug,
            kind=kind,
            title=_coerce_str(article.get("title")).strip(),
            description=description,
            summary_bullets=summary_bullets,
            body_html=body_html,
            body_md=html_body_to_markdown(body_html) if body_html else "",
            cover=_coerce_str(article.get("cover")),
            pub_date=pub_date,
            studio_id=_coerce_str(article.get("id")),
            extra_image_urls=extra_imgs,
        )
    except Exception as e:
        log.warning("  parse failed %s: %s", url, e)
        return None


def step_scrape(limit: Optional[int] = None, workers: int = 4) -> None:
    """Scrape all article URLs (or first `limit`) into scraped/*.json."""
    if not URLS_JSON.exists():
        log.error("  %s missing — run `urls` step first", URLS_JSON)
        sys.exit(1)
    buckets = json.loads(URLS_JSON.read_text())
    targets = buckets["articles"] + buckets["columns"]
    if limit:
        targets = targets[:limit]
    SCRAPED_DIR.mkdir(parents=True, exist_ok=True)

    # Skip URLs already scraped
    todo = [u for u in targets
            if not (SCRAPED_DIR / f"{urllib.parse.urlparse(u).path.split('/')[-1]}.json").exists()]
    log.info("  %d targets, %d already done, %d to scrape", len(targets), len(targets) - len(todo), len(todo))

    success, fail = 0, 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(parse_one_article, u): u for u in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            url = futs[fut]
            art = fut.result()
            if art is None:
                fail += 1
                continue
            out = SCRAPED_DIR / f"{art.slug}.json"
            out.write_text(json.dumps(asdict(art), ensure_ascii=False, indent=2), encoding="utf-8")
            success += 1
            if i % 50 == 0 or i == len(todo):
                elapsed = time.time() - start
                rate = i / elapsed if elapsed else 0
                eta = (len(todo) - i) / rate if rate else 0
                log.info("  [%d/%d] success=%d fail=%d rate=%.1f/s ETA=%.0fs",
                         i, len(todo), success, fail, rate, eta)
    log.info("  done: success=%d, fail=%d, total=%d", success, fail, success + fail)


# --- Step 3: Image download ---------------------------------------------


def _ext_from_url(url: str) -> str:
    """Extract image file extension from a URL, defaulting to .jpg."""
    path = urllib.parse.urlparse(url).path.lower()
    for e in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(e):
            return e
    # Studio CDN often uses .jpg even when content is webp; default jpg.
    return ".jpg"


def _download_image(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    try:
        data = http_get(url, timeout=20)
        dest.write_bytes(data)
        return True
    except Exception as e:
        log.warning("    image fail %s: %s", url, e)
        return False


def step_images(workers: int = 8) -> None:
    """Download cover + inline body images from Studio CDN to /public/images/legacy/.

    image_map.json schema:
      {
        "<slug>": {
          "cover": "/images/legacy/<slug>.jpg" | null,
          "body": {"<original_url>": "/images/legacy/<slug>-body-<n>.<ext>", ...}
        }
      }

    Both covers and inline body images are migrated so the new site is fully
    decoupled from Studio's CDN — required for safely retiring the old site.
    """
    if not SCRAPED_DIR.exists():
        log.error("  no scraped/ directory — run `scrape` first"); sys.exit(1)
    SITE_LEGACY_IMG.mkdir(parents=True, exist_ok=True)

    files = sorted(SCRAPED_DIR.glob("*.json"))
    log.info("[3/6] Downloading covers + inline images for %d articles -> %s",
             len(files), SITE_LEGACY_IMG)

    mapping: dict = {}
    success_cover, success_body, fail = 0, 0, 0
    start = time.time()

    def _do_one(jf: Path) -> tuple[str, dict]:
        d = json.loads(jf.read_text())
        slug = d["slug"]
        entry: dict = {"cover": None, "body": {}}

        cover = d.get("cover", "")
        if cover and cover.startswith("http"):
            ext = _ext_from_url(cover)
            dest = SITE_LEGACY_IMG / f"{slug}{ext}"
            if _download_image(cover, dest):
                entry["cover"] = f"/images/legacy/{slug}{ext}"

        for i, url in enumerate(d.get("extra_image_urls", []) or []):
            if not url.startswith("http"):
                continue
            ext = _ext_from_url(url)
            dest = SITE_LEGACY_IMG / f"{slug}-body-{i}{ext}"
            if _download_image(url, dest):
                entry["body"][url] = f"/images/legacy/{slug}-body-{i}{ext}"

        return slug, entry

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_do_one, f) for f in files]
        for i, fut in enumerate(as_completed(futs), 1):
            slug, entry = fut.result()
            mapping[slug] = entry
            if entry["cover"]:
                success_cover += 1
            else:
                fail += 1
            success_body += len(entry["body"])
            if i % 100 == 0 or i == len(files):
                rate = i / (time.time() - start) if time.time() > start else 0
                log.info("  [%d/%d] cover_ok=%d body_imgs=%d cover_fail=%d rate=%.1f/s",
                         i, len(files), success_cover, success_body, fail, rate)

    IMAGE_MAP_JSON.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("  wrote %s — covers=%d, body_images=%d, cover_fail=%d",
             IMAGE_MAP_JSON, success_cover, success_body, fail)


# --- Step 4: Claude classification --------------------------------------

NEWS_CATEGORIES = ["politics", "economy", "society", "tech"]
COLUMN_CATEGORIES = ["living", "food", "health", "procedures"]
NEWS_CATEGORY_DESC = {
    "politics": "政治・行政・EU 政策・選挙・税制・移民政策",
    "economy":  "経済・ビジネス・物価・労働・住宅・企業ニュース",
    "society":  "社会・事件・事故・気象・治安・教育・医療・地域生活",
    "tech":     "テック・スタートアップ・IT・科学技術",
}
COLUMN_CATEGORY_DESC = {
    "living":     "暮らし全般・住まい・地域・コミュニティ・文化",
    "food":       "食・グルメ・レシピ・スーパー・レストラン",
    "health":     "健康・医療・スポーツ・メンタル",
    "procedures": "行政手続き・ビザ・税務・銀行・保険・教育手続き",
}


def step_classify(batch_size: int = 30) -> None:
    """Use Claude to classify each scraped article into a category.

    Output: scripts/migrate_data/classified.json mapping slug -> category.
    Cost: ~$2-3 for ~1,500 articles at batch_size=30.
    """
    if not SCRAPED_DIR.exists():
        log.error("  no scraped/ directory — run `scrape` first"); sys.exit(1)
    try:
        from anthropic import Anthropic
    except ImportError:
        log.error("  anthropic SDK missing — pip install anthropic"); sys.exit(1)

    files = sorted(SCRAPED_DIR.glob("*.json"))
    articles = [json.loads(f.read_text()) for f in files]
    log.info("[4/6] Classifying %d articles in batches of %d", len(articles), batch_size)

    # Load existing partial result if present, to resume
    classified: dict = {}
    if CLASSIFIED_JSON.exists():
        classified = json.loads(CLASSIFIED_JSON.read_text())
        log.info("  resuming: %d already classified", len(classified))

    todo = [a for a in articles if a["slug"] not in classified]
    if not todo:
        log.info("  all already classified")
        return

    # Load Anthropic API key from .env (override=True so an empty inherited
    # env var doesn't shadow the file value).
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    client = Anthropic()
    model = "claude-sonnet-4-6"

    system_prompt = f"""あなたはオランダ在住日本人向けニュースサイトの記事分類器です。
記事のタイトル + 概要から、適切なカテゴリを 1 つ選んで返してください。

【news 用カテゴリ】(kind=news の場合)
{chr(10).join(f'- {k}: {v}' for k, v in NEWS_CATEGORY_DESC.items())}

【column 用カテゴリ】(kind=column の場合)
{chr(10).join(f'- {k}: {v}' for k, v in COLUMN_CATEGORY_DESC.items())}

複数記事をまとめて与えるので、各記事の slug をキーにカテゴリ名のみを JSON で返してください。
"""

    TOOL = {
        "name": "classify_batch",
        "description": "Return category mapping for a batch of articles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mappings": {
                    "type": "object",
                    "description": "slug -> category (one of the listed categories for that kind)",
                    "additionalProperties": {"type": "string"},
                }
            },
            "required": ["mappings"],
        },
    }

    start = time.time()
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        user_lines = []
        for a in batch:
            user_lines.append(
                f"slug={a['slug']} kind={a['kind']}\n"
                f"  title: {a['title']}\n"
                f"  desc:  {(a.get('description') or '')[:200]}"
            )
        user_content = "\n\n".join(user_lines)

        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "classify_batch"},
            messages=[{"role": "user", "content": user_content}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "classify_batch":
                for slug, cat in block.input.get("mappings", {}).items():
                    classified[slug] = cat
                break

        # Persist after every batch so we can resume on Ctrl-C
        CLASSIFIED_JSON.write_text(json.dumps(classified, ensure_ascii=False, indent=2), encoding="utf-8")
        done = len(classified)
        elapsed = time.time() - start
        rate = (done - (len(classified) - len(batch))) / elapsed if elapsed else 0
        log.info("  batch %d/%d -> total classified=%d", i // batch_size + 1,
                 (len(todo) + batch_size - 1) // batch_size, done)

    log.info("  done: %d classified (out of %d total articles)", len(classified), len(articles))


# --- Step 5: Markdown generation ----------------------------------------

def _yaml_str(s: str) -> str:
    """Quote a string safely for YAML frontmatter (single-line)."""
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rewrite_body_images(body_md: str, body_map: dict) -> str:
    """Replace Studio CDN URLs in the markdown body with local /images/legacy/ paths."""
    if not body_map:
        return body_md
    for original, local in body_map.items():
        body_md = body_md.replace(original, local)
    return body_md


def _build_news_md(d: dict, category: str, image_entry: dict) -> str:
    """Build a Markdown file body for the news collection (summary required)."""
    title = d["title"] or d["slug"]
    desc = d.get("description") or title  # description must be non-empty
    pub_date = d["pub_date"] or parse_studio_slug_to_iso(d["slug"]) or "2024-01-01"
    local_img = image_entry.get("cover") if isinstance(image_entry, dict) else None
    body_map = image_entry.get("body", {}) if isinstance(image_entry, dict) else {}

    # Build summary bullets (required: min 1, max 5)
    bullets: list[str] = []
    sb = d.get("summary_bullets", "")
    if sb:
        for line in sb.replace("<br>", "\n").split("\n"):
            line = line.strip()
            if line.startswith("・"):
                bullets.append(re.sub(r"^[・\s]+", "", line))
    if not bullets:
        # Fall back to first sentence(s) of description
        sent = re.split(r"(?<=[。！？])", desc, maxsplit=1)
        first = sent[0].strip() if sent and sent[0].strip() else desc[:100]
        bullets = [first]
    bullets = [b for b in bullets if b][:5] or [title]

    parts = ["---"]
    parts.append(f"title: {_yaml_str(title)}")
    parts.append(f"description: {_yaml_str(desc)}")
    parts.append(f"pubDate: '{pub_date}'")
    parts.append(f"category: {category}")
    if local_img:
        parts.append(f"image: {local_img}")
        parts.append(f"imageAlt: {_yaml_str(title)}")
    parts.append(f"sourceUrl: {d['url']}")
    parts.append("sourceName: HARRO LIFE (legacy)")
    parts.append("summary:")
    for b in bullets:
        parts.append(f"  - {_yaml_str(b)}")
    parts.append("---")
    parts.append("")
    parts.append(f"> 📦 この記事は旧 HARRO LIFE（[{d['url']}]({d['url']})）からの移行アーカイブです。")
    parts.append("")
    parts.append(_rewrite_body_images(d["body_md"] or "", body_map))
    return "\n".join(parts) + "\n"


def _build_column_md(d: dict, category: str, image_entry: dict) -> str:
    """Build a Markdown file body for the columns collection (no summary needed)."""
    title = d["title"] or d["slug"]
    desc = d.get("description") or title
    pub_date = d["pub_date"] or parse_studio_slug_to_iso(d["slug"]) or "2024-01-01"
    local_img = image_entry.get("cover") if isinstance(image_entry, dict) else None
    body_map = image_entry.get("body", {}) if isinstance(image_entry, dict) else {}

    parts = ["---"]
    parts.append(f"title: {_yaml_str(title)}")
    parts.append(f"description: {_yaml_str(desc)}")
    parts.append(f"pubDate: '{pub_date}'")
    parts.append(f"category: {category}")
    if local_img:
        parts.append(f"image: {local_img}")
        parts.append(f"imageAlt: {_yaml_str(title)}")
    parts.append("---")
    parts.append("")
    parts.append(f"> 📦 このコラムは旧 HARRO LIFE（[{d['url']}]({d['url']})）からの移行アーカイブです。")
    parts.append("")
    parts.append(_rewrite_body_images(d["body_md"] or "", body_map))
    return "\n".join(parts) + "\n"


def step_markdown() -> None:
    """Combine scraped articles + image map + classification into Markdown files."""
    if not SCRAPED_DIR.exists():
        log.error("  no scraped/ — run scrape first"); sys.exit(1)
    image_map = json.loads(IMAGE_MAP_JSON.read_text()) if IMAGE_MAP_JSON.exists() else {}
    classified = json.loads(CLASSIFIED_JSON.read_text()) if CLASSIFIED_JSON.exists() else {}

    SITE_NEWS.mkdir(parents=True, exist_ok=True)
    SITE_COLUMNS.mkdir(parents=True, exist_ok=True)

    files = sorted(SCRAPED_DIR.glob("*.json"))
    log.info("[5/6] Generating Markdown for %d articles", len(files))

    written = 0
    for f in files:
        d = json.loads(f.read_text())
        slug = d["slug"]
        kind = d["kind"]
        category = classified.get(slug) or ("society" if kind == "news" else "living")
        # image_entry may be a dict (new schema) or string/None (old schema for backward compat)
        raw = image_map.get(slug)
        if isinstance(raw, str) or raw is None:
            image_entry = {"cover": raw, "body": {}}
        else:
            image_entry = raw

        rendered = (_build_news_md(d, category, image_entry) if kind == "news"
                    else _build_column_md(d, category, image_entry))
        out_dir = SITE_NEWS if kind == "news" else SITE_COLUMNS
        out_path = out_dir / f"legacy-{slug}.md"
        out_path.write_text(rendered, encoding="utf-8")
        written += 1
        if written % 200 == 0:
            log.info("  wrote %d / %d", written, len(files))

    log.info("  done: %d Markdown files written", written)


# --- Step 6: _redirects ------------------------------------------------


def step_redirects() -> None:
    """Generate Cloudflare Pages _redirects file mapping old Studio URLs -> new."""
    if not URLS_JSON.exists():
        log.error("  urls.json missing — run `urls` first"); sys.exit(1)
    buckets = json.loads(URLS_JSON.read_text())

    lines = ["# Auto-generated by scripts/migrate.py — old HARRO LIFE -> new", ""]

    # /articles/{slug} -> /news/legacy-{slug}
    for u in buckets.get("articles", []):
        path = urllib.parse.urlparse(u).path  # /articles/290426-3
        slug = path.split("/")[-1]
        lines.append(f"{path} /news/legacy-{slug} 301")

    # /life/column/{slug} -> /columns/legacy-{slug}
    for u in buckets.get("columns", []):
        path = urllib.parse.urlparse(u).path
        slug = path.split("/")[-1]
        lines.append(f"{path} /columns/legacy-{slug} 301")

    # Index pages: send to new homepage equivalents
    lines.append("")
    lines.append("# Index pages")
    lines.append("/life /news 301")
    lines.append("/life/column /columns 301")

    SITE_REDIRECTS.parent.mkdir(parents=True, exist_ok=True)
    SITE_REDIRECTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("[6/6] wrote %s with %d redirect rules", SITE_REDIRECTS, len(lines) - 4)


# --- Entry --------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("urls")
    sp_scrape = sub.add_parser("scrape")
    sp_scrape.add_argument("--limit", type=int, default=None)
    sp_scrape.add_argument("--workers", type=int, default=4)
    sub.add_parser("images")
    sub.add_parser("classify")
    sub.add_parser("markdown")
    sub.add_parser("redirects")
    sub.add_parser("all")
    args = p.parse_args()

    if args.cmd == "urls":
        step_urls()
    elif args.cmd == "scrape":
        step_scrape(limit=args.limit, workers=args.workers)
    elif args.cmd == "images":
        step_images()
    elif args.cmd == "classify":
        step_classify()
    elif args.cmd == "markdown":
        step_markdown()
    elif args.cmd == "redirects":
        step_redirects()
    elif args.cmd == "all":
        step_urls()
        step_scrape()
        step_images()
        step_classify()
        step_markdown()
        step_redirects()
    return 0


if __name__ == "__main__":
    sys.exit(main())
