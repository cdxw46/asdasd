from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PBX_PATH = ROOT / "services" / "pbx-core" / "main.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("smurf_pbx_core_main", PBX_PATH)
assert spec and spec.loader
pbx_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pbx_module
spec.loader.exec_module(pbx_module)
PBXCoreService = pbx_module.PBXCoreService


def _write_cfg(path: Path, db_path: Path) -> None:
    path.write_text(
        f"""
global:
  domain: smurf.local
  log_level: INFO
  max_global_calls: 20
security:
  jwt_secret: test-secret
sip:
  registration_min_expires: 60
  registration_max_expires: 3600
rtp:
  recording_path: "{db_path.parent}/recordings"
database:
  sqlite_path: "{db_path}"
provisioning:
  templates_path: "{ROOT}/provisioning-templates"
bus:
  pbx_command_host: "127.0.0.1"
  pbx_command_port: 19101
""".strip(),
        encoding="utf-8",
    )


def _service(tmp_path: Path) -> PBXCoreService:
    cfg = tmp_path / "config.yml"
    db = tmp_path / "smurf.db"
    _write_cfg(cfg, db)
    return PBXCoreService(str(cfg))


def test_route_call_is_idempotent_for_same_call_id(tmp_path: Path):
    svc = _service(tmp_path)
    first = svc._route_call("call-1", "1000", "1001")
    second = svc._route_call("call-1", "1000", "1001")
    assert first.status == "ok"
    assert second.status == "ok"
    active = svc.db.list_active_calls()
    assert len([c for c in active if c["call_id"] == "call-1"]) == 1


def test_reject_self_call(tmp_path: Path):
    svc = _service(tmp_path)
    decision = svc._route_call("call-self", "1000", "1000")
    assert decision.status == "error"
    assert decision.reason == "loop_call_forbidden"


def test_chat_send_too_long_message_is_rejected(tmp_path: Path):
    svc = _service(tmp_path)
    result = pbx_module.asyncio.run(
        svc._handle_command(
            {
                "action": "chat_send",
                "from_ext": "1000",
                "to_ext": "1001",
                "message": "x" * 5000,
            }
        )
    )
    assert result["ok"] is False
    assert result["error"] == "chat_message_too_long"


def test_presence_status_validation(tmp_path: Path):
    svc = _service(tmp_path)
    result = pbx_module.asyncio.run(
        svc._handle_command(
            {
                "action": "set_presence",
                "extension": "1000",
                "status": "invalid-status",
            }
        )
    )
    assert result["ok"] is False
    assert result["error"] == "unknown_presence_status"

