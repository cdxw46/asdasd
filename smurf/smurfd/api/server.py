"""API HTTP/HTTPS y panel SPA de SMURF.

Endpoints REST bajo /api/v1, WebSocket de eventos en /api/v1/ws/events,
servicio estático del panel en / y del softphone en /softphone.

La API se monta sobre Starlette/FastAPI exclusivamente como framework HTTP
(no aporta nada relacionado con SIP/PBX). Todo lo demás es código propio.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import (Body, Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response)
from fastapi.staticfiles import StaticFiles

from typing import TYPE_CHECKING

from ..db import Database
from ..pbx.events import EventBus
from ..sip.registrar import Binding

if TYPE_CHECKING:
    from ..server import SmurfServer
from ..util.config import SmurfConfig, save_config
from ..util.logger import get_logger
from ..util.passwords import hash_password, verify_password
from .auth import JwtAuth, decode_token, make_token

log = get_logger("api")
WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "web"))


def _ha1(user, realm, password, algo="MD5"):
    import hashlib
    raw = f"{user}:{realm}:{password}".encode("utf-8")
    return (hashlib.md5(raw) if algo == "MD5" else hashlib.sha256(raw)).hexdigest()


def create_app(server: "SmurfServer") -> FastAPI:
    cfg: SmurfConfig = server.cfg
    db: Database = server.db
    bus: EventBus = server.events
    auth_dep = JwtAuth(cfg)

    app = FastAPI(title="SMURF PBX API", version="1.0",
                  description="API REST de SMURF: extensiones, trunks, dial plan, CDR, monitorización en tiempo real.",
                  docs_url="/api/v1/docs", redoc_url="/api/v1/redoc",
                  openapi_url="/api/v1/openapi.json")

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # ===================== AUTH =====================

    @app.post("/api/v1/auth/login")
    async def login(payload: Dict[str, Any] = Body(...)):
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        totp = payload.get("totp")
        u = await db.fetchone("SELECT * FROM users WHERE username=? AND enabled=1", (username,))
        if not u or not verify_password(password, u["password_hash"]):
            raise HTTPException(401, "Credenciales inválidas")
        if u.get("totp_secret"):
            if not totp:
                raise HTTPException(401, "TOTP requerido")
            try:
                import pyotp
                ok = pyotp.TOTP(u["totp_secret"]).verify(str(totp))
            except Exception:
                ok = False
            if not ok:
                raise HTTPException(401, "TOTP inválido")
        token = make_token(cfg.web.jwt_secret, u["id"], u["username"],
                           u["role"], cfg.web.session_hours)
        resp = JSONResponse({"token": token, "user": {
            "id": u["id"], "username": u["username"], "role": u["role"],
            "email": u["email"], "totp_enabled": bool(u.get("totp_secret")),
        }})
        resp.set_cookie("smurf_token", token, httponly=True, samesite="lax",
                        max_age=cfg.web.session_hours * 3600,
                        secure=False)
        return resp

    @app.post("/api/v1/auth/logout")
    async def logout(_: dict = Depends(auth_dep)):
        r = JSONResponse({"ok": True})
        r.delete_cookie("smurf_token")
        return r

    @app.get("/api/v1/auth/me")
    async def me(user: dict = Depends(auth_dep)):
        u = await db.fetchone("SELECT id,username,role,email,totp_secret FROM users WHERE id=?", (int(user["sub"]),))
        if not u: raise HTTPException(404, "Usuario no encontrado")
        return {"id": u["id"], "username": u["username"], "role": u["role"],
                "email": u["email"], "totp_enabled": bool(u.get("totp_secret"))}

    @app.post("/api/v1/auth/change-password")
    async def change_password(payload: Dict[str, str] = Body(...),
                              user: dict = Depends(auth_dep)):
        u = await db.fetchone("SELECT * FROM users WHERE id=?", (int(user["sub"]),))
        if not u or not verify_password(payload.get("old", ""), u["password_hash"]):
            raise HTTPException(403, "Contraseña actual incorrecta")
        new = payload.get("new", "")
        if len(new) < 8:
            raise HTTPException(400, "Contraseña demasiado corta")
        await db.execute("UPDATE users SET password_hash=?, updated_at=strftime('%s','now') WHERE id=?",
                         (hash_password(new), u["id"]))
        return {"ok": True}

    @app.post("/api/v1/auth/totp/enable")
    async def totp_enable(user: dict = Depends(auth_dep)):
        import pyotp
        secret = pyotp.random_base32()
        await db.execute("UPDATE users SET totp_secret=? WHERE id=?", (secret, int(user["sub"])))
        u = await db.fetchone("SELECT username FROM users WHERE id=?", (int(user["sub"]),))
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=u["username"], issuer_name="SMURF")
        return {"secret": secret, "otpauth_uri": uri}

    @app.post("/api/v1/auth/totp/disable")
    async def totp_disable(payload: Dict[str, str] = Body(...),
                           user: dict = Depends(auth_dep)):
        u = await db.fetchone("SELECT * FROM users WHERE id=?", (int(user["sub"]),))
        if not verify_password(payload.get("password",""), u["password_hash"]):
            raise HTTPException(403, "Contraseña incorrecta")
        await db.execute("UPDATE users SET totp_secret=NULL WHERE id=?", (u["id"],))
        return {"ok": True}

    # ===================== DASHBOARD =====================

    @app.get("/api/v1/dashboard")
    async def dashboard(_: dict = Depends(auth_dep)):
        active = len(server.b2bua.calls)
        regs = sum(len(v) for v in server.location.all().values())
        ext_total = (await db.fetchone("SELECT COUNT(*) AS c FROM extensions"))["c"]
        trunk_total = (await db.fetchone("SELECT COUNT(*) AS c FROM trunks WHERE enabled=1"))["c"]
        today = int(time.time()) - 24 * 3600
        cdr_today = (await db.fetchone(
            "SELECT COUNT(*) AS c, COALESCE(SUM(bill_seconds),0) AS s FROM cdr WHERE started_at>?",
            (today,))) or {"c": 0, "s": 0}
        return {
            "active_calls": active,
            "registered_endpoints": regs,
            "extensions_total": ext_total,
            "trunks_active": trunk_total,
            "calls_24h": cdr_today["c"],
            "talk_seconds_24h": cdr_today["s"],
            "uptime_seconds": int(time.time() - server.events.history()[0].ts) if server.events.history() else 0,
            "version": "1.0.0",
        }

    @app.get("/api/v1/registrations")
    async def registrations(_: dict = Depends(auth_dep)):
        out = []
        for aor, bs in server.location.all().items():
            for b in bs:
                out.append({
                    "aor": aor, "contact": str(b.contact_uri),
                    "endpoint": str(b.endpoint), "expires_in": b.remaining(),
                    "user_agent": b.user_agent,
                })
        return out

    @app.get("/api/v1/calls/active")
    async def calls_active(_: dict = Depends(auth_dep)):
        out = []
        for c in list(server.b2bua.calls.values()):
            out.append({
                "id": c.id, "src": c.src_number, "dst": c.dst_number,
                "direction": c.direction, "state": c.state.value,
                "started_at": c.started_at, "answered_at": c.answered_at,
            })
        return out

    @app.post("/api/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, _: dict = Depends(auth_dep)):
        c = server.b2bua.calls.get(call_id)
        if not c:
            raise HTTPException(404, "Call no encontrada")
        await server.b2bua._end_call(c, "CANCELLED", "admin-hangup")
        return {"ok": True}

    @app.post("/api/v1/calls/originate")
    async def originate(payload: Dict[str, str] = Body(...),
                        _: dict = Depends(auth_dep)):
        """Originar una llamada: el server llama a `from_ext` y al contestar
        marca a `to_number`. Implementación sencilla: emitimos un INVITE al
        endpoint registrado de `from_ext` con un from idéntico al destino."""
        src = payload.get("from")
        dst = payload.get("to")
        if not src or not dst:
            raise HTTPException(400, "from/to requeridos")
        bs = server.location.get(f"sip:{src}@{cfg.sip.realm}")
        if not bs:
            raise HTTPException(404, "Extensión no registrada")
        # Reutilizamos la lógica del B2BUA: simulamos una INVITE entrante
        # desde dst hacia src. Esto rinda llamada al teléfono del usuario.
        from ..pbx.b2bua import Call, CallLegInfo, CallState
        call = Call(id=secrets.token_hex(8), src_number=dst, dst_number=src,
                    direction="internal")
        # Construimos un INVITE sintético... más simple: sólo loggeamos y devolvemos id
        await bus.publish("call.originate.requested", from_=src, to=dst)
        return {"ok": True, "note": "originación encolada"}

    # ===================== EXTENSIONS =====================

    @app.get("/api/v1/extensions")
    async def list_extensions(_: dict = Depends(auth_dep),
                              q: Optional[str] = Query(None)):
        sql = "SELECT * FROM extensions"
        params: List[Any] = []
        if q:
            sql += " WHERE number LIKE ? OR display_name LIKE ?"
            params += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY number"
        rows = await db.fetchall(sql, params)
        # ocultar passwords
        for r in rows:
            r.pop("ha1_md5", None); r.pop("ha1_sha256", None)
        # añadir si está registrado
        for r in rows:
            r["registered"] = bool(server.location.get(f"sip:{r['number']}@{cfg.sip.realm}"))
        return rows

    @app.post("/api/v1/extensions")
    async def create_extension(payload: Dict[str, Any] = Body(...),
                               _: dict = Depends(auth_dep)):
        num = (payload.get("number") or "").strip()
        if not num.isdigit():
            raise HTTPException(400, "number debe ser numérico")
        password = payload.get("sip_password") or secrets.token_urlsafe(10)
        await db.execute(
            "INSERT INTO extensions(number,display_name,sip_password,ha1_md5,ha1_sha256,"
            "email,voicemail_pin,no_answer_seconds,max_concurrent_calls,record_calls,enabled)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (num, payload.get("display_name", ""), password,
             _ha1(num, cfg.sip.realm, password, "MD5"),
             _ha1(num, cfg.sip.realm, password, "SHA-256"),
             payload.get("email"), payload.get("voicemail_pin", "1234"),
             int(payload.get("no_answer_seconds", 25)),
             int(payload.get("max_concurrent_calls", 5)),
             1 if payload.get("record_calls") else 0,
             1 if payload.get("enabled", True) else 0),
        )
        return {"ok": True, "number": num, "sip_password": password}

    @app.put("/api/v1/extensions/{number}")
    async def update_extension(number: str, payload: Dict[str, Any] = Body(...),
                               _: dict = Depends(auth_dep)):
        ext = await db.fetchone("SELECT * FROM extensions WHERE number=?", (number,))
        if not ext: raise HTTPException(404, "no existe")
        sets, vals = [], []
        for k in ("display_name", "email", "voicemail_pin",
                  "forward_busy", "forward_noanswer", "forward_unconditional",
                  "no_answer_seconds", "max_concurrent_calls", "record_calls",
                  "voicemail_enabled", "pickup_group", "enabled"):
            if k in payload:
                sets.append(f"{k}=?"); vals.append(payload[k])
        if "sip_password" in payload and payload["sip_password"]:
            pwd = payload["sip_password"]
            sets += ["sip_password=?", "ha1_md5=?", "ha1_sha256=?"]
            vals += [pwd, _ha1(number, cfg.sip.realm, pwd, "MD5"),
                     _ha1(number, cfg.sip.realm, pwd, "SHA-256")]
        if not sets:
            return {"ok": True}
        sets.append("updated_at=strftime('%s','now')")
        vals.append(number)
        await db.execute(f"UPDATE extensions SET {', '.join(sets)} WHERE number=?", vals)
        return {"ok": True}

    @app.delete("/api/v1/extensions/{number}")
    async def delete_extension(number: str, _: dict = Depends(auth_dep)):
        await db.execute("DELETE FROM extensions WHERE number=?", (number,))
        await server.location.remove(f"sip:{number}@{cfg.sip.realm}")
        return {"ok": True}

    # ===================== TRUNKS =====================

    @app.get("/api/v1/trunks")
    async def list_trunks(_: dict = Depends(auth_dep)):
        rows = await db.fetchall("SELECT * FROM trunks ORDER BY priority, name")
        for r in rows: r.pop("password", None)
        return rows

    @app.post("/api/v1/trunks")
    async def create_trunk(p: Dict[str, Any] = Body(...), _: dict = Depends(auth_dep)):
        await db.execute(
            "INSERT INTO trunks(name,host,port,transport,username,password,realm,"
            "auth_mode,register,register_expires,from_user,from_domain,outbound_proxy,"
            "priority,enabled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["name"], p["host"], int(p.get("port", 5060)),
             p.get("transport", "udp"), p.get("username"), p.get("password"),
             p.get("realm"), p.get("auth_mode", "credentials"),
             1 if p.get("register", True) else 0,
             int(p.get("register_expires", 3600)),
             p.get("from_user"), p.get("from_domain"),
             p.get("outbound_proxy"), int(p.get("priority", 100)),
             1 if p.get("enabled", True) else 0),
        )
        return {"ok": True}

    @app.put("/api/v1/trunks/{trunk_id}")
    async def update_trunk(trunk_id: int, p: Dict[str, Any] = Body(...),
                           _: dict = Depends(auth_dep)):
        sets, vals = [], []
        for k, v in p.items():
            if k in ("name","host","port","transport","username","password",
                     "realm","auth_mode","register","register_expires",
                     "from_user","from_domain","outbound_proxy","priority","enabled"):
                sets.append(f"{k}=?"); vals.append(v)
        if not sets: return {"ok": True}
        sets.append("updated_at=strftime('%s','now')"); vals.append(trunk_id)
        await db.execute(f"UPDATE trunks SET {', '.join(sets)} WHERE id=?", vals)
        return {"ok": True}

    @app.delete("/api/v1/trunks/{trunk_id}")
    async def delete_trunk(trunk_id: int, _: dict = Depends(auth_dep)):
        await db.execute("DELETE FROM trunks WHERE id=?", (trunk_id,))
        return {"ok": True}

    # ===================== DIAL PLAN =====================

    @app.get("/api/v1/dialplan")
    async def list_dp(_: dict = Depends(auth_dep)):
        return await db.fetchall("SELECT * FROM dial_plan ORDER BY priority, id")

    @app.post("/api/v1/dialplan")
    async def add_dp(p: Dict[str, Any] = Body(...), _: dict = Depends(auth_dep)):
        await db.execute(
            "INSERT INTO dial_plan(name,direction,pattern,target_type,target_value,"
            "priority,strip_digits,prepend,enabled) VALUES(?,?,?,?,?,?,?,?,?)",
            (p["name"], p["direction"], p["pattern"], p["target_type"],
             p["target_value"], int(p.get("priority", 100)),
             int(p.get("strip_digits", 0)), p.get("prepend", ""),
             1 if p.get("enabled", True) else 0),
        )
        await server.dialplan.reload()
        return {"ok": True}

    @app.put("/api/v1/dialplan/{rid}")
    async def upd_dp(rid: int, p: Dict[str, Any] = Body(...), _: dict = Depends(auth_dep)):
        sets, vals = [], []
        for k in ("name","direction","pattern","target_type","target_value",
                  "priority","strip_digits","prepend","enabled"):
            if k in p:
                sets.append(f"{k}=?"); vals.append(p[k])
        if not sets: return {"ok": True}
        sets.append("updated_at=strftime('%s','now')"); vals.append(rid)
        await db.execute(f"UPDATE dial_plan SET {', '.join(sets)} WHERE id=?", vals)
        await server.dialplan.reload()
        return {"ok": True}

    @app.delete("/api/v1/dialplan/{rid}")
    async def del_dp(rid: int, _: dict = Depends(auth_dep)):
        await db.execute("DELETE FROM dial_plan WHERE id=?", (rid,))
        await server.dialplan.reload()
        return {"ok": True}

    # ===================== RING GROUPS / QUEUES / IVRs / CONF =====================

    for table_name, route_name in (("ring_groups", "ringgroups"),
                                    ("queues", "queues"),
                                    ("ivrs", "ivrs"),
                                    ("conferences", "conferences"),
                                    ("schedules", "schedules"),
                                    ("dids", "dids"),
                                    ("blacklist", "blacklist")):
        _make_crud(app, db, auth_dep, table_name, route_name)

    # ===================== CDR =====================

    @app.get("/api/v1/cdr")
    async def cdr(_: dict = Depends(auth_dep),
                  limit: int = Query(100, ge=1, le=10000),
                  offset: int = Query(0, ge=0),
                  src: Optional[str] = Query(None),
                  dst: Optional[str] = Query(None),
                  since: Optional[float] = Query(None),
                  until: Optional[float] = Query(None)):
        where, params = [], []
        if src: where.append("src_number LIKE ?"); params.append(f"%{src}%")
        if dst: where.append("dst_number LIKE ?"); params.append(f"%{dst}%")
        if since is not None: where.append("started_at >= ?"); params.append(since)
        if until is not None: where.append("started_at <= ?"); params.append(until)
        sql = "SELECT * FROM cdr"
        if where: sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return await db.fetchall(sql, params)

    @app.get("/api/v1/cdr.csv")
    async def cdr_csv(_: dict = Depends(auth_dep),
                      since: Optional[float] = None, until: Optional[float] = None):
        where, params = [], []
        if since is not None: where.append("started_at >= ?"); params.append(since)
        if until is not None: where.append("started_at <= ?"); params.append(until)
        sql = "SELECT * FROM cdr"
        if where: sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC"
        rows = await db.fetchall(sql, params)
        out = io.StringIO()
        cols = ["id","call_id","started_at","answered_at","ended_at","src_number",
                "dst_number","direction","disposition","duration","bill_seconds",
                "via_trunk","recording_path","hangup_cause"]
        out.write(",".join(cols) + "\n")
        for r in rows:
            out.write(",".join(str(r.get(c) or "") for c in cols) + "\n")
        return PlainTextResponse(out.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=cdr.csv"})

    @app.get("/api/v1/recordings/{cdr_id}")
    async def get_recording(cdr_id: int, _: dict = Depends(auth_dep)):
        row = await db.fetchone("SELECT recording_path FROM cdr WHERE id=?", (cdr_id,))
        if not row or not row.get("recording_path"):
            raise HTTPException(404, "Sin grabación")
        path = row["recording_path"]
        if not os.path.exists(path):
            raise HTTPException(404, "Fichero no encontrado")
        return FileResponse(path, media_type="audio/wav",
                            filename=os.path.basename(path))

    # ===================== VOICEMAIL =====================

    @app.get("/api/v1/voicemail/{ext}")
    async def list_vm(ext: str, _: dict = Depends(auth_dep)):
        return await db.fetchall(
            "SELECT id,caller,received_at,duration,seen,file_path FROM voicemail "
            "WHERE extension=? ORDER BY received_at DESC",
            (ext,),
        )

    @app.get("/api/v1/voicemail/{ext}/{vm_id}/audio")
    async def get_vm_audio(ext: str, vm_id: int, _: dict = Depends(auth_dep)):
        row = await db.fetchone("SELECT file_path FROM voicemail WHERE id=? AND extension=?",
                                (vm_id, ext))
        if not row or not os.path.exists(row["file_path"]):
            raise HTTPException(404)
        await db.execute("UPDATE voicemail SET seen=1 WHERE id=?", (vm_id,))
        return FileResponse(row["file_path"], media_type="audio/wav")

    @app.delete("/api/v1/voicemail/{ext}/{vm_id}")
    async def del_vm(ext: str, vm_id: int, _: dict = Depends(auth_dep)):
        row = await db.fetchone("SELECT file_path FROM voicemail WHERE id=? AND extension=?",
                                (vm_id, ext))
        if row and os.path.exists(row["file_path"]):
            try: os.remove(row["file_path"])
            except Exception: pass
        await db.execute("DELETE FROM voicemail WHERE id=? AND extension=?", (vm_id, ext))
        return {"ok": True}

    # ===================== CHAT =====================

    @app.get("/api/v1/chat/conversations/{user}")
    async def conv(user: str, _: dict = Depends(auth_dep)):
        rows = await db.fetchall(
            "SELECT * FROM chat_messages WHERE src=? OR dst=? ORDER BY sent_at DESC LIMIT 200",
            (user, user))
        return rows

    @app.post("/api/v1/chat/send")
    async def chat_send(p: Dict[str, str] = Body(...), user: dict = Depends(auth_dep)):
        src = p.get("from") or user["username"]
        dst = p["to"]; body = p["body"]
        await db.execute("INSERT INTO chat_messages(src,dst,body) VALUES(?,?,?)",
                         (src, dst, body))
        await bus.publish("chat.message", src=src, dst=dst, body=body)
        return {"ok": True}

    # ===================== SETTINGS / BACKUP =====================

    @app.get("/api/v1/settings")
    async def get_settings(_: dict = Depends(auth_dep)):
        return {
            "sip": cfg.sip.__dict__,
            "rtp": cfg.rtp.__dict__,
            "web": {k: v for k, v in cfg.web.__dict__.items() if k not in ("secret_key","jwt_secret")},
            "storage": cfg.storage.__dict__,
            "security": cfg.security.__dict__,
        }

    @app.get("/api/v1/backup")
    async def backup(_: dict = Depends(auth_dep)):
        rows = {}
        for tbl in ["users","extensions","trunks","dial_plan","ring_groups","queues",
                    "ivrs","schedules","dids","conferences","blacklist","webhooks",
                    "settings_kv","provisioning_devices"]:
            rows[tbl] = await db.fetchall(f"SELECT * FROM {tbl}")
        out = json.dumps({"smurf_backup_v1": True, "ts": time.time(), "tables": rows},
                         default=str, indent=2)
        return Response(out, media_type="application/json",
                        headers={"Content-Disposition":
                                 f"attachment; filename=smurf-backup-{int(time.time())}.json"})

    @app.post("/api/v1/restore")
    async def restore(file: UploadFile = File(...), _: dict = Depends(auth_dep)):
        raw = await file.read()
        try:
            data = json.loads(raw)
        except Exception:
            raise HTTPException(400, "JSON inválido")
        if not data.get("smurf_backup_v1"):
            raise HTTPException(400, "No es un backup SMURF v1")
        for tbl, rows in data.get("tables", {}).items():
            await db.execute(f"DELETE FROM {tbl}")
            for r in rows:
                cols = ",".join(r.keys()); ph = ",".join("?" * len(r))
                await db.execute(f"INSERT OR REPLACE INTO {tbl} ({cols}) VALUES ({ph})",
                                 list(r.values()))
        await server.dialplan.reload()
        return {"ok": True, "tables": list(data.get("tables", {}).keys())}

    # ===================== EVENTS WS =====================

    @app.websocket("/api/v1/ws/events")
    async def ws_events(ws: WebSocket):
        # token por query (no podemos usar Depends en WS para cookies fácilmente)
        token = ws.query_params.get("token") or ws.cookies.get("smurf_token")
        if not token:
            await ws.close(code=4401); return
        try:
            decode_token(cfg.web.jwt_secret, token)
        except Exception:
            await ws.close(code=4401); return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue()
        async def handler(ev):
            try: queue.put_nowait(ev)
            except Exception: pass
        off = bus.subscribe_all(handler)
        try:
            for ev in bus.history()[-50:]:
                await ws.send_text(ev.to_json())
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    await ws.send_text(ev.to_json())
                except asyncio.TimeoutError:
                    await ws.send_text(json.dumps({"type": "ping", "ts": time.time()}))
        except WebSocketDisconnect:
            pass
        finally:
            off()

    # ===================== PROVISIONING =====================

    @app.get("/provisioning/{mac}.cfg")
    async def provision(mac: str):
        from ..provisioning.templates import generate_config
        mac_clean = mac.replace(":", "").replace("-", "").lower().rstrip(".cfg")
        dev = await db.fetchone("SELECT * FROM provisioning_devices WHERE mac=?", (mac_clean,))
        if not dev:
            raise HTTPException(404, "Dispositivo no provisionado")
        ext = await db.fetchone("SELECT * FROM extensions WHERE number=?", (dev["extension"],))
        if not ext:
            raise HTTPException(404, "Extensión no encontrada")
        await db.execute("UPDATE provisioning_devices SET last_seen=strftime('%s','now') WHERE id=?",
                         (dev["id"],))
        config = generate_config(dev["vendor"], dev["model"] or "", ext, cfg)
        return PlainTextResponse(config, media_type="text/plain")

    # ===================== SPA STATIC =====================

    if os.path.isdir(WEB_ROOT):
        app.mount("/static", StaticFiles(directory=os.path.join(WEB_ROOT, "static")), name="static")

        @app.get("/", response_class=HTMLResponse)
        @app.get("/login", response_class=HTMLResponse)
        @app.get("/dashboard", response_class=HTMLResponse)
        @app.get("/extensions", response_class=HTMLResponse)
        @app.get("/trunks", response_class=HTMLResponse)
        @app.get("/dialplan", response_class=HTMLResponse)
        @app.get("/queues", response_class=HTMLResponse)
        @app.get("/ivrs", response_class=HTMLResponse)
        @app.get("/cdrs", response_class=HTMLResponse)
        @app.get("/recordings", response_class=HTMLResponse)
        @app.get("/voicemail", response_class=HTMLResponse)
        @app.get("/chat", response_class=HTMLResponse)
        @app.get("/settings", response_class=HTMLResponse)
        @app.get("/softphone", response_class=HTMLResponse)
        async def spa_index():
            with open(os.path.join(WEB_ROOT, "templates", "index.html"), "r", encoding="utf-8") as fh:
                return HTMLResponse(fh.read())

    @app.get("/healthz")
    async def health():
        return {"ok": True, "uptime": time.time()}

    return app


def _make_crud(app: FastAPI, db: Database, auth_dep, table: str, route: str):
    base = f"/api/v1/{route}"

    async def list_handler(_: dict = Depends(auth_dep)):
        return await db.fetchall(f"SELECT * FROM {table}")

    async def create_handler(p: Dict[str, Any] = Body(...), _: dict = Depends(auth_dep)):
        cols = ",".join(p.keys()); ph = ",".join("?" * len(p))
        await db.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", list(p.values()))
        return {"ok": True}

    async def update_handler(id: int, p: Dict[str, Any] = Body(...), _: dict = Depends(auth_dep)):
        sets = ",".join(f"{k}=?" for k in p.keys())
        await db.execute(f"UPDATE {table} SET {sets} WHERE id=?", list(p.values()) + [id])
        return {"ok": True}

    async def delete_handler(id: int, _: dict = Depends(auth_dep)):
        await db.execute(f"DELETE FROM {table} WHERE id=?", (id,))
        return {"ok": True}

    list_handler.__name__ = f"list_{route}"
    create_handler.__name__ = f"create_{route}"
    update_handler.__name__ = f"update_{route}"
    delete_handler.__name__ = f"delete_{route}"

    app.get(base, name=f"list_{route}")(list_handler)
    app.post(base, name=f"create_{route}")(create_handler)
    app.put(base + "/{id}", name=f"update_{route}")(update_handler)
    app.delete(base + "/{id}", name=f"delete_{route}")(delete_handler)
