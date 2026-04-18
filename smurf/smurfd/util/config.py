"""Carga y gestión centralizada de la configuración de SMURF.

La configuración se lee de un fichero JSON (por defecto /etc/smurf/smurf.json
o ./config/smurf.json en desarrollo) y se complementa con valores por
defecto. Los cambios en la base de datos (extensiones, trunks, dial plan)
se gestionan por el módulo db; este fichero sólo cubre parámetros estáticos
de arranque (puertos, rutas, secretos).
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field, asdict
from typing import List, Optional


def _default_config_path() -> str:
    for p in ("/etc/smurf/smurf.json", os.path.abspath("config/smurf.json")):
        if os.path.exists(p):
            return p
    return os.path.abspath("config/smurf.json")


@dataclass
class SipConfig:
    udp_bind: str = "0.0.0.0"
    udp_port: int = 5060
    tcp_bind: str = "0.0.0.0"
    tcp_port: int = 5060
    tls_bind: str = "0.0.0.0"
    tls_port: int = 5061
    ws_bind: str = "0.0.0.0"
    ws_port: int = 5062
    wss_bind: str = "0.0.0.0"
    wss_port: int = 5063
    public_ip: Optional[str] = None
    realm: str = "smurf.local"
    user_agent: str = "SMURF-PBX/1.0"
    max_forwards: int = 70
    registration_min_expires: int = 60
    registration_default_expires: int = 3600
    registration_max_expires: int = 7200
    transactions_t1_ms: int = 500
    transactions_t2_ms: int = 4000
    transactions_t4_ms: int = 5000


@dataclass
class RtpConfig:
    bind: str = "0.0.0.0"
    port_min: int = 16384
    port_max: int = 32767
    dscp: int = 46
    max_silence_ms: int = 30000
    enable_srtp: bool = True


@dataclass
class WebConfig:
    bind: str = "0.0.0.0"
    http_port: int = 5000
    https_port: int = 5001
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    secret_key: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    jwt_secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    session_hours: int = 12


@dataclass
class StorageConfig:
    db_path: str = "/var/lib/smurf/smurf.db"
    recordings_dir: str = "/var/lib/smurf/recordings"
    voicemail_dir: str = "/var/lib/smurf/voicemail"
    sounds_dir: str = "/var/lib/smurf/sounds"
    provisioning_dir: str = "/var/lib/smurf/provisioning"
    log_dir: str = "/var/log/smurf"


@dataclass
class SecurityConfig:
    fail2ban_window_sec: int = 300
    fail2ban_max_failures: int = 8
    fail2ban_ban_sec: int = 3600
    rate_limit_per_ip_pps: int = 200
    enable_2fa_admin: bool = False


@dataclass
class SmurfConfig:
    sip: SipConfig = field(default_factory=SipConfig)
    rtp: RtpConfig = field(default_factory=RtpConfig)
    web: WebConfig = field(default_factory=WebConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    log_level: str = "INFO"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _merge(base: dict, overlay: dict) -> dict:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Optional[str] = None) -> SmurfConfig:
    cfg = SmurfConfig()
    p = path or _default_config_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            base = json.loads(cfg.to_json())
            merged = _merge(base, user)
            cfg = SmurfConfig(
                sip=SipConfig(**merged.get("sip", {})),
                rtp=RtpConfig(**merged.get("rtp", {})),
                web=WebConfig(**merged.get("web", {})),
                storage=StorageConfig(**merged.get("storage", {})),
                security=SecurityConfig(**merged.get("security", {})),
                log_level=merged.get("log_level", "INFO"),
            )
        except Exception as exc:
            raise RuntimeError(f"No se pudo leer {p}: {exc}") from exc
    return cfg


def save_config(cfg: SmurfConfig, path: Optional[str] = None) -> str:
    p = path or _default_config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(cfg.to_json())
    return p
