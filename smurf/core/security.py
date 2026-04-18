"""Security helpers: digest auth, JWT and passwords."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from passlib.context import CryptContext


# Use pbkdf2_sha256 to avoid runtime backend incompatibilities on some distros.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def make_nonce() -> str:
    return uuid.uuid4().hex


def digest_ha1(username: str, realm: str, password: str, algorithm: str = "MD5") -> str:
    raw = f"{username}:{realm}:{password}".encode()
    algo = algorithm.upper()
    if algo == "MD5":
        return hashlib.md5(raw).hexdigest()
    if algo == "SHA-256":
        return hashlib.sha256(raw).hexdigest()
    raise ValueError(f"Unsupported digest algorithm: {algorithm}")


def digest_ha2(method: str, uri: str, algorithm: str = "MD5") -> str:
    raw = f"{method}:{uri}".encode()
    algo = algorithm.upper()
    if algo == "MD5":
        return hashlib.md5(raw).hexdigest()
    if algo == "SHA-256":
        return hashlib.sha256(raw).hexdigest()
    raise ValueError(f"Unsupported digest algorithm: {algorithm}")


def digest_response(
    ha1: str,
    nonce: str,
    nc: str,
    cnonce: str,
    qop: str,
    ha2: str,
    algorithm: str = "MD5",
) -> str:
    raw = f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
    algo = algorithm.upper()
    if algo == "MD5":
        return hashlib.md5(raw).hexdigest()
    if algo == "SHA-256":
        return hashlib.sha256(raw).hexdigest()
    raise ValueError(f"Unsupported digest algorithm: {algorithm}")


def secure_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def create_jwt(subject: str, secret: str, expires_seconds: int, extra: dict[str, Any] | None = None) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + expires_seconds,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm="HS256")


@dataclass(slots=True)
class JwtPayload:
    subject: str
    issued_at: int
    expires_at: int
    claims: dict[str, Any]


def decode_jwt(token: str, secret: str) -> JwtPayload:
    payload = jwt.decode(token, secret, algorithms=["HS256"])
    return JwtPayload(
        subject=payload.get("sub", ""),
        issued_at=int(payload.get("iat", 0)),
        expires_at=int(payload.get("exp", 0)),
        claims=payload,
    )

