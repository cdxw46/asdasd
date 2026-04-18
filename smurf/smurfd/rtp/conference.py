"""Conferencias multi-parte con mezcla N-1 (RFC 3550 §2.3).

Cada participante recibe la suma de los demás (no se oye a sí mismo).
La mezcla se realiza a 8 kHz / 16 bit lineal y luego se transcodea al
formato de cada leg (μ-law/A-law). Frame loop: 20 ms.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque, Dict, List, Optional

from ..util.logger import get_logger
from .codecs import decode_to_pcm16, encode_from_pcm16, pcm_mix, samples_per_frame
from .engine import RtpLeg
from .packet import RtpPacket

log = get_logger("rtp.conf")


class ConferenceParticipant:
    def __init__(self, name: str, leg: RtpLeg):
        self.name = name
        self.leg = leg
        self.pcm_queue: Deque[bytes] = deque(maxlen=20)
        leg.on_rtp = self._on_rtp
        self.muted: bool = False

    def _on_rtp(self, pkt: RtpPacket) -> None:
        pcm, _ = decode_to_pcm16(pkt.payload, self.leg.pt)
        if pcm:
            self.pcm_queue.append(pcm)

    def take_frame(self, frame_size_bytes: int) -> bytes:
        if self.muted or not self.pcm_queue:
            return b"\x00" * frame_size_bytes
        chunk = self.pcm_queue.popleft()
        if len(chunk) < frame_size_bytes:
            chunk += b"\x00" * (frame_size_bytes - len(chunk))
        return chunk[:frame_size_bytes]


class ConferenceBridge:
    def __init__(self, conf_id: str, ptime_ms: int = 20):
        self.conf_id = conf_id
        self.ptime_ms = ptime_ms
        self.participants: Dict[str, ConferenceParticipant] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.created_at = time.time()

    def add(self, leg: RtpLeg, name: Optional[str] = None) -> str:
        pid = name or f"p{len(self.participants)+1}"
        self.participants[pid] = ConferenceParticipant(pid, leg)
        log.info("Conf %s: + %s", self.conf_id, pid)
        return pid

    async def remove(self, pid: str) -> None:
        p = self.participants.pop(pid, None)
        if p:
            await p.leg.close()
            log.info("Conf %s: - %s", self.conf_id, pid)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        period = self.ptime_ms / 1000.0
        next_t = time.time()
        while not self._stop.is_set():
            await asyncio.sleep(max(0, next_t - time.time()))
            next_t += period
            if not self.participants:
                continue
            frame_bytes = samples_per_frame(0, self.ptime_ms) * 2  # PCM16
            frames: Dict[str, bytes] = {
                pid: p.take_frame(frame_bytes) for pid, p in self.participants.items()
            }
            for pid, part in self.participants.items():
                others = [f for k, f in frames.items() if k != pid]
                mixed = pcm_mix(others) if others else b"\x00" * frame_bytes
                if len(mixed) < frame_bytes:
                    mixed += b"\x00" * (frame_bytes - len(mixed))
                payload = encode_from_pcm16(mixed[:frame_bytes], part.leg.pt)
                if payload:
                    part.leg.send_pkt(part.leg.pt, payload)

    async def stop(self) -> None:
        self._stop.set()
        for pid in list(self.participants.keys()):
            await self.remove(pid)
        if self._task:
            await asyncio.wait([self._task], timeout=1)
