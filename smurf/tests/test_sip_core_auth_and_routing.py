from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from core.sip import SIPMessage, build_response, digest_response


def _load_sip_core_module():
    module_path = Path(__file__).resolve().parents[1] / "services" / "sip-core" / "main.py"
    spec = importlib.util.spec_from_file_location("smurf_sip_core_main", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _register_request(extension: str, call_id: str, contact_port: int = 52000) -> SIPMessage:
    msg = SIPMessage(method="REGISTER", request_uri="sip:smurf.local")
    msg.add_header("Via", "SIP/2.0/UDP 127.0.0.1:52000;branch=z9hG4bK-test")
    msg.add_header("From", f"<sip:{extension}@smurf.local>;tag=fromtag")
    msg.add_header("To", f"<sip:{extension}@smurf.local>")
    msg.add_header("Call-Id", call_id)
    msg.add_header("Cseq", "1 REGISTER")
    msg.add_header("Contact", f"<sip:{extension}@127.0.0.1:{contact_port}>")
    msg.add_header("Max-Forwards", "70")
    msg.add_header("Content-Length", "0")
    return msg


def _invite_request(from_ext: str, to_ext: str, call_id: str) -> SIPMessage:
    msg = SIPMessage(method="INVITE", request_uri=f"sip:{to_ext}@smurf.local")
    msg.add_header("Via", "SIP/2.0/UDP 127.0.0.1:53000;branch=z9hG4bK-invite")
    msg.add_header("From", f"<sip:{from_ext}@smurf.local>;tag=fromtag")
    msg.add_header("To", f"<sip:{to_ext}@smurf.local>")
    msg.add_header("Call-Id", call_id)
    msg.add_header("Cseq", "1 INVITE")
    msg.add_header("Contact", f"<sip:{from_ext}@127.0.0.1:53000>")
    msg.add_header("Content-Length", "0")
    return msg


def test_validate_register_auth_success(tmp_path):
    sip_core = _load_sip_core_module()
    db_path = tmp_path / "smurf.db"
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "\n".join(
            [
                "database:",
                f"  sqlite_path: \"{db_path}\"",
                "security:",
                "  sip_realm: \"smurf.local\"",
            ]
        ),
        encoding="utf-8",
    )

    service = sip_core.SIPCoreService(str(cfg_path))
    nonce = service.nonces.create_nonce()
    username = "1000"
    uri = "sip:smurf.local"
    response = digest_response(
        username=username,
        realm=service.config.security.sip_realm,
        password="smurf1000",
        method="REGISTER",
        uri=uri,
        nonce=nonce,
        nc="00000001",
        cnonce="abcdef01",
        qop="auth",
        algorithm="MD5",
    )

    request = _register_request("1000", "register-auth-ok")
    request.add_header(
        "Authorization",
        (
            f'Digest username="{username}", realm="{service.config.security.sip_realm}", '
            f'nonce="{nonce}", uri="{uri}", response="{response}", algorithm=MD5, '
            'qop=auth, nc=00000001, cnonce="abcdef01"'
        ),
    )

    valid, detail = service._validate_register_auth(request)
    assert valid is True
    assert detail == "1000"


def test_validate_register_auth_rejects_bad_digest(tmp_path):
    sip_core = _load_sip_core_module()
    db_path = tmp_path / "smurf.db"
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "\n".join(
            [
                "database:",
                f"  sqlite_path: \"{db_path}\"",
            ]
        ),
        encoding="utf-8",
    )
    service = sip_core.SIPCoreService(str(cfg_path))
    nonce = service.nonces.create_nonce()
    request = _register_request("1000", "register-auth-bad")
    request.add_header(
        "Authorization",
        (
            f'Digest username="1000", realm="{service.config.security.sip_realm}", '
            f'nonce="{nonce}", uri="sip:smurf.local", response="bad", algorithm=MD5, '
            'qop=auth, nc=00000001, cnonce="abcdef01"'
        ),
    )

    valid, detail = service._validate_register_auth(request)
    assert valid is False
    assert detail == "digest_mismatch"


def test_transaction_cache_returns_same_response(tmp_path):
    sip_core = _load_sip_core_module()
    db_path = tmp_path / "smurf.db"
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "\n".join(
            [
                "database:",
                f"  sqlite_path: \"{db_path}\"",
            ]
        ),
        encoding="utf-8",
    )
    service = sip_core.SIPCoreService(str(cfg_path))
    request = _invite_request("1000", "1001", "invite-cache")
    response = build_response(request, 404, "Not Found")
    service._cache_transaction_response(request, response, ttl_seconds=30)

    cached = service._cached_transaction_response(request)
    assert cached is not None
    assert cached.status_code == 404

