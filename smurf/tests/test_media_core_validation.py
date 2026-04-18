from __future__ import annotations

import asyncio
import importlib.util
import struct
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


def test_recording_accepts_symmetric_rtp_port_rebinding(tmp_path: Path):
    service = _service(tmp_path)
    call_id = "call-media-symmetric"
    create_result = asyncio.run(
        service._handle_command(
            {
                "action": "create_session",
                "call_id": call_id,
                "peer_a": {"ip": "127.0.0.1", "rtp_port": 34010},
                "peer_b": {"ip": "127.0.0.1", "rtp_port": 34012},
                "record": True,
            }
        )
    )
    assert create_result["ok"] is True

    packet = struct.pack("!BBHII", 0x80, 0, 1, 160, 0x12345678) + (b"\x00" * 160)
    asyncio.run(service._handle_rtp_packet(packet, ("127.0.0.1", 34999)))
    asyncio.run(service._handle_command({"action": "end_session", "call_id": call_id}))

    recording_path = tmp_path / "recordings" / f"{call_id}.rtp"
    assert recording_path.exists()
    assert recording_path.stat().st_size > 0
    assert service.endpoint_index == {}
    service.udp_socket.close()


def test_unknown_source_port_ignored_when_multiple_calls_share_ip(tmp_path: Path):
    service = _service(tmp_path)
    first = asyncio.run(
        service._handle_command(
            {
                "action": "create_session",
                "call_id": "call-media-1",
                "peer_a": {"ip": "127.0.0.1", "rtp_port": 34020},
                "peer_b": {"ip": "127.0.0.1", "rtp_port": 34022},
                "record": True,
            }
        )
    )
    second = asyncio.run(
        service._handle_command(
            {
                "action": "create_session",
                "call_id": "call-media-2",
                "peer_a": {"ip": "127.0.0.1", "rtp_port": 34024},
                "peer_b": {"ip": "127.0.0.1", "rtp_port": 34026},
                "record": True,
            }
        )
    )
    assert first["ok"] is True
    assert second["ok"] is True

    packet = struct.pack("!BBHII", 0x80, 0, 2, 320, 0x22334455) + (b"\x01" * 120)
    asyncio.run(service._handle_rtp_packet(packet, ("127.0.0.1", 34998)))
    assert ("127.0.0.1", 34998) not in service.endpoint_index

    asyncio.run(service._handle_command({"action": "end_session", "call_id": "call-media-1"}))
    asyncio.run(service._handle_command({"action": "end_session", "call_id": "call-media-2"}))

    first_path = tmp_path / "recordings" / "call-media-1.rtp"
    second_path = tmp_path / "recordings" / "call-media-2.rtp"
    assert first_path.exists() and first_path.stat().st_size == 0
    assert second_path.exists() and second_path.stat().st_size == 0
    service.udp_socket.close()
