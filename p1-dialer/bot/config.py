"""Carga y valida la configuracion desde variables de entorno."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Falta la variable de entorno obligatoria: {name}")
    return val


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                ids.add(int(chunk))
            except ValueError:
                pass
    return ids


@dataclass
class Config:
    telegram_token: str
    allowed_user_ids: set[int]

    ami_host: str
    ami_port: int
    ami_user: str
    ami_secret: str

    transfer_number: str
    default_country_code: str
    max_concurrent: int
    dial_timeout: int
    dtmf_timeout: int

    trunk_endpoint: str = "narayana"
    ivr_context: str = "ivr-outbound"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_token=_get("TELEGRAM_BOT_TOKEN"),
            allowed_user_ids=_parse_ids(os.environ.get("ALLOWED_USER_IDS", "")),
            ami_host=os.environ.get("AMI_HOST", "127.0.0.1"),
            ami_port=int(os.environ.get("AMI_PORT", "5038")),
            ami_user=os.environ.get("AMI_USER", "p1bot"),
            ami_secret=_get("AMI_SECRET"),
            transfer_number=_get("TRANSFER_NUMBER"),
            default_country_code=os.environ.get("DEFAULT_COUNTRY_CODE", "34"),
            max_concurrent=int(os.environ.get("MAX_CONCURRENT", "1")),
            dial_timeout=int(os.environ.get("DIAL_TIMEOUT", "30")),
            dtmf_timeout=int(os.environ.get("DTMF_TIMEOUT", "8")),
        )
