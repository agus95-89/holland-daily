"""Generate sample episode using Gemini 2.5 Flash TTS Multi-Speaker.

Modeled after press-release-bot's dialog generation. Distinct improvements vs.
the earlier Google Cloud TTS (Neural2) sample:
- Multi-speaker TTS in a single request → naturally connected dialog tone
- Announcer (Speaker 1, Kore) handles intro / news body / outro
- Commentator (Speaker 2, Puck) only adds short background notes
- Reading-error mitigation rules added to system prompt + post-process regex

Borrows GEMINI_API_KEY from press-release-bot/.env if not set in our own.
Output: ~/Desktop/HARRO-LIFE-sample-C-gemini.mp3
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
import wave
from datetime import date
from pathlib import Path

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydub import AudioSegment

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
# Borrow GEMINI_API_KEY from neighboring project if not set locally
load_dotenv(ROOT.parent / "press-release-bot" / ".env", override=False)

sys.path.insert(0, str(ROOT))
from src.summarize import Summary  # noqa: E402

AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sample-dialog-gemini")

TTS_MODEL = "gemini-2.5-flash-preview-tts"
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2

# Voice picks — same as press-release-bot reference
ANNOUNCER_VOICE = "Kore"   # 女性、明るくクリア
COMMENTATOR_VOICE = "Puck"  # 男性、自然

# ──────────────────────────────────────────────────────────────────────
# Script-generation system prompt — announcer leads, commentator only adds notes
# ──────────────────────────────────────────────────────────────────────

DIALOG_SYSTEM_PROMPT = """あなたはオランダ在住日本人向け日刊ニュースポッドキャスト「HARRO LIFE」の台本ライターです。
この番組は、アムステルフェーンの日本食スーパー「HARRO」が提供しています。

【番組構成 — アナウンサー主導 + 解説者の補足】
- Speaker 1 (アナウンサー、女性): **番組進行・ニュース本文の読み上げ・締め** をすべて担当
- Speaker 2 (解説者、男性): 各ニュースに対する **背景・補足を必要な分だけ** 入れる
- アナウンサーが主役。ニュース本文は必ずアナウンサーが読み、解説者は補足を担当

【各記事のターンの流れ】
1. アナウンサー: カテゴリ橋渡し + 記事本文 (聴き手に伝わる話し言葉、3〜6 文)
2. 解説者: 背景・補足 (**長さは内容次第**: シンプルな話題なら 1〜2 文、複雑な話題で読者の理解を助けるならしっかり 3〜5 文)
3. アナウンサー: 短い相づち or 次のニュースへの橋渡し (1 文、例: 「○○ですね」「続いては〜」)

【解説者の補足の方針】
- **「分かりやすさ最優先」**: ユーザーが理解できるなら短くて良い、補足が必要なら長くてよい
- 無理に削らない、無理に膨らませない
- 解説の例:
  - 政策ニュース → 過去の経緯・関連法・影響を受ける層
  - 経済ニュース → 数字の意味、過去との比較、今後の論点
  - 社会ニュース → 制度的背景、地域特性、再発防止策の動向
- 推測・予測・主観は禁止 (記事内事実 / 公開済みの背景情報のみ)

【読み方の重要ルール — TTS 誤読を防ぐため】

**漢字の読み違いを避けるため、以下は必ずひらがな表記にする** (Google TTS が誤読しやすい):
- 「数ヶ月」「数か月」→ **「すうかげつ」** とひらがなで
- 「数日」→ **「すうじつ」**
- 「数週間」→ **「すうしゅうかん」**
- 「数年」→ **「すうねん」**
- 「一日」→ 文脈に応じて **「いちにち」** or **「ついたち」** とひらがなで明示
- 「七時」→ **「しちじ」**、「八時」→ **「はちじ」** と漢字+ひらがなを並べるか、ひらがなで
- 「ヶ月」「ヵ月」のような小さい「ヶ」「ヵ」を含む表記は使わない

**ブランド名はカタカナ表記が必須**:
- 「HARRO」→ **「ハロー」** (絶対にアルファベット表記で残さない)
- 「HARRO LIFE」→ **「ハロー・ライフ」**
- 旧称「Holland Daily」「ホランドデイリー」は使用しない

**その他の表記**:
- 数字は自然な日本語: 「2026年」「3億ユーロ」「25パーセント」
- 英語・オランダ語の固有名詞は原則カタカナ、必要なら短く原語を補足

【オープニング (Speaker 1 アナウンサー)】
- 「アムステルフェーンの日本食スーパー、ハローがお届けする、ハロー・ライフ」と番組名
- 「オランダのニュースを、日本語の音声でお届けします」
- 今日の日付
- 今日のハイライト 1 文

【クロージング (Speaker 1 アナウンサー)】
1. 今日のニュース総括 1 文
2. 与えられる **HARRO からの一言** を自然に読み上げる (宣伝臭くせず、おまけ的に)
3. 「明日もぜひお聴きください」

