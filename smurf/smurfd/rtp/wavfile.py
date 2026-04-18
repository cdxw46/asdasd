"""Lectura/escritura de WAV PCM 16-bit mono usando sólo la stdlib.

Reproductor de audio asíncrono sobre un RtpLeg para anuncios, IVR y MOH.
"""
from __future__ import annotations

import asyncio
import os
import struct
import wave
from typing import Optional

from .codecs import encode_from_pcm16, pcm_resample, samples_per_frame
from .engine import RtpLeg


def load_wav_pcm16(path: str, target_rate: int = 8000) -> bytes:
    """Carga un WAV mono y devuelve PCM 16-bit a target_rate Hz."""
    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"WAV {path}: sample width {sw} != 2")
    if ch == 2:
        out = bytearray()
        for i in range(0, len(frames), 4):
            l = struct.unpack_from("<h", frames, i)[0]
            r = struct.unpack_from("<h", frames, i + 2)[0]
            mono = max(-32768, min(32767, (l + r) // 2))
            out += struct.pack("<h", mono)
        frames = bytes(out)
    if sr != target_rate:
        frames, _ = pcm_resample(frames, sr, target_rate)
    return frames


def save_wav_pcm16(path: str, pcm: bytes, sample_rate: int = 8000) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


class AudioPlayer:
    """Reproduce un buffer PCM en un leg, frame a frame."""
    def __init__(self, leg: RtpLeg, pcm: bytes, ptime_ms: int = 20,
                 loop: bool = False):
        self.leg = leg
        self.pcm = pcm
        self.ptime_ms = ptime_ms
        self.loop = loop
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> "AudioPlayer":
        self._task = asyncio.create_task(self._run())
        return self

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except Exception:
                pass

    async def wait(self) -> None:
        if self._task:
            await self._task

    async def _run(self) -> None:
        bytes_per_frame = samples_per_frame(self.leg.pt, self.ptime_ms) * 2
        period = self.ptime_ms / 1000.0
        offset = 0
        next_t = asyncio.get_event_loop().time()
        first = True
        while not self._stop.is_set():
            chunk = self.pcm[offset:offset + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                if self.loop:
                    chunk += self.pcm[: bytes_per_frame - len(chunk)]
                    offset = bytes_per_frame - len(chunk)
                else:
                    chunk += b"\x00" * (bytes_per_frame - len(chunk))
                    payload = encode_from_pcm16(chunk, self.leg.pt)
                    if payload:
                        self.leg.send_pkt(self.leg.pt, payload, marker=first)
                    return
            else:
                offset += bytes_per_frame
            payload = encode_from_pcm16(chunk, self.leg.pt)
            if payload:
                self.leg.send_pkt(self.leg.pt, payload, marker=first)
            first = False
            next_t += period
            await asyncio.sleep(max(0, next_t - asyncio.get_event_loop().time()))
