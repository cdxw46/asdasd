from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEDIA_PATH = ROOT / "services" / "media-core" / "main.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("smurf_media_core_main", MEDIA_PATH)
assert spec and spec.loader
media_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = media_module
spec.loader.exec_module(media_module)
MediaCoreService = media_module.MediaCoreService


def _write_cfg(path: Path, db_path: Path, recording_path: Path) -> None:
    path.write_text(
        f"""
global:
  domain: smurf.local
  log_level: INFO
database:
  sqlite_path: "{db_path}"
rtp:
  bind_host: "127.0.0.1"
  min_port: 34000
  max_port: 34100
  recording_path: "{recording_path}"
provisioning:
  templates_path: "{ROOT}/provisioning-templates"
bus:
  media_command_host: "127.0.0.1"
  media_command_port: 19201
""".strip(),
        encoding="utf-8",
    )


def _service(tmp_path: Path) -> MediaCoreService:
    cfg_path = tmp_path / "config.yml"
    db_path = tmp_path / "smurf.db"
    recording_path = tmp_path / "recordings"
    _write_cfg(cfg_path, db_path, recording_path)
    return MediaCoreService(config_path=str(cfg_path))


def test_parse_peer_requires_valid_payload(tmp_path: Path):
    service = _service(tmp_path)
    bad_payload = {"peer_a": {"ip": "127.0.0.1", "rtp_port": "abc"}}
    try:
        service._parse_peer(bad_payload, "peer_a")
        assert False, "expected ValueError for non-numeric port"
    except ValueError as exc:
        assert "rtp_port must be numeric" in str(exc)
    finally:
        service.udp_socket.close()


def test_create_session_rejects_invalid_peer(tmp_path: Path):
    service = _service(tmp_path)
    result = asyncio.run(
        service._handle_command(
            {
                "action": "create_session",
                "call_id": "call-media-invalid",
                "peer_a": {"ip": "127.0.0.1", "rtp_port": "abc"},
                "peer_b": {"ip": "127.0.0.1", "rtp_port": 34020},
            }
        )
    )
    assert result["ok"] is False
    assert "rtp_port must be numeric" in result["error"]
    service.udp_socket.close()
