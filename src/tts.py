"""Generate podcast MP3 from a Speaker 1 / Speaker 2 dialog script using
Gemini 2.5 Flash TTS Multi-Speaker.

The script is split at speaker-turn boundaries into ~800-char chunks so each
TTS call stays short enough to avoid the high-frequency artifact that Gemini
Multi-Speaker produces on long single-shot syntheses (validated 2026-05-02:
4-min single shot is clean, 11-min single shot has audible whine; 11×~1-min
chunks stitched with 700 ms silence are clean).

Each chunk is synthesized in one Multi-Speaker call (the announcer/commentator
voices stay consistent). Chunks are concatenated with a brief inter-chunk
silence that doubles as a natural pause between speakers.
"""
from __future__ import annotations

import io
import logging
import os
import re
import wave
from pathlib import Path

from google import genai
from google.genai import types
from pydub import AudioSegment

import imageio_ffmpeg

log = logging.getLogger(__name__)

# Point pydub at the bundled ffmpeg binary so we don't need a system install
# (also keeps macOS dev parity with the GitHub Actions runner).
AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

TTS_MODEL = "gemini-2.5-flash-preview-tts"
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM

SPEAKER_LINE_RE = re.compile(r"^Speaker [12]:")


def split_script_into_chunks(script: str, max_chars_per_chunk: int = 800) -> list[str]:
    """Split the script at speaker-turn boundaries, keeping each chunk under
    the char limit. Speaker turns are never split; we only break *between*
    turns, at the start of a "Speaker N:" line.
    """
    lines = script.split("\n")
    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0

    for line in lines:
        is_speaker_start = bool(SPEAKER_LINE_RE.match(line))
        line_chars = len(line)

        if is_speaker_start and current_chars + line_chars > max_chars_per_chunk and current:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(line)
        current_chars += line_chars

    if current:
        chunks.append(current)

    return [("\n".join(c)).strip() for c in chunks if any(l.strip() for l in c)]


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


def _gemini_speech_config(announcer_voice: str, commentator_voice: str) -> types.SpeechConfig:
    return types.SpeechConfig(
        multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
            speaker_voice_configs=[
                types.SpeakerVoiceConfig(
                    speaker="Speaker 1",
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=announcer_voice,
                        ),
                    ),
                ),
                types.SpeakerVoiceConfig(
                    speaker="Speaker 2",
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=commentator_voice,
                        ),
                    ),
                ),
            ]
        )
    )


def _synth_one_chunk(
    client: genai.Client,
    text: str,
    announcer_voice: str,
    commentator_voice: str,
) -> AudioSegment:
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=_gemini_speech_config(announcer_voice, commentator_voice),
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


def script_to_mp3(
    script: str,
    out_path: Path,
    announcer_voice: str = "Kore",
    commentator_voice: str = "Puck",
    chunk_max_chars: int = 800,
    inter_chunk_silence_ms: int = 700,
    bitrate: str = "128k",
) -> None:
    """Synthesize a Speaker 1 / Speaker 2 dialog script into an MP3 file.

    Splits the script into ~chunk_max_chars-char chunks at speaker-turn
    boundaries, calls Gemini Multi-Speaker TTS once per chunk, then stitches
    the audio with a brief silence between chunks for natural pacing.
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    client = genai.Client(api_key=api_key)

    chunks = split_script_into_chunks(script, max_chars_per_chunk=chunk_max_chars)
    if not chunks:
        raise ValueError("Script produced no chunks (no Speaker 1/Speaker 2 lines?)")
    log.info(
        "TTS: %d chunks (max %d chars each), voices=%s+%s, inter-chunk silence=%dms",
        len(chunks), chunk_max_chars, announcer_voice, commentator_voice, inter_chunk_silence_ms,
    )

    silence = AudioSegment.silent(duration=inter_chunk_silence_ms)
    parts: list[AudioSegment] = []
    for i, chunk in enumerate(chunks, 1):
        log.info("  Synthesizing chunk %d/%d (%d chars)...", i, len(chunks), len(chunk))
        seg = _synth_one_chunk(client, chunk, announcer_voice, commentator_voice)
        parts.append(seg)

    combined: AudioSegment = parts[0]
    for seg in parts[1:]:
        combined = combined + silence + seg

    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.export(str(out_path), format="mp3", bitrate=bitrate)
    log.info(
        "MP3 written: %s (%d KB, %.1f sec, %d chunks stitched)",
        out_path, out_path.stat().st_size // 1024, len(combined) / 1000, len(chunks),
    )
