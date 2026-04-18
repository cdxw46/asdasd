"""Construcción de Authorization para REGISTER/INVITE saliente hacia trunks
SIP que requieren digest auth (RFC 7616 / 3261).

Toma un challenge WWW-Authenticate o Proxy-Authenticate y devuelve la
cabecera Authorization (o Proxy-Authorization) lista para enviar en el
siguiente intento.
"""
from __future__ import annotations

import hashlib
import secrets

from ..sip.auth import _HASHERS, parse_auth_header


def _h(algo: str, data: str) -> str:
    return _HASHERS[algo.upper()](data.encode("utf-8"))


def build_authorization(challenge: str, username: str, password: str,
                        method: str, uri: str, body: bytes = b"") -> str:
    scheme, p = parse_auth_header(challenge)
    realm = p.get("realm", "")
    nonce = p.get("nonce", "")
    qop_list = [q.strip() for q in p.get("qop", "").split(",") if q.strip()]
    qop = "auth" if "auth" in qop_list else (qop_list[0] if qop_list else "")
    algo = (p.get("algorithm") or "MD5").upper()
    if algo not in _HASHERS:
        algo = "MD5"
    cnonce = secrets.token_hex(8)
    nc = "00000001"

    ha1 = _h(algo, f"{username}:{realm}:{password}")
    if algo.endswith("-SESS"):
        ha1 = _h(algo, f"{ha1}:{nonce}:{cnonce}")

    if qop == "auth-int":
        body_hash = _h(algo, "") if not body else _HASHERS[algo](body)
        ha2 = _h(algo, f"{method}:{uri}:{body_hash}")
    else:
        ha2 = _h(algo, f"{method}:{uri}")

    if qop in ("auth", "auth-int"):
        response = _h(algo, f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        response = _h(algo, f"{ha1}:{nonce}:{ha2}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'algorithm={algo}',
        f'response="{response}"',
    ]
    if "opaque" in p:
        parts.append(f'opaque="{p["opaque"]}"')
    if qop:
        parts.append(f'qop={qop}')
        parts.append(f'nc={nc}')
        parts.append(f'cnonce="{cnonce}"')
    return "Digest " + ", ".join(parts)
