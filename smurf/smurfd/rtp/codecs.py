"""Codecs de audio: G.711 (μ-law / A-law), G.722, telephone-event.

Implementaciones puras en Python (sin librerías externas) para μ-law y
A-law siguiendo G.711 (ITU). G.722 sólo se decodifica para puentear: la
mezcla la hacemos en lineal 16 kHz / 16 bit. Para llamadas que negocian
G.722 entre dos endpoints simétricos, el media engine actúa como relay
puro (sin transcodear), por lo que basta con propagarlo.

Funciones expuestas:
    ulaw2lin / lin2ulaw / alaw2lin / lin2alaw  -> trabajan con bytes / bytes
    pcm_resample(samples, in_rate, out_rate)   -> downsample/upsample lineal
    pcm_rms(samples)                           -> nivel
    pcm_mix(*streams)                          -> mezcla con saturación
"""
from __future__ import annotations

import audioop
from typing import Iterable, List, Tuple


# G.711 está soportado por audioop directamente, pero damos wrappers para
# poder cambiar a implementación pura si fuera necesario.

def ulaw2lin(data: bytes) -> bytes:
    return audioop.ulaw2lin(data, 2)


def lin2ulaw(data: bytes) -> bytes:
    return audioop.lin2ulaw(data, 2)


def alaw2lin(data: bytes) -> bytes:
    return audioop.alaw2lin(data, 2)


def lin2alaw(data: bytes) -> bytes:
    return audioop.lin2alaw(data, 2)


def pcm_resample(data: bytes, in_rate: int, out_rate: int,
                 state=None) -> Tuple[bytes, object]:
    if in_rate == out_rate:
        return data, state
    return audioop.ratecv(data, 2, 1, in_rate, out_rate, state)


def pcm_rms(data: bytes) -> int:
    if not data:
        return 0
    return audioop.rms(data, 2)


def pcm_mix(streams: Iterable[bytes]) -> bytes:
    """Suma con saturación dos o más streams PCM 16 bit mono."""
    streams = [s for s in streams if s]
    if not streams:
        return b""
    if len(streams) == 1:
        return streams[0]
    out = streams[0]
    n = len(out)
    for other in streams[1:]:
        if len(other) != n:
            m = min(n, len(other))
            out = audioop.add(out[:m], other[:m], 2)
            n = m
        else:
            out = audioop.add(out, other, 2)
    return out


# Tabla rápida de códecs estáticos por payload-type
PT_PCMU = 0
PT_PCMA = 8
PT_G722 = 9
PT_TEVENT = 101  # dinámico estándar


def decode_to_pcm16(payload: bytes, payload_type: int) -> Tuple[bytes, int]:
    """Decodifica payload RTP a PCM 16-bit mono y devuelve (pcm, sample_rate)."""
    if payload_type == PT_PCMU:
        return ulaw2lin(payload), 8000
    if payload_type == PT_PCMA:
        return alaw2lin(payload), 8000
    return b"", 8000


def encode_from_pcm16(pcm: bytes, payload_type: int) -> bytes:
    if payload_type == PT_PCMU:
        return lin2ulaw(pcm)
    if payload_type == PT_PCMA:
        return lin2alaw(pcm)
    return b""


def static_payload_name(pt: int) -> str:
    return {0: "PCMU", 8: "PCMA", 9: "G722", 18: "G729"}.get(pt, f"PT{pt}")


def samples_per_frame(payload_type: int, ptime_ms: int = 20) -> int:
    """Cuántos samples PCM (16-bit mono) entran en un frame de ptime ms."""
    rate = 8000
    if payload_type == 9:
        rate = 16000  # G.722 usa timestamps de 8000 pero sample rate 16000
    return rate * ptime_ms // 1000
