"""Minimal but practical SIP primitives and parser utilities for SMURF."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

SIP_VERSION = "SIP/2.0"
MAX_CONTENT_LENGTH = 1024 * 1024


def _normalize_header_name(name: str) -> str:
    return "-".join(part.capitalize() for part in name.strip().split("-"))


@dataclass
class SIPMessage:
    method: Optional[str] = None
    request_uri: Optional[str] = None
    status_code: Optional[int] = None
    reason: Optional[str] = None
    version: str = SIP_VERSION
    headers: Dict[str, List[str]] = field(default_factory=dict)
    body: str = ""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    def add_header(self, name: str, value: str) -> None:
        name = _normalize_header_name(name)
        self.headers.setdefault(name, []).append(value.strip())

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.headers.get(_normalize_header_name(name))
        if not values:
            return default
        return values[0]

    def get_all(self, name: str) -> List[str]:
        return self.headers.get(_normalize_header_name(name), [])

    def to_bytes(self) -> bytes:
        if self.is_request:
            start_line = f"{self.method} {self.request_uri} {self.version}"
        else:
            start_line = f"{self.version} {self.status_code} {self.reason}"

        lines = [start_line]
        body_bytes = self.body.encode("utf-8")

        # Ensure Content-Length consistency
        headers = {k: list(v) for k, v in self.headers.items()}
        headers["Content-Length"] = [str(len(body_bytes))]

        for name, values in headers.items():
            for value in values:
                lines.append(f"{name}: {value}")

        lines.append("")
        payload = "\r\n".join(lines).encode("utf-8") + b"\r\n" + body_bytes
        return payload


def parse_sip_message(raw_data: bytes) -> SIPMessage:
    messages, remainder = split_sip_messages(raw_data)
    if remainder:
        raise ValueError("incomplete_sip_message")
    if not messages:
        raise ValueError("Empty SIP message")
    if len(messages) > 1:
        raise ValueError("parse_sip_message expects a single SIP message")

    data = messages[0].decode("utf-8", errors="replace")
    sep_len = 4
    split_index = data.find("\r\n\r\n")
    if split_index == -1:
        split_index = data.find("\n\n")
        sep_len = 2
    if split_index == -1:
        raise ValueError("invalid_sip_message_missing_header_terminator")
    header_part = data[:split_index]
    body = data[split_index + sep_len :]

    raw_lines = header_part.splitlines()
    if not raw_lines:
        raise ValueError("empty_sip_header")
    lines = [line.rstrip("\r") for line in raw_lines]

    start = lines[0]
    msg = SIPMessage(body=body)
    if start.startswith("SIP/2.0"):
        parts = start.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError(f"Invalid status line: {start}")
        msg.version = parts[0]
        msg.status_code = int(parts[1])
        msg.reason = parts[2] if len(parts) > 2 else ""
    else:
        parts = start.split(" ", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid request line: {start}")
        msg.method = parts[0].upper()
        msg.request_uri = parts[1]
        msg.version = parts[2]
        if msg.version != SIP_VERSION:
            raise ValueError(f"Unsupported SIP version: {msg.version}")

    current_header = None
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith((" ", "\t")) and current_header:
            # Header folding continuation
            msg.headers[current_header][-1] += f" {line.strip()}"
            continue
        if ":" not in line:
            raise ValueError(f"invalid_header_line: {line}")
        key, value = line.split(":", 1)
        key = _normalize_header_name(key)
        current_header = key
        msg.add_header(key, value.strip())

    if msg.is_request:
        for required in ("Via", "From", "To", "Call-Id", "Cseq"):
            if not msg.get(required):
                raise ValueError(f"missing_required_header: {required}")

    return msg


def _find_header_terminator(data: bytes, start: int = 0) -> tuple[int, int] | None:
    idx = data.find(b"\r\n\r\n", start)
    if idx != -1:
        return idx, 4
    idx = data.find(b"\n\n", start)
    if idx != -1:
        return idx, 2
    return None


def _extract_content_length(header_blob: bytes) -> int:
    header_text = header_blob.decode("utf-8", errors="replace")
    for line in header_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() in {"content-length", "l"}:
            raw = value.strip()
            if not raw.isdigit():
                raise ValueError("invalid_content_length")
            amount = int(raw)
            if amount < 0 or amount > MAX_CONTENT_LENGTH:
                raise ValueError("content_length_out_of_bounds")
            return amount
    return 0


def split_sip_messages(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split stream bytes into complete SIP messages plus trailing remainder."""
    messages: list[bytes] = []
    cursor = 0
    total = len(buffer)

    while cursor < total:
        # Skip CRLF keepalive frames.
        if buffer[cursor : cursor + 2] == b"\r\n":
            cursor += 2
            continue
        if buffer[cursor : cursor + 1] == b"\n":
            cursor += 1
            continue

        header = _find_header_terminator(buffer, cursor)
        if not header:
            break
        header_end, sep_len = header
        header_blob = buffer[cursor:header_end]
        content_length = _extract_content_length(header_blob)
        message_end = header_end + sep_len + content_length
        if message_end > total:
            break
        messages.append(buffer[cursor:message_end])
        cursor = message_end

    return messages, buffer[cursor:]


