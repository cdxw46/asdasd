"""Autenticación HTTP Digest para SIP (RFC 3261 §22, RFC 7616, RFC 8760).

Soporta los algoritmos MD5, MD5-sess, SHA-256 y SHA-256-sess, qop=auth y
qop=auth-int. Genera challenges WWW-Authenticate / Proxy-Authenticate y
valida las respuestas de los UAs.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


_HASHERS = {
    "MD5": lambda d: hashlib.md5(d).hexdigest(),
    "MD5-SESS": lambda d: hashlib.md5(d).hexdigest(),
    "SHA-256": lambda d: hashlib.sha256(d).hexdigest(),
    "SHA-256-SESS": lambda d: hashlib.sha256(d).hexdigest(),
    "SHA-512-256": lambda d: hashlib.sha512(d).hexdigest()[:64],
    "SHA-512-256-SESS": lambda d: hashlib.sha512(d).hexdigest()[:64],
}


def _h(algo: str, data: str) -> str:
    return _HASHERS[algo.upper()](data.encode("utf-8"))


def _kd(algo: str, secret: str, data: str) -> str:
    return _h(algo, f"{secret}:{data}")


def parse_auth_header(value: str) -> Tuple[str, Dict[str, str]]:
    """Parsea Authorization/Proxy-Authorization. Devuelve (scheme, params)."""
    value = value.strip()
    sp = value.find(" ")
    if sp == -1:
        return value, {}
    scheme = value[:sp].strip()
    rest = value[sp + 1 :].strip()
    out: Dict[str, str] = {}
    pos = 0
    n = len(rest)
    while pos < n:
        while pos < n and rest[pos] in " \t,":
            pos += 1
        if pos >= n:
            break
        eq = rest.find("=", pos)
        if eq == -1:
            break
        key = rest[pos:eq].strip().lower()
        pos = eq + 1
        while pos < n and rest[pos] in " \t":
            pos += 1
        if pos < n and rest[pos] == '"':
            pos += 1
            buf = []
            while pos < n and rest[pos] != '"':
                if rest[pos] == "\\" and pos + 1 < n:
                    buf.append(rest[pos + 1])
                    pos += 2
                else:
                    buf.append(rest[pos])
                    pos += 1
            value_str = "".join(buf)
            if pos < n:
                pos += 1
        else:
            comma = rest.find(",", pos)
            end = n if comma == -1 else comma
            value_str = rest[pos:end].strip()
            pos = end
        out[key] = value_str
    return scheme, out


def build_challenge(realm: str, algorithm: str = "MD5", qop: str = "auth",
                    stale: bool = False, opaque_secret: str = "smurf",
                    domain: Optional[str] = None) -> str:
    nonce_raw = f"{time.time():.6f}:{secrets.token_hex(16)}"
    nonce_sig = hmac.new(opaque_secret.encode(), nonce_raw.encode(),
                         hashlib.sha256).hexdigest()[:16]
    nonce = f"{nonce_raw}:{nonce_sig}"
    opaque = secrets.token_hex(8)
    parts = [
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'algorithm={algorithm}',
        f'qop="{qop}"',
        f'opaque="{opaque}"',
    ]
    if domain:
        parts.insert(1, f'domain="{domain}"')
    if stale:
        parts.append("stale=true")
    return "Digest " + ", ".join(parts)


def verify_nonce(nonce: str, opaque_secret: str = "smurf",
                 max_age: float = 600.0) -> bool:
    try:
        ts_str, _rand, sig = nonce.rsplit(":", 2)
        rand = nonce.split(":")[1]
        raw = f"{ts_str}:{rand}"
        expect = hmac.new(opaque_secret.encode(), raw.encode(),
                          hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(expect, sig):
            return False
        ts = float(ts_str)
        return (time.time() - ts) <= max_age
    except Exception:
        return False


@dataclass
class DigestCredentials:
    username: str
    realm: str
    password: str  # plaintext o HA1 precomputado si starts with "ha1:"

    def ha1(self, algorithm: str) -> str:
        if self.password.startswith("ha1:"):
            return self.password[4:]
        return _h(algorithm, f"{self.username}:{self.realm}:{self.password}")


def verify_response(method: str, body: bytes, auth_value: str,
                    creds: DigestCredentials,
                    opaque_secret: str = "smurf",
                    nonce_max_age: float = 600.0) -> Tuple[bool, bool]:
    """Devuelve (ok, stale). stale=True implica que se debe re-emitir un challenge."""
    scheme, p = parse_auth_header(auth_value)
    if scheme.lower() != "digest":
        return False, False
    algo = (p.get("algorithm") or "MD5").upper()
    if algo not in _HASHERS:
        return False, False
    if p.get("username", "") != creds.username:
        return False, False
    if p.get("realm", "") != creds.realm:
        return False, False
    nonce = p.get("nonce", "")
    if not verify_nonce(nonce, opaque_secret, nonce_max_age):
        return False, True
    uri = p.get("uri", "")
    qop = p.get("qop", "")
    cnonce = p.get("cnonce", "")
    nc = p.get("nc", "")

    ha1 = creds.ha1(algo)
    if algo.endswith("-SESS"):
        ha1 = _h(algo, f"{ha1}:{nonce}:{cnonce}")

    if qop == "auth-int":
        body_hash = _h(algo, "")
        if body:
            body_hash = _HASHERS[algo](body)
        ha2 = _h(algo, f"{method}:{uri}:{body_hash}")
    else:
        ha2 = _h(algo, f"{method}:{uri}")

    if qop in ("auth", "auth-int"):
        expected = _kd(algo, ha1, f"{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        expected = _kd(algo, ha1, f"{nonce}:{ha2}")

    return hmac.compare_digest(expected, p.get("response", "")), False
