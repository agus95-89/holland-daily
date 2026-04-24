from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from google.cloud import texttospeech
from pydub import AudioSegment

log = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r"<(F|M)>(.*?)</\1>", re.DOTALL)
SENTENCE_SPLIT = re.compile(r"(?<=[。！？])\s*")
MAX_CHUNK_CHARS = 2500


def script_to_mp3(
    script: str,
    out_path: Path,
    intro_voice: str,
    body_voice: str,
    speaking_rate: float = 1.05,
) -> None:
    client = texttospeech.TextToSpeechClient()
    segments = _parse_script(script)
    if not segments:
        raise ValueError("Script has no <F> or <M> tagged segments")

    log.info("Synthesizing %d segments", len(segments))
    combined = AudioSegment.silent(duration=400)
    for i, (tag, text) in enumerate(segments):
        voice_name = intro_voice if tag == "F" else body_voice
        audio = _synthesize(client, text, voice_name, speaking_rate)
        combined += audio + AudioSegment.silent(duration=350)
        if (i + 1) % 5 == 0:
            log.info("  %d/%d segments synthesized", i + 1, len(segments))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.export(out_path, format="mp3", bitrate="128k")
    log.info("MP3 written: %s (%d KB)", out_path, out_path.stat().st_size // 1024)


def _parse_script(script: str) -> list[tuple[str, str]]:
    result = []
    for m in TAG_PATTERN.finditer(script):
        tag = m.group(1)
        text = m.group(2).strip()
        if text:
            result.append((tag, text))
    return result


def _synthesize(
    client: texttospeech.TextToSpeechClient,
    text: str,
    voice_name: str,
    rate: float,
) -> AudioSegment:
    audio = AudioSegment.silent(duration=0)
    for chunk in _chunk_text(text, MAX_CHUNK_CHARS):
        synthesis_input = texttospeech.SynthesisInput(text=chunk)
        voice = texttospeech.VoiceSelectionParams(
            language_code="ja-JP",
            name=voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=rate,
        )
        resp = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
        )
        audio += AudioSegment.from_file(io.BytesIO(resp.audio_content), format="mp3")
    return audio


def _chunk_text(text: str, max_chars: int) -> list[str]:
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
