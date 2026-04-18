"""Wrapper async sobre SQLite para SMURF.

Singleton accesible vía `get_database()` que lee la ruta del fichero de
config. Inicializa el esquema, crea el usuario admin por defecto y la
extensión 1000 de prueba si no existe ninguna.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from typing import Any, Dict, Iterable, List, Optional, Sequence

import aiosqlite

from ..util.config import SmurfConfig, load_config
from ..util.logger import get_logger
from ..util.passwords import hash_password

log = get_logger("db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def _ha1(user: str, realm: str, password: str, algo: str = "MD5") -> str:
    raw = f"{user}:{realm}:{password}".encode("utf-8")
    if algo.upper() == "MD5":
        return hashlib.md5(raw).hexdigest()
    if algo.upper() == "SHA-256":
        return hashlib.sha256(raw).hexdigest()
    raise ValueError(algo)


class Database:
    def __init__(self, path: str, realm: str = "smurf.local"):
        self.path = path
        self.realm = realm
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            await self._db.executescript(fh.read())
        await self._db.commit()
        await self._bootstrap()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("DB no abierta")
        return self._db

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        async with self._lock:
            await self.conn.executemany(sql, rows)
            await self.conn.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        r = await self.fetchone("SELECT v FROM settings_kv WHERE k=?", (key,))
        return r["v"] if r else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings_kv(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=strftime('%s','now')",
            (key, value),
        )

    async def _bootstrap(self) -> None:
        n = await self.fetchone("SELECT COUNT(*) AS c FROM users")
        if n and n["c"] == 0:
            pwd = "smurf-admin"
            await self.execute(
                "INSERT INTO users(username,password_hash,role,email) VALUES(?,?,?,?)",
                ("admin", hash_password(pwd), "superadmin", "admin@smurf.local"),
            )
            log.warning("Usuario admin creado: usuario='admin' password='%s'  ¡CÁMBIALO!", pwd)
            await self.set_setting("first_run", "1")
            await self.set_setting("default_admin_password", pwd)

        n = await self.fetchone("SELECT COUNT(*) AS c FROM extensions")
        if n and n["c"] == 0:
            for num in ("1000", "1001"):
                pwd = secrets.token_urlsafe(10)
                await self.execute(
                    "INSERT INTO extensions(number,display_name,sip_password,ha1_md5,ha1_sha256,email,voicemail_pin) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (num, f"Extension {num}", pwd,
                     _ha1(num, self.realm, pwd, "MD5"),
                     _ha1(num, self.realm, pwd, "SHA-256"),
                     None, "1234"),
                )
                log.warning("Extensión SIP creada: %s / %s", num, pwd)

        n = await self.fetchone("SELECT COUNT(*) AS c FROM dial_plan")
        if n and n["c"] == 0:
            await self.executemany(
                "INSERT INTO dial_plan(name,direction,pattern,target_type,target_value,priority) VALUES(?,?,?,?,?,?)",
                [
                    ("Internas 4 dígitos", "internal", r"^[12345]\d{3}$", "extension", r"\g<0>", 10),
                    ("Salida internacional", "outbound", r"^\+?\d{6,16}$", "trunk", "default", 100),
                    ("Buzón propio", "internal", r"^\*97$", "voicemail", "self", 5),
                    ("Sala conf 8000", "internal", r"^8000$", "conference", "8000", 5),
                ],
            )

    async def update_extension_password(self, number: str, password: str) -> None:
        await self.execute(
            "UPDATE extensions SET sip_password=?, ha1_md5=?, ha1_sha256=?, updated_at=strftime('%s','now') WHERE number=?",
            (password, _ha1(number, self.realm, password, "MD5"),
             _ha1(number, self.realm, password, "SHA-256"), number),
        )


_INSTANCE: Optional[Database] = None


async def get_database(cfg: Optional[SmurfConfig] = None) -> Database:
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    cfg = cfg or load_config()
    db = Database(cfg.storage.db_path, cfg.sip.realm)
    await db.open()
    _INSTANCE = db
    return db
