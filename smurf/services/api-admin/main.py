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
import time
from pathlib import Path
from typing import Any

import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pyotp
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
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
    hash_password,
    verify_password,
)

LOGGER = get_logger("api-admin")


class LoginRequest(BaseModel):
    username: str
    password: str
    otp_code: str | None = None


class ExtensionCreate(BaseModel):
    extension: str = Field(pattern=r"^[0-9]+$")
    display_name: str
    auth_username: str
    auth_password: str
    voicemail_pin: str = "1234"
    max_calls: int = 3
    role: str = "user"


class ExtensionUpdate(BaseModel):
    display_name: str
    auth_password: str
    voicemail_pin: str
    max_calls: int
    role: str
    enabled: bool = True


class TrunkCreate(BaseModel):
    name: str
    host: str
    port: int = 5060
    transport: str = "udp"
    auth_type: str = "credentials"
    username: str = ""
    password: str = ""
    outbound_prefix: str = ""
    priority: int = 100
    max_channels: int = 30


class DialplanRuleCreate(BaseModel):
    name: str
    pattern: str
    action: str
    target: str
    priority: int = 100


class ChatRequest(BaseModel):
    from_ext: str
    to_ext: str
    message: str


class PresenceRequest(BaseModel):
    extension: str
    status: str
    note: str = ""


class OriginateRequest(BaseModel):
    from_ext: str
    to_ext: str
    call_id: str | None = None


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
            return {"items": self.db.cdr_history(limit)}

        @self.app.get("/api/v1/cdr/export/csv")
        async def cdr_export_csv(request: Request, limit: int = 500):
            _ = self._admin_guard(request.headers.get("Authorization"))
            rows = self.db.cdr_history(limit)
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
            rows = self.db.cdr_history(limit)
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
            return {"items": self.db.list_recordings(limit)}

        @self.app.get("/api/v1/recordings/download")
        async def download_recording(request: Request, path: str):
            _ = self._admin_guard(request.headers.get("Authorization"))
            file_path = Path(path)
            if not file_path.exists() or not file_path.is_file():
                raise HTTPException(status_code=404, detail="Recording not found")
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
            return {"items": self.db.chat_history(ext_a, ext_b, limit)}

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
            db_path = Path(self.config.database.sqlite_path)
            if not db_path.exists():
                raise HTTPException(status_code=404, detail="Database not found")
            backup_path = self.backup_dir / f"smurf-backup-{ts}.db"
            backup_path.write_bytes(db_path.read_bytes())
            return {"ok": True, "path": str(backup_path)}

        @self.app.post("/api/v1/restore")
        async def restore(request: Request, backup_path: str):
            _ = self._admin_guard(request.headers.get("Authorization"))
            src = Path(backup_path)
            dst = Path(self.config.database.sqlite_path)
            if not src.exists():
                raise HTTPException(status_code=404, detail="Backup file not found")
            dst.write_bytes(src.read_bytes())
            return {"ok": True}

        @self.app.get("/api/v1/logs")
        async def get_logs(request: Request, limit: int = 200):
            _ = self._admin_guard(request.headers.get("Authorization"))
            log_path = Path("/var/log/smurf/smurf.log")
            if not log_path.exists():
                return {"items": []}
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return {"items": lines[-limit:]}

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
            tpl_dir = Path(self.config.provisioning.templates_path)
            tpl_file = tpl_dir / f"{vendor.lower()}.tpl"
            if not tpl_file.exists():
                raise HTTPException(status_code=404, detail="Template not found")
            return Response(tpl_file.read_text(encoding="utf-8"), media_type="text/plain")

        @self.app.get("/provisioning/{vendor}/{extension}.cfg")
        async def provisioning_cfg(vendor: str, extension: str):
            ext = self.db.get_extension(extension)
            if not ext:
                raise HTTPException(status_code=404, detail="Unknown extension")
            tpl_dir = Path(self.config.provisioning.templates_path)
            tpl_file = tpl_dir / f"{vendor.lower()}.tpl"
            if not tpl_file.exists():
                raise HTTPException(status_code=404, detail="Template not found")
            template = tpl_file.read_text(encoding="utf-8")
            payload = template.format(
                EXTENSION=extension,
                AUTH_USER=ext["auth_username"],
                AUTH_PASS=ext["auth_password"],
                SIP_SERVER=self.config.global_.domain,
                SIP_PORT=self.config.sip.udp_port,
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

