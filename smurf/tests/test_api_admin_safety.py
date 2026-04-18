from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
API_MAIN = ROOT / "services" / "api-admin" / "main.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("smurf_api_admin_main", API_MAIN)
assert spec and spec.loader
api_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api_module
spec.loader.exec_module(api_module)
AdminService = api_module.AdminService


class _FakeBusClient:
    async def request(self, payload):
        action = str(payload.get("action", ""))
        if action == "ping":
            return {"ok": True}
        if action == "route_call":
            return {"ok": True, "status": "ok", "target_extension": payload.get("to_ext")}
        return {"ok": True}


def _build_service(tmp_path: Path) -> AdminService:
    cfg = f"""
global:
  domain: smurf.local
  log_level: INFO
security:
  jwt_secret: "test-secret-0123456789abcdef0123456789abcdef"
  access_token_minutes: 30
database:
  sqlite_path: "{tmp_path / 'smurf.db'}"
rtp:
  recording_path: "{tmp_path / 'recordings'}"
provisioning:
  templates_path: "{tmp_path / 'tpl'}"
  base_url: "https://127.0.0.1:5001/provisioning"
api:
  host: "127.0.0.1"
  port: 5001
  cors_origins: ["*"]
"""
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(cfg, encoding="utf-8")
    (tmp_path / "recordings").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tpl").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tpl" / "yealink.tpl").write_text(
        "line={EXTENSION}-{DISPLAY_NAME}-{AUTH_USER}-{AUTH_PASS}-{SIP_SERVER}-{SIP_UDP_PORT}-{SIP_TLS_PORT}-{PROVISIONING_URL}\n",
        encoding="utf-8",
    )
    svc = AdminService(config_path=str(cfg_path))
    fake = _FakeBusClient()
    svc.pbx_client = fake
    svc.media_client = fake
    svc.sip_client = fake
    return svc


def _admin_token(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "smurfadmin"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_recording_download_rejects_path_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = _build_service(Path(tmp))
        client = TestClient(svc.app)
        token = _admin_token(client)
        response = client.get(
            "/api/v1/recordings/download",
            params={"path": "/etc/passwd"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400


def test_backup_and_restore_use_safe_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = _build_service(Path(tmp))
        client = TestClient(svc.app)
        token = _admin_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        backup_resp = client.post("/api/v1/backup", headers=headers)
        assert backup_resp.status_code == 200, backup_resp.text
        backup_path = backup_resp.json()["path"]
        assert Path(backup_path).exists()

        restore_resp = client.post(
            "/api/v1/restore",
            params={"backup_path": backup_path},
            headers=headers,
        )
        assert restore_resp.status_code == 200, restore_resp.text

        bad_restore = client.post(
            "/api/v1/restore",
            params={"backup_path": "/etc/passwd"},
            headers=headers,
        )
        assert bad_restore.status_code == 400


def test_log_limit_is_clamped_and_vendor_is_sanitized(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log_file = root / "smurf.log"
        lines = [f"line-{i}" for i in range(20)]
        log_file.write_text("\n".join(lines), encoding="utf-8")
        monkeypatch.setenv("SMURF_LOG_PATH", str(log_file))

        svc = _build_service(root)
        client = TestClient(svc.app)
        token = _admin_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        logs = client.get("/api/v1/logs", params={"limit": 10_000}, headers=headers)
        assert logs.status_code == 200
        assert len(logs.json()["items"]) == len(lines)

        bad_vendor = client.get(
            "/api/v1/provisioning/template/../../etc",
            headers=headers,
        )
        assert bad_vendor.status_code in {400, 404}


def test_backup_file_is_valid_sqlite_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = _build_service(Path(tmp))
        client = TestClient(svc.app)
        token = _admin_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        backup_resp = client.post("/api/v1/backup", headers=headers)
        backup_path = Path(backup_resp.json()["path"])
        with sqlite3.connect(backup_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            assert row is not None
            assert int(row[0]) >= 1
