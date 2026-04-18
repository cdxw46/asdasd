"""Autenticación de la API y panel: JWT + opcional TOTP."""
import time
from typing import Optional

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status

from ..db import Database, get_database
from ..util.config import SmurfConfig, load_config
from ..util.passwords import verify_password


ALGO = "HS256"


def make_token(secret: str, user_id: int, username: str, role: str,
               hours: int = 12) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time() + hours * 3600),
    }
    return jwt.encode(payload, secret, algorithm=ALGO)


def decode_token(secret: str, token: str) -> dict:
    return jwt.decode(token, secret, algorithms=[ALGO])


async def authenticate_user(db: Database, username: str, password: str) -> Optional[dict]:
    row = await db.fetchone(
        "SELECT * FROM users WHERE username=? AND enabled=1", (username,)
    )
    if not row:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return row


class JwtAuth:
    def __init__(self, cfg: SmurfConfig):
        self.cfg = cfg

    async def __call__(self,
                       authorization: Optional[str] = Header(None),
                       smurf_token: Optional[str] = Cookie(None, alias="smurf_token")) -> dict:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
        elif smurf_token:
            token = smurf_token
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="No autenticado",
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            payload = decode_token(self.cfg.web.jwt_secret, token)
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail=f"Token inválido: {exc}",
                                headers={"WWW-Authenticate": "Bearer"})
        return payload


def require_role(*roles: str):
    async def _dep(user: dict = Depends(JwtAuth(load_config()))) -> dict:
        if roles and user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Permisos insuficientes")
        return user
    return _dep
