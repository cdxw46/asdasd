"""SMURF API + Admin service.

Provides:
- HTTPS admin panel (port 5001 by default)
- JWT auth and optional TOTP 2FA
- Extension/trunk/dialplan management
- CDR reports (JSON/CSV/Excel)
- Real-time dashboard and softphone web UI
- Provisioning endpoints and basic backup/restore APIs
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pyotp
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.status import HTTP_401_UNAUTHORIZED
import uvicorn
from openpyxl import Workbook

from core.bus import JsonCommandClient
from core.config import load_config
from core.db import Database
from core.logging_utils import configure_json_logging, get_logger
from core.security import (
    create_jwt,
    decode_jwt,
    verify_password,
)

LOGGER = get_logger("api-admin")
VALID_PRESENCE_STATUSES = {"available", "busy", "away", "dnd", "offline"}
TRUNK_TRANSPORTS = {"udp", "tcp", "tls"}
TRUNK_AUTH_TYPES = {"credentials", "ip"}
MAX_EXPORT_LIMIT = 5000
MAX_CHAT_MESSAGE_LENGTH = 4096


def _normalize_extension(value: str) -> str:
    return value.strip()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    otp_code: str | None = Field(default=None, min_length=6, max_length=16)

    @field_validator("username")
    @classmethod
    def _username_strip(cls, value: str) -> str:
        return value.strip()


class ExtensionCreate(BaseModel):
    extension: str = Field(pattern=r"^[0-9]+$", min_length=2, max_length=15)
    display_name: str = Field(min_length=1, max_length=128)
    auth_username: str = Field(min_length=1, max_length=64)
    auth_password: str = Field(min_length=6, max_length=128)
    voicemail_pin: str = Field(default="1234", pattern=r"^[0-9]{4,10}$")
    max_calls: int = Field(default=3, ge=1, le=100)
    role: str = Field(default="user", min_length=1, max_length=32)

    @field_validator("extension")
    @classmethod
    def _extension_strip(cls, value: str) -> str:
        return _normalize_extension(value)


class ExtensionUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    auth_password: str = Field(min_length=6, max_length=128)
    voicemail_pin: str = Field(pattern=r"^[0-9]{4,10}$")
    max_calls: int = Field(ge=1, le=100)
    role: str = Field(min_length=1, max_length=32)
    enabled: bool = True


class TrunkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5060, ge=1, le=65535)
    transport: str = Field(default="udp", min_length=3, max_length=8)
    auth_type: str = Field(default="credentials", min_length=2, max_length=32)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)
    outbound_prefix: str = Field(default="", max_length=16)
    priority: int = Field(default=100, ge=1, le=10000)
    max_channels: int = Field(default=30, ge=1, le=1000)

    @field_validator("transport")
    @classmethod
    def _validate_transport(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in TRUNK_TRANSPORTS:
            raise ValueError("invalid transport")
        return normalized

    @field_validator("auth_type")
    @classmethod
    def _validate_auth_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in TRUNK_AUTH_TYPES:
            raise ValueError("invalid auth_type")
        return normalized


class DialplanRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    pattern: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=256)
    priority: int = Field(default=100, ge=1, le=10000)

    @field_validator("action")
    @classmethod
    def _validate_action(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"extension", "ring_group", "queue", "ivr", "trunk", "reject"}:
            raise ValueError("invalid dialplan action")
        return normalized

    @field_validator("pattern")
    @classmethod
    def _validate_regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return value


class ChatRequest(BaseModel):
    from_ext: str = Field(pattern=r"^[0-9]+$", min_length=2, max_length=15)
    to_ext: str = Field(pattern=r"^[0-9]+$", min_length=2, max_length=15)
    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LENGTH)


class PresenceRequest(BaseModel):
    extension: str = Field(pattern=r"^[0-9]+$", min_length=2, max_length=15)
    status: str = Field(min_length=1, max_length=16)
    note: str = Field(default="", max_length=256)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_PRESENCE_STATUSES:
            raise ValueError("invalid presence status")
        return normalized


class OriginateRequest(BaseModel):
    from_ext: str = Field(pattern=r"^[0-9]+$", min_length=2, max_length=15)
    to_ext: str = Field(pattern=r"^[0-9*#+]+$", min_length=1, max_length=32)
    call_id: str | None = Field(default=None, max_length=128)


def _now() -> int:
    return int(time.time())


class AdminService:
    def __init__(self, config_path: str | None):
        self.config = load_config(config_path)
        configure_json_logging("api-admin", self.config.global_.log_level)
        self.db = Database(self.config.database.sqlite_path)
        self.pbx_client = JsonCommandClient(
            self.config.bus.pbx_command_host,
            self.config.bus.pbx_command_port,
            timeout=3.0,
        )
        self.media_client = JsonCommandClient(
            self.config.bus.media_command_host,
            self.config.bus.media_command_port,
            timeout=3.0,
        )
        self.sip_client = JsonCommandClient(
            self.config.bus.sip_command_host,
            self.config.bus.sip_command_port,
            timeout=3.0,
        )
        self.base_dir = Path(__file__).resolve().parents[2]
        self.web_dir = self.base_dir / "web-admin"
        self.web_dir.mkdir(parents=True, exist_ok=True)
        db_parent = Path(self.config.database.sqlite_path).resolve().parent
        self.backup_dir = db_parent / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.app = FastAPI(
            title="SMURF PBX API",
            version="1.0.0",
            description="Enterprise PBX API and admin service",
        )
        self._configure_routes()

    def _access_token(self, username: str, role: str) -> str:
        return create_jwt(
            subject=username,
            secret=self.config.security.jwt_secret,
            expires_seconds=self.config.security.access_token_minutes * 60,
            extra={"role": role, "type": "access"},
        )

    def _auth_from_header(self, authorization: str | None) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
            )
        token = authorization[7:]
        try:
            payload = decode_jwt(token, self.config.security.jwt_secret)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {exc}",
            ) from exc
        return payload.claims

    def _admin_guard(self, authorization: str | None) -> dict[str, Any]:
        claims = self._auth_from_header(authorization)
        role = str(claims.get("role", ""))
        if role not in {"superadmin", "admin", "supervisor"}:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return claims

    def _safe_limit(self, value: int, *, default: int = 100, max_value: int = MAX_EXPORT_LIMIT) -> int:
        if value <= 0:
            return default
        return min(int(value), max_value)

    def _sanitize_vendor(self, vendor: str) -> str:
        normalized = vendor.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{2,32}", normalized):
            raise HTTPException(status_code=400, detail="Invalid vendor")
        return normalized

    def _safe_recording_path(self, requested_path: str) -> Path:
        target = Path(requested_path).resolve()
        recording_root = Path(self.config.rtp.recording_path).resolve()
        if target != recording_root and recording_root not in target.parents:
            raise HTTPException(status_code=400, detail="Recording path not allowed")
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Recording not found")
        return target

    def _safe_backup_source(self, backup_path: str) -> Path:
        source = Path(backup_path).resolve()
        allowed_root = self.backup_dir.resolve()
        if source != allowed_root and allowed_root not in source.parents:
            raise HTTPException(status_code=400, detail="Backup path not allowed")
        if not source.exists() or not source.is_file():
            raise HTTPException(status_code=404, detail="Backup file not found")
        return source

    def _backup_database(self, destination: Path) -> None:
        source_db = Path(self.config.database.sqlite_path).resolve()
        if not source_db.exists():
            raise HTTPException(status_code=404, detail="Database not found")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source_db) as src_conn:
            with sqlite3.connect(destination) as dst_conn:
                src_conn.backup(dst_conn)

    def _restore_database(self, source: Path) -> None:
        dst = Path(self.config.database.sqlite_path).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp_dst = dst.with_suffix(".restore-tmp")
        if tmp_dst.exists():
            tmp_dst.unlink()
        with sqlite3.connect(source) as src_conn:
            with sqlite3.connect(tmp_dst) as dst_conn:
                src_conn.backup(dst_conn)
        shutil.move(str(tmp_dst), str(dst))

    def _configure_routes(self):
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=self.config.api.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        static_dir = self.web_dir / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @self.app.exception_handler(RequestValidationError)
        async def validation_exception_handler(_request: Request, exc: RequestValidationError):
            return JSONResponse(status_code=422, content={"detail": exc.errors()})

        @self.app.exception_handler(Exception)
        async def unhandled_exception_handler(_request: Request, exc: Exception):
            LOGGER.exception("Unhandled API exception: %s", exc)
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})

        @self.app.get("/health")
        async def health():
            pbx = await self.pbx_client.request({"action": "ping"})
            media = await self.media_client.request({"action": "ping"})
            sip = await self.sip_client.request({"action": "ping"})
            return {
                "status": "ok",
                "services": {"pbx": pbx, "media": media, "sip": sip},
            }

        @self.app.post("/api/v1/auth/login")
        async def login(body: LoginRequest):
            user = self.db.get_user(body.username)
            if not user:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            if not verify_password(body.password, user["password_hash"]):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            if int(user.get("otp_enabled", 0)) == 1:
                if not body.otp_code:
                    raise HTTPException(status_code=401, detail="OTP required")
                secret = str(user.get("otp_secret") or "")
                if not secret:
                    raise HTTPException(status_code=401, detail="OTP misconfigured")
                totp = pyotp.TOTP(secret)
                if not totp.verify(body.otp_code, valid_window=1):
                    raise HTTPException(status_code=401, detail="Invalid OTP code")
            token = self._access_token(body.username, user["role"])
            return {"access_token": token, "token_type": "bearer", "role": user["role"]}

        @self.app.post("/api/v1/auth/enable-2fa")
        async def enable_2fa(request: Request):
            claims = self._admin_guard(request.headers.get("Authorization"))
            username = str(claims.get("sub", ""))
            user = self.db.get_user(username)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            secret = pyotp.random_base32()
            self.db.set_user_otp(username, secret, True)
            uri = pyotp.TOTP(secret).provisioning_uri(
                name=username,
                issuer_name=self.config.security.web_2fa_issuer,
            )
            return {"secret": secret, "otpauth_uri": uri}

        @self.app.get("/api/v1/dashboard")
        async def dashboard(request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            data = self.db.dashboard_stats()
            data["presence"] = self.db.list_presence()
            data["active_calls_detail"] = self.db.list_active_calls()
            return data

        @self.app.get("/api/v1/extensions")
        async def list_extensions(request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            return {"items": self.db.list_extensions()}

        @self.app.post("/api/v1/extensions")
        async def create_extension(body: ExtensionCreate, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            if self.db.get_extension(body.extension):
                raise HTTPException(status_code=409, detail="Extension already exists")
            self.db.create_extension(
                extension=body.extension,
                display_name=body.display_name,
                auth_username=body.auth_username,
                auth_password=body.auth_password,
                voicemail_pin=body.voicemail_pin,
                max_calls=body.max_calls,
                role=body.role,
            )
            return {"ok": True}

        @self.app.put("/api/v1/extensions/{extension}")
        async def update_extension(extension: str, body: ExtensionUpdate, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            if not self.db.get_extension(extension):
                raise HTTPException(status_code=404, detail="Extension not found")
            self.db.update_extension(
                extension=extension,
                display_name=body.display_name,
                auth_password=body.auth_password,
                voicemail_pin=body.voicemail_pin,
                max_calls=body.max_calls,
                role=body.role,
                enabled=body.enabled,
            )
            return {"ok": True}

        @self.app.delete("/api/v1/extensions/{extension}")
        async def delete_extension(extension: str, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            if not self.db.get_extension(extension):
                raise HTTPException(status_code=404, detail="Extension not found")
            self.db.delete_extension(extension)
            return {"ok": True}

        @self.app.get("/api/v1/trunks")
        async def list_trunks(request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            return {"items": self.db.list_trunks()}

        @self.app.post("/api/v1/trunks")
        async def create_trunk(body: TrunkCreate, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            trunk_id = self.db.create_trunk(
                name=body.name,
                host=body.host,
                port=body.port,
                transport=body.transport,
                auth_type=body.auth_type,
                username=body.username,
                password=body.password,
                outbound_prefix=body.outbound_prefix,
                priority=body.priority,
                max_channels=body.max_channels,
            )
            return {"ok": True, "id": trunk_id}

        @self.app.get("/api/v1/dialplan")
        async def list_dialplan(request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            return {"items": self.db.list_dialplan_rules()}

        @self.app.post("/api/v1/dialplan")
        async def create_dialplan(body: DialplanRuleCreate, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            rule_id = self.db.create_dialplan_rule(
                name=body.name,
                pattern=body.pattern,
                action=body.action,
                target=body.target,
                priority=body.priority,
            )
            return {"ok": True, "id": rule_id}

        @self.app.post("/api/v1/calls/originate")
        async def originate_call(body: OriginateRequest, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            call_id = body.call_id or f"smurf-{_now()}-{body.from_ext}-{body.to_ext}"
            result = await self.pbx_client.request(
                {
                    "action": "route_call",
                    "call_id": call_id,
                    "from_ext": body.from_ext,
                    "to_ext": body.to_ext,
                }
            )
            return {"call_id": call_id, "result": result}

        @self.app.get("/api/v1/calls/active")
        async def active_calls(request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            return {"items": self.db.list_active_calls()}

        @self.app.get("/api/v1/cdr")
        async def cdr_history(request: Request, limit: int = 500):
            _ = self._admin_guard(request.headers.get("Authorization"))
            safe_limit = self._safe_limit(limit, default=500)
            return {"items": self.db.cdr_history(safe_limit)}

        @self.app.get("/api/v1/cdr/export/csv")
        async def cdr_export_csv(request: Request, limit: int = 500):
            _ = self._admin_guard(request.headers.get("Authorization"))
            safe_limit = self._safe_limit(limit, default=500)
            rows = self.db.cdr_history(safe_limit)
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "id",
                    "call_id",
                    "from_ext",
                    "to_ext",
                    "result",
                    "started_at",
                    "answered_at",
                    "ended_at",
                    "duration_seconds",
                    "bill_seconds",
                    "trunk_name",
                    "cost",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            return StreamingResponse(
                iter([output.getvalue()]),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="smurf_cdr.csv"'},
            )

        @self.app.get("/api/v1/cdr/export/excel")
        async def cdr_export_excel(request: Request, limit: int = 500):
            _ = self._admin_guard(request.headers.get("Authorization"))
            safe_limit = self._safe_limit(limit, default=500)
            rows = self.db.cdr_history(safe_limit)
            wb = Workbook()
            ws = wb.active
            ws.title = "CDR"
            headers = [
                "id",
                "call_id",
                "from_ext",
                "to_ext",
                "result",
                "started_at",
                "answered_at",
                "ended_at",
                "duration_seconds",
                "bill_seconds",
                "trunk_name",
                "cost",
            ]
            ws.append(headers)
            for row in rows:
                ws.append([row.get(h) for h in headers])
            stream = io.BytesIO()
            wb.save(stream)
            stream.seek(0)
            return StreamingResponse(
                stream,
                media_type=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                headers={"Content-Disposition": 'attachment; filename="smurf_cdr.xlsx"'},
            )

        @self.app.get("/api/v1/recordings")
        async def list_recordings(request: Request, limit: int = 200):
            _ = self._admin_guard(request.headers.get("Authorization"))
            safe_limit = self._safe_limit(limit, default=200, max_value=1000)
            return {"items": self.db.list_recordings(safe_limit)}

        @self.app.get("/api/v1/recordings/download")
        async def download_recording(request: Request, path: str):
            _ = self._admin_guard(request.headers.get("Authorization"))
            file_path = self._safe_recording_path(path)
            return FileResponse(str(file_path))

        @self.app.get("/api/v1/voicemail/{extension}")
        async def voicemail(extension: str, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            return {"items": self.db.voicemail_for_extension(extension)}

        @self.app.post("/api/v1/chat/send")
        async def chat_send(body: ChatRequest, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            result = await self.pbx_client.request(
                {
                    "action": "chat_send",
                    "from_ext": body.from_ext,
                    "to_ext": body.to_ext,
                    "message": body.message,
                }
            )
            return result

        @self.app.get("/api/v1/chat/history")
        async def chat_history(request: Request, ext_a: str, ext_b: str, limit: int = 200):
            _ = self._admin_guard(request.headers.get("Authorization"))
            safe_limit = self._safe_limit(limit, default=200, max_value=1000)
            return {"items": self.db.chat_history(ext_a, ext_b, safe_limit)}

        @self.app.post("/api/v1/presence/set")
        async def set_presence(body: PresenceRequest, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            result = await self.pbx_client.request(
                {
                    "action": "set_presence",
                    "extension": body.extension,
                    "status": body.status,
                    "note": body.note,
                }
            )
            return result

        @self.app.get("/api/v1/presence")
        async def list_presence(request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            return {"items": self.db.list_presence()}

        @self.app.post("/api/v1/backup")
        async def backup(request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            ts = _now()
            backup_path = self.backup_dir / f"smurf-backup-{ts}.db"
            self._backup_database(backup_path)
            return {"ok": True, "path": str(backup_path)}

        @self.app.post("/api/v1/restore")
        async def restore(request: Request, backup_path: str):
            _ = self._admin_guard(request.headers.get("Authorization"))
            src = self._safe_backup_source(backup_path)
            self._restore_database(src)
            return {"ok": True}

        @self.app.get("/api/v1/logs")
        async def get_logs(request: Request, limit: int = 200):
            _ = self._admin_guard(request.headers.get("Authorization"))
            log_path = Path(os.environ.get("SMURF_LOG_PATH", "/var/log/smurf/smurf.log"))
            if not log_path.exists():
                return {"items": []}
            safe_limit = self._safe_limit(limit, default=200, max_value=2000)
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return {"items": lines[-safe_limit:]}

        @self.app.post("/api/v1/tls/upload")
        async def upload_tls(
            request: Request,
            cert_file: UploadFile = File(...),
            key_file: UploadFile = File(...),
        ):
            _ = self._admin_guard(request.headers.get("Authorization"))
            tls_dir = Path("/etc/smurf/tls")
            tls_dir.mkdir(parents=True, exist_ok=True)
            cert_path = tls_dir / "server.crt"
            key_path = tls_dir / "server.key"
            cert_path.write_bytes(await cert_file.read())
            key_path.write_bytes(await key_file.read())
            os.chmod(cert_path, 0o644)
            os.chmod(key_path, 0o600)
            return {"ok": True, "cert_path": str(cert_path), "key_path": str(key_path)}

        @self.app.get("/api/v1/provisioning/template/{vendor}")
        async def get_provisioning_template(vendor: str, request: Request):
            _ = self._admin_guard(request.headers.get("Authorization"))
            safe_vendor = self._sanitize_vendor(vendor)
            tpl_dir = Path(self.config.provisioning.templates_path)
            tpl_file = tpl_dir / f"{safe_vendor}.tpl"
            if not tpl_file.exists():
                raise HTTPException(status_code=404, detail="Template not found")
            return Response(tpl_file.read_text(encoding="utf-8"), media_type="text/plain")

        @self.app.get("/provisioning/{vendor}/{extension}.cfg")
        async def provisioning_cfg(vendor: str, extension: str):
            safe_vendor = self._sanitize_vendor(vendor)
            ext = self.db.get_extension(extension)
            if not ext:
                raise HTTPException(status_code=404, detail="Unknown extension")
            tpl_dir = Path(self.config.provisioning.templates_path)
            tpl_file = tpl_dir / f"{safe_vendor}.tpl"
            if not tpl_file.exists():
                raise HTTPException(status_code=404, detail="Template not found")
            template = tpl_file.read_text(encoding="utf-8")
            payload = template.format(
                EXTENSION=extension,
                DISPLAY_NAME=ext.get("display_name", extension),
                AUTH_USER=ext["auth_username"],
                AUTH_PASS=ext["auth_password"],
                SIP_SERVER=self.config.global_.domain,
                SIP_PORT=self.config.sip.udp_port,
                SIP_UDP_PORT=self.config.sip.udp_port,
                SIP_TLS_PORT=self.config.sip.tls_port,
                PROVISIONING_URL=self.config.provisioning.base_url,
            )
            return Response(payload, media_type="text/plain")

        @self.app.get("/api/v1/openapi.json")
        async def openapi_json():
            return JSONResponse(self.app.openapi())

        @self.app.get("/manifest.webmanifest")
        async def manifest():
            manifest = self.web_dir / "manifest.webmanifest"
            if not manifest.exists():
                raise HTTPException(status_code=404, detail="Manifest not found")
            return FileResponse(str(manifest), media_type="application/manifest+json")

        @self.app.get("/sw.js")
        async def service_worker():
            sw_path = self.web_dir / "sw.js"
            if not sw_path.exists():
                raise HTTPException(status_code=404, detail="Service worker not found")
            return FileResponse(str(sw_path), media_type="application/javascript")

        @self.app.get("/", response_class=HTMLResponse)
        async def admin_index():
            index_path = self.web_dir / "index.html"
            if not index_path.exists():
                return HTMLResponse("<h1>SMURF Admin</h1><p>UI not found</p>", status_code=200)
            return HTMLResponse(index_path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMURF API/Admin service")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    return parser.parse_args()


def main():
    args = parse_args()
    service = AdminService(config_path=args.config)
    ssl_kwargs: dict[str, Any] = {}
    cert_path = Path(service.config.api.tls_cert_path)
    key_path = Path(service.config.api.tls_key_path)
    if cert_path.exists() and key_path.exists():
        ssl_kwargs = {
            "ssl_certfile": str(cert_path),
            "ssl_keyfile": str(key_path),
        }
    uvicorn.run(
        service.app,
        host=service.config.api.host,
        port=service.config.api.port,
        log_level=service.config.global_.log_level.lower(),
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()

