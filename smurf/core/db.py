"""SQLite-backed persistence layer for SMURF services."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .logging_utils import get_logger
from .security import hash_password

LOGGER = get_logger("core.db")


def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> Dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class Database:
    """SQLite wrapper with PBX-oriented helper methods."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _dict_factory
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self.conn() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS extensions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extension TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL,
                    auth_username TEXT UNIQUE NOT NULL,
                    auth_password TEXT NOT NULL,
                    voicemail_pin TEXT DEFAULT '1234',
                    max_calls INTEGER DEFAULT 3,
                    role TEXT DEFAULT 'user',
                    outbound_cid TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS registrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extension TEXT NOT NULL,
                    contact_uri TEXT NOT NULL,
                    source_ip TEXT NOT NULL,
                    source_port INTEGER NOT NULL,
                    transport TEXT NOT NULL,
                    user_agent TEXT DEFAULT '',
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_registrations_ext ON registrations(extension);
                CREATE INDEX IF NOT EXISTS idx_registrations_exp ON registrations(expires_at);

                CREATE TABLE IF NOT EXISTS trunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5060,
                    transport TEXT NOT NULL DEFAULT 'udp',
                    auth_type TEXT NOT NULL DEFAULT 'credentials',
                    username TEXT DEFAULT '',
                    password TEXT DEFAULT '',
                    outbound_prefix TEXT DEFAULT '',
                    priority INTEGER DEFAULT 100,
                    active INTEGER DEFAULT 1,
                    max_channels INTEGER DEFAULT 30
                );

                CREATE TABLE IF NOT EXISTS dialplan_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS did_routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    did_number TEXT UNIQUE NOT NULL,
                    route_type TEXT NOT NULL,
                    route_target TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ring_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_number TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT 'all',
                    members_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS call_queues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_number TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT 'round_robin',
                    members_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ivr_menus (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ivr_number TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    prompt_file TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL DEFAULT 5,
                    invalid_target TEXT NOT NULL DEFAULT 'voicemail',
                    routes_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS active_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT UNIQUE NOT NULL,
                    from_ext TEXT NOT NULL,
                    to_ext TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    answered_at INTEGER,
                    ended_at INTEGER,
                    trunk_name TEXT DEFAULT '',
                    recording_path TEXT DEFAULT '',
                    extra_json TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_active_calls_state ON active_calls(state);

                CREATE TABLE IF NOT EXISTS cdr (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL,
                    from_ext TEXT NOT NULL,
                    to_ext TEXT NOT NULL,
                    result TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    answered_at INTEGER,
                    ended_at INTEGER,
                    duration_seconds INTEGER DEFAULT 0,
                    bill_seconds INTEGER DEFAULT 0,
                    trunk_name TEXT DEFAULT '',
                    cost REAL DEFAULT 0.0
                );
                CREATE INDEX IF NOT EXISTS idx_cdr_started_at ON cdr(started_at);

                CREATE TABLE IF NOT EXISTS call_recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    codec TEXT NOT NULL,
                    duration_seconds INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rtp_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT UNIQUE NOT NULL,
                    caller_ip TEXT NOT NULL,
                    caller_port INTEGER NOT NULL,
                    callee_ip TEXT NOT NULL,
                    callee_port INTEGER NOT NULL,
                    codec TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    ended_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS voicemail_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extension TEXT NOT NULL,
                    caller_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    is_new INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_ext TEXT NOT NULL,
                    to_ext TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS presence (
                    extension TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webhooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS security_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT UNIQUE NOT NULL,
                    reason TEXT NOT NULL,
                    blocked_at INTEGER NOT NULL,
                    expires_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    otp_secret TEXT DEFAULT '',
                    otp_enabled INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                """
            )
            self._ensure_seed_data(conn)

    def _ensure_seed_data(self, conn: sqlite3.Connection):
        now = int(time.time())
        defaults = [
            ("1000", "Test User 1000", "1000", "smurf1000"),
            ("1001", "Test User 1001", "1001", "smurf1001"),
        ]
        for ext, name, auth_username, auth_password in defaults:
            exists = conn.execute(
                "SELECT id FROM extensions WHERE extension = ?", (ext,)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO extensions (
                    extension, display_name, auth_username, auth_password, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ext, name, auth_username, auth_password, now),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO presence (extension, status, updated_at)
                VALUES (?, 'available', ?)
                """,
                (ext, now),
            )

        admin = conn.execute(
            "SELECT id FROM users WHERE username = 'admin'"
        ).fetchone()
        if not admin:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("admin", hash_password("smurfadmin"), "superadmin", now),
            )
        if not conn.execute("SELECT id FROM trunks WHERE name = 'default-sip-trunk'").fetchone():
            conn.execute(
                """
                INSERT INTO trunks (
                    name, host, port, transport, auth_type, username, password,
                    outbound_prefix, priority, active, max_channels
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "default-sip-trunk",
                    "127.0.0.1",
                    5060,
                    "udp",
                    "credentials",
                    "trunkuser",
                    "trunkpass",
                    "9",
                    100,
                    1,
                    60,
                ),
            )

    # Generic helpers
    def fetchall(self, query: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.conn() as conn:
            return conn.execute(query, tuple(params)).fetchall()

    def fetchone(
        self, query: str, params: Iterable[Any] = ()
    ) -> Optional[Dict[str, Any]]:
        with self.conn() as conn:
            return conn.execute(query, tuple(params)).fetchone()

    def execute(self, query: str, params: Iterable[Any] = ()) -> int:
        with self.conn() as conn:
            cur = conn.execute(query, tuple(params))
            return cur.lastrowid

    # Extension helpers
    def list_extensions(self) -> List[Dict[str, Any]]:
        return self.fetchall(
            """
            SELECT id, extension, display_name, auth_username, voicemail_pin, max_calls,
                   role, outbound_cid, enabled, created_at
            FROM extensions ORDER BY extension
            """
        )

    def get_extension_by_auth(self, username: str) -> Optional[Dict[str, Any]]:
        return self.fetchone("SELECT * FROM extensions WHERE auth_username = ?", (username,))

    def get_extension(self, extension: str) -> Optional[Dict[str, Any]]:
        return self.fetchone("SELECT * FROM extensions WHERE extension = ?", (extension,))

    def create_extension(
        self,
        extension: str,
        display_name: str,
        auth_username: str,
        auth_password: str,
        voicemail_pin: str = "1234",
        max_calls: int = 3,
        role: str = "user",
    ) -> int:
        now = int(time.time())
        ext_id = self.execute(
            """
            INSERT INTO extensions (
                extension, display_name, auth_username, auth_password,
                voicemail_pin, max_calls, role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extension,
                display_name,
                auth_username,
                auth_password,
                voicemail_pin,
                max_calls,
                role,
                now,
            ),
        )
        self.execute(
            """
            INSERT OR REPLACE INTO presence (extension, status, updated_at)
            VALUES (?, 'available', ?)
            """,
            (extension, now),
        )
        return ext_id

    def update_extension(
        self,
        extension: str,
        display_name: str,
        auth_password: str,
        voicemail_pin: str,
        max_calls: int,
        role: str,
        enabled: bool,
    ) -> None:
        self.execute(
            """
            UPDATE extensions
            SET display_name = ?, auth_password = ?, voicemail_pin = ?, max_calls = ?,
                role = ?, enabled = ?
            WHERE extension = ?
            """,
            (
                display_name,
                auth_password,
                voicemail_pin,
                max_calls,
                role,
                int(enabled),
                extension,
            ),
        )

    def delete_extension(self, extension: str) -> None:
        self.execute("DELETE FROM registrations WHERE extension = ?", (extension,))
        self.execute("DELETE FROM presence WHERE extension = ?", (extension,))
        self.execute("DELETE FROM extensions WHERE extension = ?", (extension,))

    # Registration helpers
    def upsert_registration(
        self,
        extension: str,
        contact_uri: str,
        source_ip: str,
        source_port: int,
        transport: str,
        user_agent: str,
        expires_at: int,
    ) -> int:
        now = int(time.time())
        existing = self.fetchone(
            """
            SELECT id FROM registrations
            WHERE extension = ? AND source_ip = ? AND source_port = ?
            """,
            (extension, source_ip, source_port),
        )
        if existing:
            self.execute(
                """
                UPDATE registrations
                SET contact_uri = ?, transport = ?, user_agent = ?, expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    contact_uri,
                    transport,
                    user_agent,
                    expires_at,
                    now,
                    existing["id"],
                ),
            )
            return int(existing["id"])
        return self.execute(
            """
            INSERT INTO registrations (
                extension, contact_uri, source_ip, source_port, transport, user_agent, expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extension,
                contact_uri,
                source_ip,
                source_port,
                transport,
                user_agent,
                expires_at,
                now,
            ),
        )

    def remove_registration(self, extension: str, source_ip: str, source_port: int):
        self.execute(
            """
            DELETE FROM registrations
            WHERE extension = ? AND source_ip = ? AND source_port = ?
            """,
            (extension, source_ip, source_port),
        )

    def purge_expired_registrations(self):
        now = int(time.time())
        self.execute("DELETE FROM registrations WHERE expires_at <= ?", (now,))

    def active_registrations(self) -> List[Dict[str, Any]]:
        now = int(time.time())
        return self.fetchall(
            """
            SELECT * FROM registrations
            WHERE expires_at > ?
            ORDER BY extension, updated_at DESC
            """,
            (now,),
        )

    def get_best_registration(self, extension: str) -> Optional[Dict[str, Any]]:
        now = int(time.time())
        return self.fetchone(
            """
            SELECT * FROM registrations
            WHERE extension = ? AND expires_at > ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (extension, now),
        )

    # Trunks and routing
    def list_trunks(self) -> List[Dict[str, Any]]:
        return self.fetchall("SELECT * FROM trunks ORDER BY priority ASC, id ASC")

    def create_trunk(
        self,
        name: str,
        host: str,
        port: int,
        transport: str,
        auth_type: str,
        username: str,
        password: str,
        outbound_prefix: str,
        priority: int,
        max_channels: int,
    ) -> int:
        return self.execute(
            """
            INSERT INTO trunks (
                name, host, port, transport, auth_type, username, password,
                outbound_prefix, priority, max_channels
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                host,
                port,
                transport,
                auth_type,
                username,
                password,
                outbound_prefix,
                priority,
                max_channels,
            ),
        )

    def list_dialplan_rules(self) -> List[Dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM dialplan_rules WHERE enabled = 1 ORDER BY priority ASC, id ASC"
        )

    def create_dialplan_rule(
        self, name: str, pattern: str, action: str, target: str, priority: int
    ) -> int:
        return self.execute(
            """
            INSERT INTO dialplan_rules (name, pattern, action, target, priority)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, pattern, action, target, priority),
        )

    def list_ring_groups(self) -> List[Dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM ring_groups ORDER BY group_number")
        for row in rows:
            row["members"] = json.loads(row.get("members_json") or "[]")
        return rows

    def create_ring_group(
        self,
        group_number: str,
        name: str,
        strategy: str,
        members: list[str],
    ) -> int:
        return self.execute(
            """
            INSERT INTO ring_groups (group_number, name, strategy, members_json)
            VALUES (?, ?, ?, ?)
            """,
            (group_number, name, strategy, json.dumps(members)),
        )

    def list_queues(self) -> List[Dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM call_queues ORDER BY queue_number")
        for row in rows:
            row["members"] = json.loads(row.get("members_json") or "[]")
        return rows

    def create_queue(
        self,
        queue_number: str,
        name: str,
        strategy: str,
        members: list[str],
    ) -> int:
        return self.execute(
            """
            INSERT INTO call_queues (queue_number, name, strategy, members_json)
            VALUES (?, ?, ?, ?)
            """,
            (queue_number, name, strategy, json.dumps(members)),
        )

    def list_ivr(self) -> List[Dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM ivr_menus ORDER BY ivr_number")
        for row in rows:
            row["routes"] = json.loads(row.get("routes_json") or "{}")
        return rows

    def create_ivr(
        self,
        ivr_number: str,
        name: str,
        prompt_file: str,
        timeout_seconds: int,
        invalid_target: str,
        routes: dict[str, str],
    ) -> int:
        return self.execute(
            """
            INSERT INTO ivr_menus (
                ivr_number, name, prompt_file, timeout_seconds, invalid_target, routes_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ivr_number,
                name,
                prompt_file,
                timeout_seconds,
                invalid_target,
                json.dumps(routes),
            ),
        )

    # Calls / CDR
    def start_call(
        self,
        call_id: str,
        from_ext: str,
        to_ext: str,
        trunk_name: str = "",
        extra: dict[str, Any] | None = None,
    ):
        now = int(time.time())
        self.execute(
            """
            INSERT OR REPLACE INTO active_calls (
                call_id, from_ext, to_ext, state, started_at, trunk_name, extra_json
            ) VALUES (?, ?, ?, 'ringing', ?, ?, ?)
            """,
            (call_id, from_ext, to_ext, now, trunk_name, json.dumps(extra or {})),
        )

    def update_call_state(self, call_id: str, state: str):
        now = int(time.time())
        if state == "answered":
            self.execute(
                """
                UPDATE active_calls
                SET state = ?, answered_at = COALESCE(answered_at, ?)
                WHERE call_id = ?
                """,
                (state, now, call_id),
            )
        else:
            self.execute(
                "UPDATE active_calls SET state = ? WHERE call_id = ?", (state, call_id)
            )

    def list_active_calls(self) -> List[Dict[str, Any]]:
        return self.fetchall("SELECT * FROM active_calls ORDER BY started_at DESC")

    def end_call(self, call_id: str, result: str):
        now = int(time.time())
        call = self.fetchone("SELECT * FROM active_calls WHERE call_id = ?", (call_id,))
        if not call:
            return
        started = int(call["started_at"])
        answered = call.get("answered_at")
        duration = max(0, now - started)
        bill = max(0, now - int(answered)) if answered else 0
        self.execute(
            """
            INSERT INTO cdr (
                call_id, from_ext, to_ext, result, started_at, answered_at, ended_at,
                duration_seconds, bill_seconds, trunk_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                call["from_ext"],
                call["to_ext"],
                result,
                started,
                answered,
                now,
                duration,
                bill,
                call.get("trunk_name", ""),
            ),
        )
        self.execute("DELETE FROM active_calls WHERE call_id = ?", (call_id,))

    def cdr_history(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM cdr ORDER BY started_at DESC LIMIT ?", (int(limit),)
        )

    # Recording / voicemail
    def add_recording(self, call_id: str, file_path: str, codec: str, duration: int):
        self.execute(
            """
            INSERT INTO call_recordings (call_id, file_path, codec, duration_seconds, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (call_id, file_path, codec, duration, int(time.time())),
        )

    def list_recordings(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM call_recordings ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )

    def add_voicemail(
        self, extension: str, caller_id: str, file_path: str, duration_seconds: int
    ):
        self.execute(
            """
            INSERT INTO voicemail_messages (extension, caller_id, file_path, duration_seconds, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (extension, caller_id, file_path, duration_seconds, int(time.time())),
        )

    def voicemail_for_extension(self, extension: str) -> List[Dict[str, Any]]:
        return self.fetchall(
            """
            SELECT * FROM voicemail_messages
            WHERE extension = ?
            ORDER BY created_at DESC
            """,
            (extension,),
        )

    # Chat / presence
    def add_chat_message(self, from_ext: str, to_ext: str, message: str):
        self.execute(
            """
            INSERT INTO chat_messages (from_ext, to_ext, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (from_ext, to_ext, message, int(time.time())),
        )

    def chat_history(self, ext_a: str, ext_b: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self.fetchall(
            """
            SELECT * FROM chat_messages
            WHERE (from_ext = ? AND to_ext = ?) OR (from_ext = ? AND to_ext = ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (ext_a, ext_b, ext_b, ext_a, int(limit)),
        )

    def set_presence(self, extension: str, status: str, note: str = ""):
        self.execute(
            """
            INSERT OR REPLACE INTO presence (extension, status, note, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (extension, status, note, int(time.time())),
        )

    def list_presence(self) -> List[Dict[str, Any]]:
        return self.fetchall("SELECT * FROM presence ORDER BY extension")

    # Webhooks
    def list_webhooks(self) -> List[Dict[str, Any]]:
        return self.fetchall("SELECT * FROM webhooks ORDER BY id DESC")

    def create_webhook(self, event_name: str, target_url: str, active: bool = True) -> int:
        return self.execute(
            """
            INSERT INTO webhooks (event_name, target_url, active)
            VALUES (?, ?, ?)
            """,
            (event_name, target_url, int(active)),
        )

    # Security
    def add_security_block(self, ip: str, reason: str, expires_at: int | None):
        self.execute(
            """
            INSERT INTO security_blocks (ip, reason, blocked_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET reason = excluded.reason, blocked_at = excluded.blocked_at, expires_at = excluded.expires_at
            """,
            (ip, reason, int(time.time()), expires_at),
        )

    def is_blocked_ip(self, ip: str) -> bool:
        row = self.fetchone(
            """
            SELECT * FROM security_blocks
            WHERE ip = ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (ip, int(time.time())),
        )
        return row is not None

    # User management
    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        return self.fetchone("SELECT * FROM users WHERE username = ?", (username,))

    def set_user_otp(self, username: str, otp_secret: str, otp_enabled: bool):
        self.execute(
            """
            UPDATE users SET otp_secret = ?, otp_enabled = ?
            WHERE username = ?
            """,
            (otp_secret, int(otp_enabled), username),
        )

    # Dashboard
    def dashboard_stats(self) -> Dict[str, Any]:
        now = int(time.time())
        today_start = now - (now % 86400)
        active_calls = self.fetchone("SELECT COUNT(*) AS n FROM active_calls")
        regs = self.fetchone(
            "SELECT COUNT(*) AS n FROM registrations WHERE expires_at > ?", (now,)
        )
        trunks = self.fetchone("SELECT COUNT(*) AS n FROM trunks WHERE active = 1")
        calls_today = self.fetchone(
            "SELECT COUNT(*) AS n FROM cdr WHERE started_at >= ?", (today_start,)
        )
        return {
            "active_calls": int((active_calls or {}).get("n", 0)),
            "registered_extensions": int((regs or {}).get("n", 0)),
            "active_trunks": int((trunks or {}).get("n", 0)),
            "calls_today": int((calls_today or {}).get("n", 0)),
        }

