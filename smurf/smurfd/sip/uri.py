"""Parser y serializador de SIP URIs y Name-Addr (RFC 3261 §19.1, §20).

Implementa:
    sip:user:password@host:port;params?headers
    sips:user@host;transport=tls
    "Display Name" <sip:user@host>;tag=...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import quote, unquote


_URI_RE = re.compile(
    r"^(?P<scheme>sips?|tel):"
    r"(?:(?P<user>[^@:;?\s]+)(?::(?P<password>[^@;?\s]*))?@)?"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[^:;?\s]+)"
    r"(?::(?P<port>\d+))?"
    r"(?P<params>(?:;[^?\s]+)*)"
    r"(?:\?(?P<headers>[^\s]+))?$"
)


def _parse_kv_list(s: str, sep: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not s:
        return out
    for part in s.split(sep):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = unquote(v.strip())
        else:
            out[part.strip().lower()] = ""
    return out


@dataclass
class SipURI:
    scheme: str = "sip"
    user: Optional[str] = None
    password: Optional[str] = None
    host: str = ""
    port: Optional[int] = None
    parameters: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: str) -> "SipURI":
        raw = raw.strip()
        m = _URI_RE.match(raw)
        if not m:
            raise ValueError(f"URI SIP inválida: {raw!r}")
        params = _parse_kv_list(m.group("params").lstrip(";"), ";")
        headers = _parse_kv_list(m.group("headers") or "", "&")
        return cls(
            scheme=m.group("scheme"),
            user=unquote(m.group("user")) if m.group("user") else None,
            password=unquote(m.group("password")) if m.group("password") else None,
            host=m.group("host"),
            port=int(m.group("port")) if m.group("port") else None,
            parameters=params,
            headers=headers,
        )

    @property
    def transport(self) -> str:
        return self.parameters.get("transport", "udp" if self.scheme == "sip" else "tls").lower()

    @property
    def aor(self) -> str:
        """Address-of-record canonical: user@host (sin port/params/headers)."""
        u = self.user or ""
        return f"{self.scheme}:{u}@{self.host}".lower()

    def __str__(self) -> str:
        s = f"{self.scheme}:"
        if self.user is not None:
            s += quote(self.user, safe="!$&'()*+,;=:&")
            if self.password is not None:
                s += ":" + quote(self.password, safe="!$&'()*+,;=:&")
            s += "@"
        s += self.host
        if self.port is not None:
            s += f":{self.port}"
        for k, v in self.parameters.items():
            s += ";" + (f"{k}={quote(v, safe='[]')}" if v != "" else k)
        if self.headers:
            s += "?" + "&".join(
                f"{k}={quote(v, safe='')}" for k, v in self.headers.items()
            )
        return s


@dataclass
class NameAddr:
    """Header value tipo From/To/Contact: \"Display\" <uri>;params"""
    display: Optional[str] = None
    uri: SipURI = field(default_factory=SipURI)
    parameters: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: str) -> "NameAddr":
        s = raw.strip()
        display: Optional[str] = None
        if s.startswith('"'):
            end = s.find('"', 1)
            if end == -1:
                raise ValueError(f"Display name sin cerrar: {raw!r}")
            display = s[1:end]
            s = s[end + 1 :].lstrip()
        if "<" in s:
            lt = s.index("<")
            if display is None and lt > 0:
                d = s[:lt].strip()
                display = d if d else None
            gt = s.index(">", lt)
            uri = SipURI.parse(s[lt + 1 : gt])
            tail = s[gt + 1 :]
        else:
            sc_pos = -1
            in_brk = False
            for i, ch in enumerate(s):
                if ch == "[":
                    in_brk = True
                elif ch == "]":
                    in_brk = False
                elif ch == ";" and not in_brk:
                    sc_pos = i
                    break
            if sc_pos >= 0:
                uri = SipURI.parse(s[:sc_pos])
                tail = s[sc_pos:]
            else:
                uri = SipURI.parse(s)
                tail = ""
        params = _parse_kv_list(tail.lstrip(";"), ";") if tail else {}
        return cls(display=display, uri=uri, parameters=params)

    def __str__(self) -> str:
        out = ""
        if self.display:
            if any(c in self.display for c in " \t\"<>"):
                out += f"\"{self.display}\" "
            else:
                out += f"{self.display} "
        out += f"<{self.uri}>"
        for k, v in self.parameters.items():
            out += ";" + (f"{k}={v}" if v != "" else k)
        return out
