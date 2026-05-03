"""Generate sample episode using production src/script.py + src/tts.py.

This bypasses the live RSS / summarization steps by reconstructing Summary
objects from already-published harro-life-site Markdown files. Useful for
iterating on prompt + TTS settings without re-running the full pipeline.

Output: ~/Desktop/HARRO-LIFE-sample-{label}.mp3
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
load_dotenv(ROOT.parent / "press-release-bot" / ".env", override=False)

sys.path.insert(0, str(ROOT))
from src.summarize import Summary  # noqa: E402
from src.script import build_script  # noqa: E402
from src import tts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sample-dialog-gemini")

CATEGORY_MAP_EN_TO_JA = {
    "society": "社会・事件",
    "economy": "経済・ビジネス",
    "politics": "政治・政策",
    "tech": "テック・スタートアップ",
    "living": "生活・文化",
    "food": "生活・文化",
    "health": "生活・文化",
    "procedures": "生活・文化",
}
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def load_summaries_from_md(news_dir: Path, date_prefix: str) -> list[Summary]:
    files = sorted(news_dir.glob(f"{date_prefix}-*.md"))
    out: list[Summary] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            continue
        meta = yaml.safe_load(m.group(1)) or {}
        bullets = meta.get("summary") or []
        bullet_block = "\n".join(f"・{b}" for b in bullets) if isinstance(bullets, list) else ""
        description = meta.get("description") or ""
        summary_ja = (description + ("\n" + bullet_block if bullet_block else "")).strip()
        category_ja = CATEGORY_MAP_EN_TO_JA.get((meta.get("category") or "").strip(), "社会・事件")
        if meta.get("breaking"):
            importance = 5
        elif meta.get("featured"):
            importance = 4
        else:
            importance = 3
        out.append(
            Summary(
                title_ja=str(meta.get("title") or "").strip(),
                summary_ja=summary_ja,
                category=category_ja,
                importance=importance,
                original_title=str(meta.get("title") or "").strip(),
                original_link=str(meta.get("sourceUrl") or "").strip(),
                source=str(meta.get("sourceName") or "オランダ").strip(),
            )
        )
    return out


def main() -> int:
    target_date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-05-01"
    label = sys.argv[2] if len(sys.argv) > 2 else "F"
    try:
        y, mth, d = (int(x) for x in target_date_str.split("-"))
        target_date = date(y, mth, d)
    except (ValueError, AttributeError):
        log.error("Invalid date %s", target_date_str)
        return 1

    news_dir = ROOT.parent / "harro-life-site" / "src" / "content" / "news"
    summaries = load_summaries_from_md(news_dir, target_date_str)
    if not summaries:
        log.error("No markdown files for %s in %s", target_date_str, news_dir)
        return 1
    log.info("Loaded %d summaries from %s", len(summaries), target_date_str)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set in .env")
        return 1
    claude_client = Anthropic(api_key=api_key)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    log.info("Generating script via production build_script (model=%s)...", model)
    script = build_script(summaries, target_date, claude_client, model)

    script_path = Path(f"/tmp/sample-dialog-gemini-{target_date_str}-script.txt")
    script_path.write_text(script, encoding="utf-8")
    log.info("Script saved: %s (%d chars)", script_path, len(script))

    # Load TTS config from sources.yaml so sample matches production
    cfg_path = ROOT / "config" / "sources.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    tts_cfg = cfg["tts"]
    log.info("TTS config: voices=%s+%s, chunk_max_chars=%d, silence=%dms, bitrate=%s",
             tts_cfg["announcer_voice"], tts_cfg["commentator_voice"],
             tts_cfg["chunk_max_chars"], tts_cfg["inter_chunk_silence_ms"],
             tts_cfg["bitrate"])

    out_path = Path(f"/Users/daisuke.suga/Desktop/HARRO-LIFE-sample-{label}.mp3")
    tts.script_to_mp3(
        script,
        out_path,
        announcer_voice=tts_cfg["announcer_voice"],
        commentator_voice=tts_cfg["commentator_voice"],
        chunk_max_chars=tts_cfg["chunk_max_chars"],
        inter_chunk_silence_ms=tts_cfg["inter_chunk_silence_ms"],
        bitrate=tts_cfg["bitrate"],
    )

    log.info("=" * 60)
    log.info("Sample MP3 ready: %s", out_path)
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