PARAM_RE = re.compile(r';\s*([a-zA-Z0-9\-_.!%*+`\'~]+)(?:=("[^"]*"|[^;]+))?')


def parse_uri_header(value: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for match in PARAM_RE.finditer(value):
        key = match.group(1).lower()
        raw = match.group(2) or ""
        params[key] = raw.strip('"')
    return params


def parse_auth_header(value: str) -> Dict[str, str]:
    value = value.strip()
    if value.lower().startswith("digest "):
        value = value[7:]
    result: Dict[str, str] = {}
    parts = re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', value)
    for part in parts:
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip().lower()
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        result[key] = raw
    return result


def _hash_by_algorithm(algorithm: str, data: str) -> str:
    algorithm = algorithm.upper()
    if algorithm == "MD5":
        return hashlib.md5(data.encode("utf-8")).hexdigest()
    if algorithm == "SHA-256":
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    raise ValueError(f"Unsupported digest algorithm: {algorithm}")


def digest_response(
    username: str,
    realm: str,
    password: str,
    method: str,
    uri: str,
    nonce: str,
    nc: Optional[str] = None,
    cnonce: Optional[str] = None,
    qop: Optional[str] = None,
    algorithm: str = "MD5",
) -> str:
    ha1 = _hash_by_algorithm(algorithm, f"{username}:{realm}:{password}")
    ha2 = _hash_by_algorithm(algorithm, f"{method}:{uri}")
    if qop:
        if not (nc and cnonce):
            raise ValueError("nc and cnonce are required when qop is set")
        return _hash_by_algorithm(algorithm, f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _hash_by_algorithm(algorithm, f"{ha1}:{nonce}:{ha2}")


def generate_nonce(length: int = 24) -> str:
    return secrets.token_hex(length // 2)


def make_digest_challenge(realm: str, algorithm: str = "MD5", qop: str = "auth") -> str:
    nonce = generate_nonce()
    opaque = secrets.token_hex(8)
    return (
        f'Digest realm="{realm}", nonce="{nonce}", algorithm={algorithm}, '
        f'qop="{qop}", opaque="{opaque}"'
    )


def build_response(
    request: SIPMessage,
    status_code: int,
    reason: str,
    extra_headers: Optional[Dict[str, Union[str, list[str]]]] = None,
    body: str = "",
) -> SIPMessage:
    response = SIPMessage(status_code=status_code, reason=reason)
    via_headers = request.get_all("Via")
    for via in via_headers:
        response.add_header("Via", via)

    for hdr in ("From", "To", "Call-Id", "Cseq"):
        value = request.get(hdr)
        if value:
            # Ensure UAS tag if missing in To for dialog-forming responses.
            if hdr == "To" and ";tag=" not in value and status_code >= 180:
                value = value + f";tag={secrets.token_hex(4)}"
            response.add_header(hdr, value)

    response.add_header("Server", "SMURF/0.1")
    if extra_headers:
        for name, value in extra_headers.items():
            if isinstance(value, list):
                for item in value:
                    response.add_header(name, item)
            else:
                response.add_header(name, value)
    response.body = body
    return response


def parse_contact_uri(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    if "<" in header_value and ">" in header_value:
        start = header_value.index("<") + 1
        end = header_value.index(">", start)
        return header_value[start:end].strip()
    return header_value.split(";")[0].strip()


def parse_aor(uri: str) -> str:
    uri = uri.strip()
    if uri.startswith("sip:"):
        uri = uri[4:]
    if ";" in uri:
        uri = uri.split(";", 1)[0]
    return uri


def parse_sdp_media_port(sdp: str) -> Optional[int]:
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m=audio"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
    return None


def random_branch() -> str:
    return "z9hG4bK" + os.urandom(6).hex()


def secure_compare_digest(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