【出力フォーマット】
Speaker 1: (アナウンサーの台詞)

Speaker 2: (解説者の台詞)

Speaker 1: (次のターン)

- 各話者の発話の **間に空行を 1 行入れる** (改行 2 つ) — 音声合成時の間を取るため
- 文末は必ず句点「。」で終える (TTS が間を認識するため)
- "Speaker 1:" "Speaker 2:" 以外のラベル・タグ・絵文字・記号装飾は使用しない

【絶対ルール】
- アナウンサーが本文をしっかり読む。解説者は「補足ですが」「背景としては」などのフレーズで自然に入る
- HARRO からの一言は宣伝臭くせず、自然に短く温かく

【厳密な文字数指示 — 必ず守ってください】
- **目標: 合計 7000〜7500 字 (約 15〜17 分の音声)、最大 20 分まで OK**
- これは絶対の目標で、**短く出力してはいけません**
- ニュース 10 本それぞれが **700〜800 字** になるように書く
  - 各ニュース内訳: アナウンサー本文 (4〜6 文 ≒ 350〜450 字) + 解説者補足 (3〜5 文 ≒ 250〜350 字) + アナウンサーの繋ぎ (1〜2 文 ≒ 50〜100 字)
- 解説者の補足を *惜しまない*。背景・経緯・関連事実をしっかり盛り込む
- 全体構成: オープニング 200〜300 字 + 本編 7000 字前後 (10 本 × 約 700 字) + クロージング 300〜500 字
- **書き終わる前に文字数を意識して、6500 字未満で終わらないこと**
"""

# ──────────────────────────────────────────────────────────────────────
# Post-process safety net — regex-based reading fixes for known mis-reads
# ──────────────────────────────────────────────────────────────────────

READING_FIXES = [
    # 数 + 期間 — Google TTS が「かず」と訓読みしてしまう罠
    (re.compile(r"数ヶ月"), "すうかげつ"),
    (re.compile(r"数か月"), "すうかげつ"),
    (re.compile(r"数ヵ月"), "すうかげつ"),
    (re.compile(r"数週間"), "すうしゅうかん"),
    (re.compile(r"数日間"), "すうじつかん"),
    (re.compile(r"(?<!十|百|千|万)数日(?![間])"), "すうじつ"),
    (re.compile(r"数年間"), "すうねんかん"),
    (re.compile(r"(?<!十|百|千|万)数年(?![間前後])"), "すうねん"),
    (re.compile(r"数年前"), "すうねんまえ"),
    (re.compile(r"数年後"), "すうねんご"),
    (re.compile(r"数か所"), "すうかしょ"),
    # ブランド safety net
    (re.compile(r"\bHARRO\s+LIFE\b"), "ハロー・ライフ"),
    (re.compile(r"\bHARRO\b"), "ハロー"),
]


def apply_reading_fixes(script: str) -> str:
    out = script
    for pattern, replacement in READING_FIXES:
        out = pattern.sub(replacement, out)
    return out


# ──────────────────────────────────────────────────────────────────────
# Frontmatter → Summary reconstruction (same as sample_dialog.py)
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
# Script generation via Claude
# ──────────────────────────────────────────────────────────────────────

# HARRO sign-offs (mirrors src/script.py — kept here to avoid relative-import issues)
HARRO_SIGN_OFFS = [
    "ハローはアムステルフェーンの店舗、そしてオランダ全国一律3.95ユーロでお届けするオンラインショップで、皆さまの食卓をお手伝いしています。",
    "ハローでは、お醤油や味噌などの定番調味料から、お惣菜、職人の手による和食器まで、日本の食と暮らしを幅広くお取り扱いしています。",
    "ハローのオンラインショップは、オランダ全国一律3.95ユーロ、70ユーロ以上のお買い物で送料無料です。",
    "アムステルフェーンの中心、Stadshart から徒歩1分。ハローは毎日朝10時から夜8時までオープンしています。",
    "週末は、ハローのお酒コーナーから、日本酒や梅酒、焼酎などをぜひのぞいてみてください。",
    "週末のお買い物には、ハローの店頭で揃うおにぎりやお惣菜もおすすめです。",
    "ハローは日曜日もオープンしています。お休みの日のご来店も、ぜひお気軽に。",
]


def build_dialog_script(
    summaries: list[Summary],
    today: date,
    client: Anthropic,
    model: str,
) -> str:
    grouped: dict[str, list[Summary]] = {}
    for s in sorted(summaries, key=lambda x: -x.importance):
        grouped.setdefault(s.category, []).append(s)

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
            parts.append(f"    要約: {s.summary_ja}")
            parts.append(f"    重要度: {s.importance}/5")
        parts.append("")

    user_content = "\n".join(parts)

    resp = client.messages.create(
        model=model,
        max_tokens=12000,
        system=[
            {
                "type": "text",
                "text": DIALOG_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    script = "".join(text_parts).strip()
    script = apply_reading_fixes(script)
    log.info("Generated script: %d chars", len(script))
    return script


# ──────────────────────────────────────────────────────────────────────
# Gemini Multi-Speaker TTS
# ──────────────────────────────────────────────────────────────────────


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


SPEAKER_LINE_RE = re.compile(r"^Speaker [12]:", re.MULTILINE)


def split_script_into_chunks(script: str, max_chars_per_chunk: int = 1500) -> list[str]:
    """Split the script at speaker-turn boundaries so each chunk is under the
    char limit. Smaller chunks (≈2-3 min audio each) avoid Gemini TTS's
    long-form artifact / whine. Returns clean chunks ready for separate TTS calls.
    """
    lines = script.split("\n")
    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0

    for line in lines:
        is_speaker_start = bool(re.match(r"^Speaker [12]:", line))
        line_chars = len(line)

        # Boundary: only split *between* turns (at the start of a Speaker line)
        # and only if the current chunk is over the threshold.
        if is_speaker_start and current_chars + line_chars > max_chars_per_chunk and current:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(line)
        current_chars += line_chars

    if current:
        chunks.append(current)

    return [("\n".join(c)).strip() for c in chunks if any(l.strip() for l in c)]


def _gemini_speech_config() -> types.SpeechConfig:
    return types.SpeechConfig(
        multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
            speaker_voice_configs=[
                types.SpeakerVoiceConfig(
                    speaker="Speaker 1",
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=ANNOUNCER_VOICE,
                        ),
                    ),
                ),
                types.SpeakerVoiceConfig(
                    speaker="Speaker 2",
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=COMMENTATOR_VOICE,
                        ),
                    ),
                ),
            ]
        )
    )


def _synth_one_chunk(client: genai.Client, text: str) -> AudioSegment:
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=_gemini_speech_config(),
    )
    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=text,
        config=config,
    )
    if not response.candidates:
        raise RuntimeError("Gemini TTS returned no candidates")
    parts = response.candidates[0].content.parts
    if not parts or not parts[0].inline_data:
        raise RuntimeError("Gemini TTS returned no audio data")
    pcm_bytes = parts[0].inline_data.data
    wav_bytes = _pcm_to_wav_bytes(pcm_bytes)
    return AudioSegment.from_wav(io.BytesIO(wav_bytes))


def synthesize_gemini(script_text: str, out_path: Path) -> Path:
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not found — set in either HARRO LIFE .env or "
            "press-release-bot/.env"
        )

    client = genai.Client(api_key=api_key)

    # Smaller chunks → more speaker-turn boundaries become inter-chunk silences,
    # giving the dialog a more natural pause cadence between speakers.
    chunks = split_script_into_chunks(script_text, max_chars_per_chunk=800)
    log.info("Splitting script into %d chunks (max ~800 chars each) for TTS...",
             len(chunks))

    inter_chunk_silence = AudioSegment.silent(duration=700)
    parts: list[AudioSegment] = []
    for i, chunk in enumerate(chunks, 1):
        log.info("  Synthesizing chunk %d/%d (%d chars, voices=%s+%s)...",
                 i, len(chunks), len(chunk), ANNOUNCER_VOICE, COMMENTATOR_VOICE)
        seg = _synth_one_chunk(client, chunk)
        parts.append(seg)

    if not parts:
        raise RuntimeError("No audio chunks synthesized")

    # Stitch with brief silence between chunks for a natural break
    combined: AudioSegment = parts[0]
    for seg in parts[1:]:
        combined = combined + inter_chunk_silence + seg

    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.export(str(out_path), format="mp3", bitrate="128k")
    log.info("MP3 written: %s (%d KB, %.1f sec, %d chunks stitched)",
             out_path, out_path.stat().st_size // 1024,
             len(combined) / 1000, len(chunks))
    return out_path


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
    summaries = load_summaries_from_md(news_dir, target_date_str)
    if not summaries:
        log.error("No markdown files for %s", target_date_str)
        return 1
    log.info("Loaded %d summaries from %s", len(summaries), target_date_str)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set in .env")
        return 1
    claude_client = Anthropic(api_key=api_key)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    log.info("Generating dialog script via %s...", model)
    script = build_dialog_script(summaries, target_date, claude_client, model)

    script_path = Path(f"/tmp/sample-dialog-gemini-{target_date_str}-script.txt")
    script_path.write_text(script, encoding="utf-8")
    log.info("Script saved: %s", script_path)

    out_path = Path(f"/Users/daisuke.suga/Desktop/HARRO-LIFE-sample-E-split.mp3")
    synthesize_gemini(script, out_path)

    log.info("=" * 60)
    log.info("Sample MP3 ready: %s", out_path)
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
