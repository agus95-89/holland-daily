"""Generate a sample episode in dialog format (announcer + commentator).

Reads existing pipeline-generated news Markdown from harro-life-site/src/content/news/,
reconstructs Summary objects from the frontmatter, then generates a new script
with an alternative SYSTEM_PROMPT and synthesizes it with two distinct voices
and speaking rates. Output goes to /tmp/sample-dialog-{date}.mp3 — production is
untouched.

Usage:
    python -m scripts.sample_dialog [YYYY-MM-DD]    # default: 2026-05-01
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
from google.cloud import texttospeech

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

# Make repo root importable for `src.*` modules
sys.path.insert(0, str(ROOT))

from src.summarize import Summary  # noqa: E402
import src.script as script_mod  # noqa: E402

# We re-implement TTS locally without pydub/ffmpeg — sample only, no silence gaps
TAG_PATTERN = re.compile(r"<(F|M)>(.*?)</\1>", re.DOTALL)
SENTENCE_SPLIT = re.compile(r"(?<=[。！？])\s*")
MAX_CHUNK_CHARS = 2500

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sample-dialog")

# ──────────────────────────────────────────────────────────────────────
# Alternative SYSTEM_PROMPT — announcer (F) + commentator (M)
# ──────────────────────────────────────────────────────────────────────

DIALOG_SYSTEM_PROMPT = """あなたはオランダ在住日本人向け日刊ニュースポッドキャスト「HARRO LIFE」の台本ライターです。
この番組は、アムステルフェーンの日本食スーパー「HARRO」が提供しています。
与えられた記事の要約を基に、12〜15分で読み上げ可能な自然な日本語の台本を書いてください。

【番組の体裁 — アナウンサー + 解説者の 2 人体制】
- アナウンサー (女性) は司会・記事紹介・橋渡し・締めを担当
- 解説者 (男性) は各記事の本文読み + 短い背景補足を担当
- 解説者の補足は記事内の事実 / 公開情報の整理に限る。「個人的見解」「推測」「予測」は禁止
- 二人の対話のテンポは自然に。「○○さんお願いします」のような無理な掛け合いは避け、
  「続いては経済のニュースです」「ありがとうございます。背景としては…」程度にとどめる
- アナウンサーは記事の前後で短く相づちを入れる程度で、本文は解説者が読む

【読み方の重要ルール】
- 「HARRO」は必ずカタカナで「ハロー」と書く (TTS が letter-by-letter で読まないため)
- 番組名「HARRO LIFE」は必ず「ハロー・ライフ」と書く
- 旧称「Holland Daily」「ホランドデイリー」は使わない

【構成】
- 冒頭: アナウンサーがオープニング
  - 「アムステルフェーンの日本食スーパー、ハローがお届けする、ハロー・ライフ」と番組名を告げる
  - 「オランダのニュースを、日本語の音声でお届けします」のような番組説明を 1 文
  - 今日の日付
  - 今日のハイライトを 1 文
- 本編: カテゴリごとに区切り
  - アナウンサー: 「続いては〇〇のニュースです」(短い橋渡し)
  - 解説者: 各記事の本文読み (要約をそのまま読むのではなく、聴き手に伝わる話し言葉に)
           + 1〜2 文の客観的な背景補足
  - アナウンサーは記事間で短い相づち or 次への橋渡し
- 結び: アナウンサーが
  1. その日のニュースを総括する一言
  2. 与えられる **HARROからの一言** をそのまま自然に読み上げる (宣伝色を出さず、おまけ的に)
  3. 「明日もぜひお聴きください」の挨拶

【出力フォーマット】
<F>アナウンサー (女性) の台詞</F>
<M>解説者 (男性) の台詞</M>

