"""Parser y serializador de paquetes RTP y RTCP (RFC 3550 §5, §6).

RTP header (12 bytes mínimo):
     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |V=2|P|X|  CC   |M|     PT      |       sequence number         |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                           timestamp                           |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |           synchronization source (SSRC) identifier            |
    +=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+
    |            contributing source (CSRC) identifiers             |
    |                             ....                              |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


RTP_VERSION = 2


@dataclass
class RtpPacket:
    version: int = RTP_VERSION
    padding: bool = False
    extension: bool = False
    marker: bool = False
    payload_type: int = 0
    sequence: int = 0
    timestamp: int = 0
    ssrc: int = 0
    csrcs: List[int] = field(default_factory=list)
    ext_id: int = 0
    ext_data: bytes = b""
    payload: bytes = b""

    def serialize(self) -> bytes:
        cc = len(self.csrcs) & 0x0F
        b0 = (self.version & 0x3) << 6
        if self.padding: b0 |= 1 << 5
        if self.extension or self.ext_data: b0 |= 1 << 4
        b0 |= cc
        b1 = (1 << 7 if self.marker else 0) | (self.payload_type & 0x7F)
        head = struct.pack("!BBHII", b0, b1, self.sequence & 0xFFFF,
                           self.timestamp & 0xFFFFFFFF, self.ssrc & 0xFFFFFFFF)
        for c in self.csrcs:
            head += struct.pack("!I", c & 0xFFFFFFFF)
        if self.extension or self.ext_data:
            ext = self.ext_data
            pad = (-len(ext)) % 4
            ext += b"\x00" * pad
            head += struct.pack("!HH", self.ext_id & 0xFFFF, len(ext) // 4)
            head += ext
        return head + self.payload

    @classmethod
    def parse(cls, data: bytes) -> "RtpPacket":
        if len(data) < 12:
            raise ValueError("RTP demasiado corto")
        b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
        version = (b0 >> 6) & 0x3
        padding = bool((b0 >> 5) & 1)
        ext = bool((b0 >> 4) & 1)
        cc = b0 & 0x0F
        marker = bool((b1 >> 7) & 1)
        pt = b1 & 0x7F
        off = 12
        csrcs: List[int] = []
        for _ in range(cc):
            (c,) = struct.unpack("!I", data[off:off + 4]); off += 4
            csrcs.append(c)
        ext_id = 0
        ext_data = b""
        if ext:
            ext_id, ext_len = struct.unpack("!HH", data[off:off + 4]); off += 4
            ext_data = data[off:off + ext_len * 4]
            off += ext_len * 4
        payload = data[off:]
        if padding and payload:
            pad_len = payload[-1]
            payload = payload[:-pad_len]
        return cls(version=version, padding=False, extension=bool(ext_data),
                   marker=marker, payload_type=pt, sequence=seq, timestamp=ts,
                   ssrc=ssrc, csrcs=csrcs, ext_id=ext_id, ext_data=ext_data,
                   payload=payload)


# ===================== DTMF (RFC 4733) =====================

@dataclass
class DtmfEvent:
    event: int      # 0-15 (0-9, *=10, #=11, A-D=12-15)
    end: bool
    volume: int     # 0-63 (negativo en dBm0)
    duration: int   # samples

    EVENT_CHARS = "0123456789*#ABCD"

    @property
    def char(self) -> str:
        return self.EVENT_CHARS[self.event] if 0 <= self.event < 16 else "?"

    @classmethod
    def parse(cls, payload: bytes) -> Optional["DtmfEvent"]:
        if len(payload) < 4:
            return None
        e, eR, dur = struct.unpack("!BBH", payload[:4])
        end = bool((eR >> 7) & 1)
        vol = eR & 0x3F
        return cls(event=e & 0x0F, end=end, volume=vol, duration=dur)


# ===================== RTCP =====================

# Tipos
RTCP_SR = 200
RTCP_RR = 201
RTCP_SDES = 202
RTCP_BYE = 203
RTCP_APP = 204


@dataclass
class RtcpReportBlock:
    ssrc: int
    fraction_lost: int
    cumulative_lost: int
    highest_seq: int
    jitter: int
    lsr: int
    dlsr: int


@dataclass
class RtcpSR:
    ssrc: int
    ntp_msw: int
    ntp_lsw: int
    rtp_ts: int
    pkt_count: int
    octet_count: int
    reports: List[RtcpReportBlock] = field(default_factory=list)


def parse_rtcp_compound(data: bytes) -> List[Tuple[int, bytes]]:
    """Devuelve lista de (packet_type, raw_packet_bytes)."""
    out: List[Tuple[int, bytes]] = []
    off = 0
    while off + 4 <= len(data):
        b0 = data[off]
        if (b0 >> 6) & 0x3 != 2:
            break
        pt = data[off + 1]
        length, = struct.unpack("!H", data[off + 2:off + 4])
        size = (length + 1) * 4
        if off + size > len(data):
            break
        out.append((pt, data[off:off + size]))
        off += size
    return out


def build_rtcp_sr(sr: RtcpSR) -> bytes:
    rc = len(sr.reports)
    head = bytes([(2 << 6) | rc, RTCP_SR])
    body = struct.pack("!IIIIII",
                       sr.ssrc, sr.ntp_msw, sr.ntp_lsw, sr.rtp_ts,
                       sr.pkt_count, sr.octet_count)
    for r in sr.reports:
        body += struct.pack("!IBBBBIII", r.ssrc,
                            r.fraction_lost & 0xFF,
                            (r.cumulative_lost >> 16) & 0xFF,
                            (r.cumulative_lost >> 8) & 0xFF,
                            r.cumulative_lost & 0xFF,
                            r.highest_seq, r.jitter, r.lsr) \
                + struct.pack("!I", r.dlsr)
    length = (len(body) + 4) // 4 - 1
    return head + struct.pack("!H", length) + body


def build_rtcp_bye(ssrc: int, reason: str = "") -> bytes:
    rc = 1
    body = struct.pack("!I", ssrc)
    if reason:
        rb = reason.encode("utf-8")[:255]
        body += bytes([len(rb)]) + rb
        pad = (-len(body)) % 4
        body += b"\x00" * pad
    head = bytes([(2 << 6) | rc, RTCP_BYE])
    length = (len(body) + 4) // 4 - 1
    return head + struct.pack("!H", length) + body
