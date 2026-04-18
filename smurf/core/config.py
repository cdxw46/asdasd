"""Configuration model and YAML loader for SMURF services."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("/etc/smurf/config.yml")


@dataclass(slots=True)
class GlobalConfig:
    domain: str = "smurf.local"
    timezone: str = "UTC"
    default_language: str = "es"
    log_level: str = "INFO"
    media_public_ip: str = "127.0.0.1"
    max_global_calls: int = 500


@dataclass(slots=True)
class SecurityConfig:
    jwt_secret: str = "change-me-smurf-secret"
    access_token_minutes: int = 30
    refresh_token_minutes: int = 1440
    require_tls_for_sip: bool = False
    sip_realm: str = "smurf.local"
    failed_auth_block_threshold: int = 10
    failed_auth_window_seconds: int = 300
    block_duration_seconds: int = 1800
    web_2fa_issuer: str = "SMURF PBX"
    sip_rate_limit_per_ip: int = 40
    sip_rate_window_seconds: int = 1


@dataclass(slots=True)
class SIPConfig:
    udp_host: str = "0.0.0.0"
    udp_port: int = 5060
    tcp_host: str = "0.0.0.0"
    tcp_port: int = 5060
    tls_host: str = "0.0.0.0"
    tls_port: int = 5061
    ws_host: str = "0.0.0.0"
    ws_port: int = 5062
    tls_cert_path: str = "/etc/smurf/tls/server.crt"
    tls_key_path: str = "/etc/smurf/tls/server.key"
    allowed_transports: list[str] = field(
        default_factory=lambda: ["UDP", "TCP", "TLS", "WS"]
    )
    registration_min_expires: int = 60
    registration_max_expires: int = 3600
    qualify_interval_seconds: int = 30


@dataclass(slots=True)
class RTPConfig:
    bind_host: str = "0.0.0.0"
    min_port: int = 20000
    max_port: int = 40000
    srtp_enabled: bool = False
    jitter_buffer_ms: int = 60
    dscp: int = 46
    rtcp_interval_seconds: int = 5
    recording_path: str = "/var/lib/smurf/recordings"
    moh_path: str = "/var/lib/smurf/moh"


@dataclass(slots=True)
class APIConfig:
    host: str = "0.0.0.0"
    port: int = 5001
    tls_cert_path: str = "/etc/smurf/tls/server.crt"
    tls_key_path: str = "/etc/smurf/tls/server.key"
    cors_origins: list[str] = field(default_factory=lambda: ["*"])


@dataclass(slots=True)
class DatabaseConfig:
    sqlite_path: str = "/var/lib/smurf/smurf.db"


@dataclass(slots=True)
class ProvisioningConfig:
    host: str = "0.0.0.0"
    port: int = 8088
    templates_path: str = "/etc/smurf/provisioning/templates"
    base_url: str = "https://127.0.0.1:5001/provisioning"


@dataclass(slots=True)
class BusConfig:
    pbx_event_host: str = "127.0.0.1"
    pbx_event_port: int = 9100
    pbx_command_host: str = "127.0.0.1"
    pbx_command_port: int = 9101
    media_command_host: str = "127.0.0.1"
    media_command_port: int = 9201
    sip_command_host: str = "127.0.0.1"
    sip_command_port: int = 9301


@dataclass(slots=True)
class SmurfConfig:
    environment: str = "dev"
    global_: GlobalConfig = field(default_factory=GlobalConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    sip: SIPConfig = field(default_factory=SIPConfig)
    rtp: RTPConfig = field(default_factory=RTPConfig)
    api: APIConfig = field(default_factory=APIConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    provisioning: ProvisioningConfig = field(default_factory=ProvisioningConfig)
    bus: BusConfig = field(default_factory=BusConfig)


def _merge_dataclass(dc: Any, values: dict[str, Any]) -> Any:
    for key, value in values.items():
        target_key = "global_" if key == "global" else key
        if not hasattr(dc, target_key):
            continue
        current = getattr(dc, target_key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(dc, target_key, value)
    return dc


def load_config(path: str | Path | None = None) -> SmurfConfig:
    cfg = SmurfConfig()
    config_path = Path(path or os.environ.get("SMURF_CONFIG", DEFAULT_CONFIG_PATH))
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Invalid config format in {config_path}")
        _merge_dataclass(cfg, data)
    return cfg
