"""Mensaje SIP: parser y serializador (RFC 3261 §7, §20, §25).

Soporta:
    * Request line / Status line.
    * Cabeceras compactas (RFC 3261 §7.3.3).
    * Múltiples valores por cabecera (lista en orden).
    * Cuerpo binario arbitrario.
    * Reconstrucción byte-exacta para firmar/loguear.
    * Helpers para Via, CSeq, Contact, Authorization, Authenticate, Route, Record-Route.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .uri import NameAddr, SipURI


COMPACT = {
    "i": "Call-ID",
    "m": "Contact",
    "e": "Content-Encoding",
    "l": "Content-Length",
    "c": "Content-Type",
    "f": "From",
    "s": "Subject",
    "k": "Supported",
    "t": "To",
    "v": "Via",
    "r": "Refer-To",
    "b": "Referred-By",
    "u": "Allow-Events",
    "o": "Event",
    "x": "Session-Expires",
    "y": "Identity",
    "n": "Identity-Info",
    "d": "Request-Disposition",
    "j": "Reject-Contact",
    "a": "Accept-Contact",
}

REASON_PHRASES = {
    100: "Trying", 180: "Ringing", 181: "Call Is Being Forwarded",
    182: "Queued", 183: "Session Progress",
    200: "OK", 202: "Accepted", 204: "No Notification",
    300: "Multiple Choices", 301: "Moved Permanently", 302: "Moved Temporarily",
    305: "Use Proxy", 380: "Alternative Service",
    400: "Bad Request", 401: "Unauthorized", 402: "Payment Required",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    406: "Not Acceptable", 407: "Proxy Authentication Required",
    408: "Request Timeout", 410: "Gone", 412: "Conditional Request Failed",
    413: "Request Entity Too Large", 414: "Request-URI Too Long",
    415: "Unsupported Media Type", 416: "Unsupported URI Scheme",
    420: "Bad Extension", 421: "Extension Required",
    422: "Session Interval Too Small", 423: "Interval Too Brief",
    480: "Temporarily Unavailable", 481: "Call/Transaction Does Not Exist",
    482: "Loop Detected", 483: "Too Many Hops", 484: "Address Incomplete",
    485: "Ambiguous", 486: "Busy Here", 487: "Request Terminated",
    488: "Not Acceptable Here", 489: "Bad Event", 491: "Request Pending",
    493: "Undecipherable", 494: "Security Agreement Required",
    500: "Server Internal Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Server Time-out",
    505: "Version Not Supported", 513: "Message Too Large",
    600: "Busy Everywhere", 603: "Decline", 604: "Does Not Exist Anywhere",
    606: "Not Acceptable",
}


def reason_for(code: int) -> str:
    return REASON_PHRASES.get(code, "Unknown")


_REQUEST_LINE = re.compile(r"^([A-Z]+)\s+(\S+)\s+SIP/(\d+\.\d+)$")
_STATUS_LINE = re.compile(r"^SIP/(\d+\.\d+)\s+(\d{3})\s*(.*)$")


def _norm_header(name: str) -> str:
    n = name.strip()
    low = n.lower()
    if low in COMPACT:
        return COMPACT[low]
    return "-".join(p.capitalize() for p in low.split("-"))


@dataclass
class SipMessage:
    is_request: bool = False
    method: Optional[str] = None
    request_uri: Optional[SipURI] = None
    status_code: Optional[int] = None
    reason_phrase: Optional[str] = None
    version: str = "2.0"
    headers: Dict[str, List[str]] = field(default_factory=dict)
    body: bytes = b""

    @classmethod
    def parse(cls, data: bytes) -> "SipMessage":
        if isinstance(data, str):
            data = data.encode("utf-8")
        sep = data.find(b"\r\n\r\n")
        if sep == -1:
            sep_n = data.find(b"\n\n")
            if sep_n == -1:
                raise ValueError("Mensaje SIP sin separador cabecera/cuerpo")
            head = data[:sep_n].decode("utf-8", errors="replace").replace("\r\n", "\n")
            body = data[sep_n + 2 :]
        else:
            head = data[:sep].decode("utf-8", errors="replace").replace("\r\n", "\n")
            body = data[sep + 4 :]

        lines = head.split("\n")
        if not lines:
            raise ValueError("Mensaje SIP vacío")

        first = lines[0].strip()
        msg = cls(body=body)

        m = _REQUEST_LINE.match(first)
        if m:
            msg.is_request = True
            msg.method = m.group(1).upper()
            msg.request_uri = SipURI.parse(m.group(2))
            msg.version = m.group(3)
        else:
            m2 = _STATUS_LINE.match(first)
            if not m2:
                raise ValueError(f"Línea de inicio inválida: {first!r}")
            msg.is_request = False
            msg.version = m2.group(1)
            msg.status_code = int(m2.group(2))
            msg.reason_phrase = m2.group(3) or reason_for(int(m2.group(2)))

        i = 1
        unfolded: List[str] = []
        while i < len(lines):
            ln = lines[i]
            if ln == "":
                i += 1
                continue
            if ln.startswith((" ", "\t")) and unfolded:
                unfolded[-1] += " " + ln.strip()
            else:
                unfolded.append(ln)
            i += 1

        for ln in unfolded:
            if ":" not in ln:
                continue
            name, value = ln.split(":", 1)
            name = _norm_header(name)
            value = value.strip()
            if name in ("Via", "Route", "Record-Route", "Contact", "Path", "Service-Route"):
                parts = _split_top_level_commas(value)
                msg.headers.setdefault(name, []).extend(p.strip() for p in parts)
            else:
                msg.headers.setdefault(name, []).append(value)
        return msg

    def serialize(self) -> bytes:
        if self.is_request:
            assert self.method and self.request_uri is not None
            line = f"{self.method} {self.request_uri} SIP/{self.version}\r\n"
        else:
            phrase = self.reason_phrase or reason_for(self.status_code or 0)
            line = f"SIP/{self.version} {self.status_code} {phrase}\r\n"
        out = [line]
        for name, values in self.headers.items():
            for v in values:
                out.append(f"{name}: {v}\r\n")
        out.append("\r\n")
        head = "".join(out).encode("utf-8")
        return head + (self.body or b"")

    def __bytes__(self) -> bytes:
        return self.serialize()

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        v = self.headers.get(_norm_header(name))
        return v[0] if v else default

    def get_all(self, name: str) -> List[str]:
        return list(self.headers.get(_norm_header(name), []))

    def set(self, name: str, value: str) -> None:
        self.headers[_norm_header(name)] = [value]

    def add(self, name: str, value: str) -> None:
        self.headers.setdefault(_norm_header(name), []).append(value)

    def remove(self, name: str) -> None:
        self.headers.pop(_norm_header(name), None)

    @property
    def call_id(self) -> str:
        return self.get("Call-ID", "") or ""

    @property
    def cseq(self) -> Tuple[int, str]:
        v = self.get("CSeq", "0 INVALID") or "0 INVALID"
        n, _, m = v.strip().partition(" ")
        try:
            return int(n), m.strip().upper()
        except ValueError:
            return 0, "INVALID"

    @property
    def from_header(self) -> NameAddr:
        v = self.get("From", "")
        return NameAddr.parse(v) if v else NameAddr()

    @property
    def to_header(self) -> NameAddr:
        v = self.get("To", "")
        return NameAddr.parse(v) if v else NameAddr()

    def via_top(self) -> Optional["Via"]:
        vs = self.get_all("Via")
        if not vs:
            return None
        return Via.parse(vs[0])

    def vias(self) -> List["Via"]:
        return [Via.parse(v) for v in self.get_all("Via")]

    def contacts(self) -> List[NameAddr]:
        out: List[NameAddr] = []
        for v in self.get_all("Contact"):
            if v.strip() == "*":
                na = NameAddr()
                na.parameters["*"] = ""
                out.append(na)
            else:
                out.append(NameAddr.parse(v))
        return out

    def content_length(self) -> int:
        v = self.get("Content-Length")
        if v is None:
            return len(self.body or b"")
        try:
            return int(v.strip())
        except ValueError:
            return 0


def _split_top_level_commas(s: str) -> List[str]:
    """Divide por comas pero respetando comillas y ángulos (Via, Contact, ...)."""
    parts: List[str] = []
    buf = []
    in_str = False
    in_ang = 0
    in_brk = 0
    for ch in s:
        if ch == '"':
            in_str = not in_str
            buf.append(ch)
        elif ch == "<" and not in_str:
            in_ang += 1
            buf.append(ch)
        elif ch == ">" and not in_str:
            in_ang = max(0, in_ang - 1)
            buf.append(ch)
        elif ch == "[" and not in_str:
            in_brk += 1
            buf.append(ch)
        elif ch == "]" and not in_str:
            in_brk = max(0, in_brk - 1)
            buf.append(ch)
        elif ch == "," and not in_str and in_ang == 0 and in_brk == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


_VIA_RE = re.compile(
    r"^SIP/(?P<ver>\d+\.\d+)/(?P<proto>[A-Za-z0-9]+)\s+"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[^\s:;]+)(?::(?P<port>\d+))?"
    r"(?P<params>(?:;[^,\s]+)*)\s*$"
)


@dataclass
class Via:
    version: str = "2.0"
    transport: str = "UDP"
    host: str = ""
    port: Optional[int] = None
    parameters: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: str) -> "Via":
        m = _VIA_RE.match(raw.strip())
        if not m:
            raise ValueError(f"Via inválida: {raw!r}")
        params: Dict[str, str] = {}
        for part in (m.group("params") or "").split(";"):
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip().lower()] = v.strip()
            else:
                params[part.strip().lower()] = ""
        return cls(
            version=m.group("ver"),
            transport=m.group("proto").upper(),
            host=m.group("host"),
            port=int(m.group("port")) if m.group("port") else None,
            parameters=params,
        )

    @property
    def branch(self) -> str:
        return self.parameters.get("branch", "")

    @property
    def received(self) -> Optional[str]:
        return self.parameters.get("received")

    @property
    def rport(self) -> Optional[str]:
        return self.parameters.get("rport")

    def __str__(self) -> str:
        s = f"SIP/{self.version}/{self.transport} {self.host}"
        if self.port is not None:
            s += f":{self.port}"
        for k, v in self.parameters.items():
            s += ";" + (f"{k}={v}" if v != "" else k)
        return s


def make_response(req: SipMessage, code: int, reason: Optional[str] = None,
                  to_tag: Optional[str] = None, body: bytes = b"",
                  content_type: Optional[str] = None) -> SipMessage:
    """Construye una respuesta a una request siguiendo RFC 3261 §8.2.6."""
    resp = SipMessage(
        is_request=False,
        status_code=code,
        reason_phrase=reason or reason_for(code),
        body=body,
    )
    for v in req.get_all("Via"):
        resp.add("Via", v)
    for h in ("From", "Call-ID", "CSeq"):
        v = req.get(h)
        if v is not None:
            resp.set(h, v)
    to = req.get("To", "")
    if to_tag and "tag=" not in to.lower():
        to = to + f";tag={to_tag}"
    resp.set("To", to)
    for rr in req.get_all("Record-Route"):
        resp.add("Record-Route", rr)
    if body:
        resp.set("Content-Length", str(len(body)))
        if content_type:
            resp.set("Content-Type", content_type)
    else:
        resp.set("Content-Length", "0")
    return resp