【絶対ルール】
- <F> と <M> 以外のタグ、マークダウン、絵文字、記号装飾は使用しない
- 書き言葉ではなく話し言葉、耳で聴いて理解しやすい構文
- 英語・オランダ語の固有名詞は原則カタカナ表記
- 数字は「2026年」「3億ユーロ」「25パーセント」のように自然な日本語
- 目標文字数: 5000〜5500字 (約 13 分)
- 解説者の補足は事実ベース、推測・主観・予測は出さない
- HARROからの一言は宣伝臭くならず、自然に短く温かい口調で
"""

# ──────────────────────────────────────────────────────────────────────
# Local TTS — direct MP3 byte concatenation (no pydub/ffmpeg)
# ──────────────────────────────────────────────────────────────────────


def _chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = [s for s in SENTENCE_SPLIT.split(text) if s]
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) > max_chars and buf:
            chunks.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        chunks.append(buf)
    return chunks


def _synth_chunk(client, text: str, voice_name: str, rate: float) -> bytes:
    voice = texttospeech.VoiceSelectionParams(language_code="ja-JP", name=voice_name)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=rate,
    )
    resp = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=voice,
        audio_config=audio_config,
    )
    return resp.audio_content


def _synthesize_dialog_to_mp3(
    script: str,
    out_path: Path,
    intro_voice: str,
    body_voice: str,
    intro_rate: float,
    body_rate: float,
) -> None:
    """Synthesize each <F>/<M> segment and concatenate raw MP3 bytes.

    No silence gaps between segments — sample only. Most players handle the
    naive frame concatenation fine for short demo files.
    """
    client = texttospeech.TextToSpeechClient()
    segments = [(m.group(1), m.group(2).strip()) for m in TAG_PATTERN.finditer(script)]
    segments = [(t, s) for t, s in segments if s]
    if not segments:
        raise ValueError("Script has no <F> or <M> tagged segments")

    log.info("Synthesizing %d segments...", len(segments))
    parts: list[bytes] = []
    for i, (tag, text) in enumerate(segments, 1):
        voice_name = intro_voice if tag == "F" else body_voice
        rate = intro_rate if tag == "F" else body_rate
        for chunk in _chunk_text(text):
            parts.append(_synth_chunk(client, chunk, voice_name, rate))
        if i % 5 == 0:
            log.info("  %d/%d segments synthesized", i, len(segments))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        for p in parts:
            f.write(p)
    log.info("MP3 written: %s (%d KB, %d segments)",
             out_path, out_path.stat().st_size // 1024, len(segments))


# ──────────────────────────────────────────────────────────────────────
# Frontmatter → Summary reconstruction
# ──────────────────────────────────────────────────────────────────────

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
        if isinstance(bullets, list):
            bullet_block = "\n".join(f"・{b}" for b in bullets)
        else:
            bullet_block = ""

        description = meta.get("description") or ""
        summary_ja = (description + ("\n" + bullet_block if bullet_block else "")).strip()

        category_en = (meta.get("category") or "").strip()
        category_ja = CATEGORY_MAP_EN_TO_JA.get(category_en, "社会・事件")

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


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    target_date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-05-01"
    try:
        y, mth, d = (int(x) for x in target_date_str.split("-"))
        target_date = date(y, mth, d)
    except (ValueError, AttributeError):
        log.error("Invalid date %s, expected YYYY-MM-DD", target_date_str)
        return 1

    news_dir = ROOT.parent / "harro-life-site" / "src" / "content" / "news"
    if not news_dir.exists():
        log.error("News dir not found: %s", news_dir)
        return 1

    summaries = load_summaries_from_md(news_dir, target_date_str)
    if not summaries:
        log.error("No markdown files for %s in %s", target_date_str, news_dir)
        return 1
    log.info("Loaded %d summaries from %s", len(summaries), target_date_str)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set in .env")
        return 1
    client = Anthropic(api_key=api_key)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # Monkey-patch the SYSTEM_PROMPT used by build_script (in-process only)
    original_prompt = script_mod.SYSTEM_PROMPT
    script_mod.SYSTEM_PROMPT = DIALOG_SYSTEM_PROMPT
    try:
        log.info("Generating dialog-format script with %s...", model)
        script = script_mod.build_script(summaries, target_date, client, model)
    finally:
        script_mod.SYSTEM_PROMPT = original_prompt

    script_path = Path(f"/tmp/sample-dialog-{target_date_str}-script.txt")
    script_path.write_text(script, encoding="utf-8")
    log.info("Script saved to %s (%d chars)", script_path, len(script))

    # TTS settings — distinct voices and speaking rates
    intro_voice = "ja-JP-Neural2-B"   # アナウンサー (女性、明るくクリア)
    body_voice = "ja-JP-Neural2-D"    # 解説者 (男性、低め・落ち着き)
    intro_rate = 1.05                 # 通常スピード
    body_rate = 0.95                  # 解説者はやや落ち着いて

    out_path = Path(f"/tmp/sample-dialog-{target_date_str}.mp3")
    log.info("Synthesizing TTS — announcer=%s @ %.2fx, commentator=%s @ %.2fx",
             intro_voice, intro_rate, body_voice, body_rate)
    _synthesize_dialog_to_mp3(
        script=script,
        out_path=out_path,
        intro_voice=intro_voice,
        body_voice=body_voice,
        intro_rate=intro_rate,
        body_rate=body_rate,
    )

    log.info("=" * 60)
    log.info("Sample MP3 ready: %s", out_path)
    log.info("Open with: open %s", out_path)
    log.info("Compare with current: open %s/docs/episodes/%s.mp3",
             ROOT, target_date_str)
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
