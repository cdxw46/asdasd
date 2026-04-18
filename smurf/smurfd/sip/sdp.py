"""SDP parser y generador (RFC 4566, RFC 3264 offer/answer).

Soporta:
    * v=, o=, s=, c=, t=, m=, a= (rtpmap, fmtp, ptime, sendrecv/recvonly/...)
    * Múltiples m= lines, cada una con su propia c= opcional.
    * Atributos a nivel de sesión y a nivel de media.
    * ICE candidates parseo básico (a=candidate:...) y a=rtcp-mux.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class RtpMap:
    payload: int
    encoding: str
    rate: int
    channels: int = 1
    fmtp: str = ""

    def __str__(self) -> str:
        ch = f"/{self.channels}" if self.channels > 1 else ""
        return f"{self.payload} {self.encoding}/{self.rate}{ch}"


@dataclass
class Media:
    type: str = "audio"
    port: int = 0
    proto: str = "RTP/AVP"
    formats: List[int] = field(default_factory=list)
    connection: Optional[str] = None  # IP4 1.2.3.4
    attributes: List[Tuple[str, str]] = field(default_factory=list)
    rtpmaps: Dict[int, RtpMap] = field(default_factory=dict)
    direction: str = "sendrecv"  # sendrecv | sendonly | recvonly | inactive
    ptime: Optional[int] = None
    rtcp_mux: bool = False

    def add_attr(self, key: str, value: str = "") -> None:
        self.attributes.append((key, value))

    def get_attr(self, key: str) -> Optional[str]:
        for k, v in self.attributes:
            if k == key:
                return v
        return None


@dataclass
class SDP:
    version: int = 0
    origin: str = "- 0 0 IN IP4 0.0.0.0"
    session_name: str = "SMURF"
    connection: Optional[str] = None  # "1.2.3.4"
    timing: str = "0 0"
    attributes: List[Tuple[str, str]] = field(default_factory=list)
    media: List[Media] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: bytes | str) -> "SDP":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        sdp = cls()
        cur_media: Optional[Media] = None
        for line in raw.replace("\r\n", "\n").split("\n"):
            if not line or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key == "v":
                sdp.version = int(val or 0)
            elif key == "o":
                sdp.origin = val
            elif key == "s":
                sdp.session_name = val
            elif key == "c":
                parts = val.split()
                if len(parts) >= 3:
                    addr = parts[2].split("/", 1)[0]
                    if cur_media is None:
                        sdp.connection = addr
                    else:
                        cur_media.connection = addr
            elif key == "t":
                sdp.timing = val
            elif key == "m":
                parts = val.split()
                if len(parts) < 4:
                    continue
                m = Media(
                    type=parts[0],
                    port=int(parts[1].split("/", 1)[0]),
                    proto=parts[2],
                    formats=[int(x) for x in parts[3:] if x.isdigit()],
                )
                sdp.media.append(m)
                cur_media = m
            elif key == "a":
                if ":" in val:
                    ak, av = val.split(":", 1)
                else:
                    ak, av = val, ""
                ak = ak.strip()
                av = av.strip()
                target = cur_media if cur_media is not None else sdp
                if cur_media is not None:
                    if ak == "rtpmap":
                        ptype, _, enc = av.partition(" ")
                        try:
                            ptype_i = int(ptype)
                        except ValueError:
                            continue
                        enc_parts = enc.split("/")
                        rate = int(enc_parts[1]) if len(enc_parts) >= 2 else 8000
                        ch = int(enc_parts[2]) if len(enc_parts) >= 3 else 1
                        cur_media.rtpmaps[ptype_i] = RtpMap(
                            payload=ptype_i, encoding=enc_parts[0],
                            rate=rate, channels=ch,
                        )
                    elif ak == "fmtp":
                        ptype, _, params = av.partition(" ")
                        try:
                            ptype_i = int(ptype)
                            if ptype_i in cur_media.rtpmaps:
                                cur_media.rtpmaps[ptype_i].fmtp = params
                        except ValueError:
                            pass
                    elif ak == "ptime":
                        try:
                            cur_media.ptime = int(av)
                        except ValueError:
                            pass
                    elif ak in ("sendrecv", "sendonly", "recvonly", "inactive"):
                        cur_media.direction = ak
                    elif ak == "rtcp-mux":
                        cur_media.rtcp_mux = True
                target.attributes.append((ak, av))
        return sdp

    def serialize(self) -> bytes:
        out: List[str] = [
            f"v={self.version}",
            f"o={self.origin}",
            f"s={self.session_name}",
        ]
        if self.connection:
            out.append(f"c=IN IP4 {self.connection}")
        out.append(f"t={self.timing}")
        for k, v in self.attributes:
            out.append(f"a={k}:{v}" if v else f"a={k}")
        for m in self.media:
            fmts = " ".join(str(f) for f in m.formats) or "0"
            out.append(f"m={m.type} {m.port} {m.proto} {fmts}")
            if m.connection:
                out.append(f"c=IN IP4 {m.connection}")
            for ptype, rm in m.rtpmaps.items():
                out.append(f"a=rtpmap:{rm}")
                if rm.fmtp:
                    out.append(f"a=fmtp:{ptype} {rm.fmtp}")
            if m.ptime:
                out.append(f"a=ptime:{m.ptime}")
            if m.rtcp_mux and not any(k == "rtcp-mux" for k, _ in m.attributes):
                out.append("a=rtcp-mux")
            if not any(k == m.direction for k, _ in m.attributes):
                out.append(f"a={m.direction}")
            for k, v in m.attributes:
                if k in ("rtpmap", "fmtp", "ptime", m.direction, "rtcp-mux"):
                    continue
                out.append(f"a={k}:{v}" if v else f"a={k}")
        return ("\r\n".join(out) + "\r\n").encode("utf-8")

    def first_audio(self) -> Optional[Media]:
        for m in self.media:
            if m.type == "audio":
                return m
        return None


# Códecs estáticos RFC 3551
STATIC_PAYLOADS: Dict[int, RtpMap] = {
    0: RtpMap(0, "PCMU", 8000),
    3: RtpMap(3, "GSM", 8000),
    4: RtpMap(4, "G723", 8000),
    8: RtpMap(8, "PCMA", 8000),
    9: RtpMap(9, "G722", 8000),
    18: RtpMap(18, "G729", 8000),
    101: RtpMap(101, "telephone-event", 8000, fmtp="0-16"),
}


SUPPORTED_CODECS = {"PCMU", "PCMA", "G722", "telephone-event", "opus"}


def build_audio_offer(local_ip: str, port: int,
                      codecs: List[str] = ("PCMU", "PCMA", "telephone-event"),
                      direction: str = "sendrecv",
                      session_id: Optional[int] = None) -> SDP:
    import time as _t
    sid = session_id or int(_t.time())
    sdp = SDP(
        origin=f"smurf {sid} {sid} IN IP4 {local_ip}",
        session_name="SMURF",
        connection=local_ip,
    )
    m = Media(type="audio", port=port, proto="RTP/AVP", formats=[])
    next_dyn = 96
    for codec in codecs:
        cu = codec.upper()
        if cu == "PCMU":
            m.formats.append(0); m.rtpmaps[0] = STATIC_PAYLOADS[0]
        elif cu == "PCMA":
            m.formats.append(8); m.rtpmaps[8] = STATIC_PAYLOADS[8]
        elif cu == "G722":
            m.formats.append(9); m.rtpmaps[9] = STATIC_PAYLOADS[9]
        elif cu == "TELEPHONE-EVENT":
            m.formats.append(101); m.rtpmaps[101] = STATIC_PAYLOADS[101]
        elif cu == "OPUS":
            m.formats.append(next_dyn)
            m.rtpmaps[next_dyn] = RtpMap(next_dyn, "opus", 48000, 2,
                                         fmtp="minptime=10;useinbandfec=1")
            next_dyn += 1
    m.direction = direction
    m.ptime = 20
    sdp.media.append(m)
    return sdp


def negotiate_audio(offer: SDP, local_ip: str, local_port: int,
                    preferred: Tuple[str, ...] = ("PCMU", "PCMA", "G722", "opus", "telephone-event"),
                    ) -> Optional[SDP]:
    """Genera una respuesta SDP a un offer. Devuelve None si no hay códec común."""
    om = offer.first_audio()
    if om is None:
        return None
    chosen: List[Tuple[int, RtpMap]] = []
    seen_encs = set()

    for pt in om.formats:
        rm = om.rtpmaps.get(pt) or STATIC_PAYLOADS.get(pt)
        if not rm:
            continue
        enc = rm.encoding.upper()
        if enc not in {p.upper() for p in preferred}:
            continue
        if enc in seen_encs:
            continue
        seen_encs.add(enc)
        chosen.append((pt, rm))

    audio_chosen = [(pt, rm) for pt, rm in chosen if rm.encoding.upper() != "TELEPHONE-EVENT"]
    if not audio_chosen:
        return None

    audio_chosen.sort(key=lambda x: preferred.index(
        next((p for p in preferred if p.upper() == x[1].encoding.upper()), preferred[0])
    ))
    final = audio_chosen[:1]  # un sólo códec de audio activo
    for pt, rm in chosen:
        if rm.encoding.upper() == "TELEPHONE-EVENT":
            final.append((pt, rm))

    answer = SDP(
        origin=f"smurf {offer.version} {offer.version} IN IP4 {local_ip}",
        session_name="SMURF",
        connection=local_ip,
    )
    m = Media(type="audio", port=local_port, proto=om.proto,
              formats=[pt for pt, _ in final])
    for pt, rm in final:
        m.rtpmaps[pt] = rm
    m.direction = "sendrecv" if om.direction == "sendrecv" else (
        "recvonly" if om.direction == "sendonly" else
        "sendonly" if om.direction == "recvonly" else "inactive"
    )
    m.ptime = om.ptime or 20
    m.rtcp_mux = om.rtcp_mux
    if m.rtcp_mux:
        m.add_attr("rtcp-mux")
    answer.media.append(m)
    return answer
