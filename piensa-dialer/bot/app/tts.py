"""Audio helpers: convert any input to an 8 kHz mono WAV for Asterisk, and
synthesize Spanish prompts via gTTS."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile

from gtts import gTTS

logger = logging.getLogger(__name__)


def _fingerprint(text: str, lang: str) -> str:
    return hashlib.sha256(f"{lang}\n{text}".encode("utf-8")).hexdigest()


def _ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to convert audio for Asterisk")
    return ffmpeg


def to_asterisk_wav_sync(in_path: str, out_wav: str) -> None:
    """Convert any audio file (mp3, ogg, m4a, wav…) to PCM s16le 8 kHz mono."""
    os.makedirs(os.path.dirname(out_wav), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_wav = os.path.join(tmp, "out.wav")
        cmd = [
            _ffmpeg(), "-y", "-loglevel", "error",
            "-i", in_path,
            "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le",
            tmp_wav,
        ]
        subprocess.run(cmd, check=True)
        shutil.move(tmp_wav, out_wav)


async def to_asterisk_wav(in_path: str, out_wav: str) -> None:
    await asyncio.to_thread(to_asterisk_wav_sync, in_path, out_wav)


def _generate_sync(text: str, lang: str, out_wav: str) -> None:
    """Synthesize ``text`` to ``out_wav`` (PCM s16le, 8000 Hz, mono)."""
    os.makedirs(os.path.dirname(out_wav), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        mp3_path = os.path.join(tmp, "tts.mp3")
        gTTS(text=text, lang=lang).save(mp3_path)
        to_asterisk_wav_sync(mp3_path, out_wav)
    with open(out_wav + ".sha", "w", encoding="utf-8") as fh:
        fh.write(_fingerprint(text, lang))


async def synthesize(text: str, lang: str, out_wav: str) -> None:
    """Generate a TTS prompt at ``out_wav`` (always regenerates)."""
    await asyncio.to_thread(_generate_sync, text, lang, out_wav)


async def ensure_prompt(text: str, lang: str, sounds_dir: str, sound_name: str) -> str:
    """Make sure the default prompt WAV exists and matches ``text``; return its path."""
    out_wav = os.path.join(sounds_dir, f"{sound_name}.wav")
    fingerprint = _fingerprint(text, lang)

    sha_path = out_wav + ".sha"
    if os.path.exists(out_wav) and os.path.exists(sha_path):
        with open(sha_path, encoding="utf-8") as fh:
            if fh.read().strip() == fingerprint:
                logger.info("TTS prompt up to date: %s", out_wav)
                return out_wav

    logger.info("Generating TTS prompt (%s) -> %s", lang, out_wav)
    await asyncio.to_thread(_generate_sync, text, lang, out_wav)
    return out_wav
