"""Hashing de contraseñas portable usando hashlib.scrypt (sin dependencias).

Formato del hash almacenado:
    scrypt$N$r$p$salt_b64$hash_b64
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


_N = 2 ** 15
_R = 8
_P = 1
_LEN = 32


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=_N, r=_R, p=_P, dklen=_LEN, maxmem=128 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$", 5)
    except Exception:
        return False
    if scheme != "scrypt":
        return False
    try:
        n_i, r_i, p_i = int(n), int(r), int(p)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                 n=n_i, r=r_i, p=p_i, dklen=len(expected),
                                 maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(derived, expected)
    except Exception:
        return False
