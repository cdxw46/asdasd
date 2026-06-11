"""Runtime configuration, loaded from environment variables.

All telephony secrets (SIP password, bot token) come from the environment
so nothing sensitive lives in the repo. See ``.env.example``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - configuration error
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _get_id_set(name: str) -> set[int]:
    raw = os.getenv(name, "")
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:  # pragma: no cover - configuration error
            raise RuntimeError(f"{name} contains a non-numeric id: {chunk!r}") from exc
    return ids


DEFAULT_MESSAGE = (
    "Le llamamos de su proveedor de servicios. Hemos detectado actividad "
    "inusual en su cuenta. Si no ha sido usted, pulse uno para hablar con "
    "un agente. Si ha sido usted, puede colgar."
)


@dataclass
class Config:
    # Telegram
    telegram_token: str = field(default_factory=lambda: _get("TELEGRAM_BOT_TOKEN", required=True))
    allowed_user_ids: set[int] = field(default_factory=lambda: _get_id_set("TELEGRAM_ALLOWED_USERS"))

    # ARI / Asterisk
    ari_base_url: str = field(default_factory=lambda: _get("ARI_BASE_URL", "http://asterisk:8088"))
    ari_username: str = field(default_factory=lambda: _get("ARI_USERNAME", "piensa"))
    ari_password: str = field(default_factory=lambda: _get("ARI_PASSWORD", required=True))
    stasis_app: str = field(default_factory=lambda: _get("ARI_APP", "outbound"))

    # Trunk / dialing
    sip_endpoint: str = field(default_factory=lambda: _get("SIP_ENDPOINT", "narayana-endpoint"))
    caller_id: str = field(default_factory=lambda: _get("CALLER_ID", "34680540787"))
    # Where pressing "1" is sent:
    #   "sip"    -> a SIP softphone (Zoiper/PortSIP) registered as AGENT_ENDPOINT
    #   "number" -> an external PSTN number (AGENT_NUMBER) via the trunk
    agent_mode: str = field(default_factory=lambda: _get("AGENT_MODE", "sip").lower())
    agent_endpoint: str = field(default_factory=lambda: _get("AGENT_ENDPOINT", "agente1"))
    agent_number: str = field(default_factory=lambda: _get("AGENT_NUMBER", ""))

    # Campaign behaviour
    max_concurrent_calls: int = field(default_factory=lambda: _get_int("MAX_CONCURRENT_CALLS", 5))
    call_timeout: int = field(default_factory=lambda: _get_int("CALL_TIMEOUT", 30))
    ivr_max_repeats: int = field(default_factory=lambda: _get_int("IVR_MAX_REPEATS", 2))
    ivr_input_timeout: int = field(default_factory=lambda: _get_int("IVR_INPUT_TIMEOUT", 8))
    default_country_code: str = field(default_factory=lambda: _get("DEFAULT_COUNTRY_CODE", "34"))

    # Message / TTS
    message_text: str = field(default_factory=lambda: _get("MESSAGE_TEXT", DEFAULT_MESSAGE))
    tts_lang: str = field(default_factory=lambda: _get("TTS_LANG", "es"))
    # Directory shared with Asterisk (mounted at /var/lib/asterisk/sounds/custom).
    sounds_dir: str = field(default_factory=lambda: _get("SOUNDS_DIR", "/sounds"))
    sound_name: str = field(default_factory=lambda: _get("SOUND_NAME", "piensa-aviso"))

    @property
    def ari_rest_url(self) -> str:
        return self.ari_base_url.rstrip("/") + "/ari"

    @property
    def agent_dial(self) -> str:
        """Asterisk endpoint string used to reach the agent on transfer."""
        if self.agent_mode == "number":
            num = self.agent_number.lstrip("+")
            return f"PJSIP/{num}@{self.sip_endpoint}"
        # SIP softphone: dial all registered contacts of the agent endpoint.
        return f"PJSIP/{self.agent_endpoint}"

    @property
    def agent_display(self) -> str:
        if self.agent_mode == "number":
            return self.agent_number or "(sin número)"
        return f"softphone SIP «{self.agent_endpoint}»"

    @property
    def sound_media(self) -> str:
        # As referenced by Asterisk: sound:custom/<name>
        return f"sound:custom/{self.sound_name}"


def load_config() -> Config:
    return Config()
