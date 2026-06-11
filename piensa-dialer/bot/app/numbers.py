"""Parsing and normalisation of phone numbers pasted into Telegram."""
from __future__ import annotations

import re

# Anything that is not a digit or a leading + is treated as a separator.
_SPLIT_RE = re.compile(r"[^\d+]+")
_VALID_RE = re.compile(r"^\+?\d{6,15}$")


def _normalize_one(raw: str, default_country_code: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None

    # Collapse international prefixes to a single leading '+'.
    if raw.startswith("00"):
        raw = "+" + raw[2:]

    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    if plus:
        candidate = "+" + digits
    elif default_country_code and not digits.startswith(default_country_code) and len(digits) <= 10:
        # Local number without country code -> prepend the default one.
        candidate = "+" + default_country_code + digits
    else:
        candidate = "+" + digits

    if not _VALID_RE.match(candidate):
        return None
    return candidate


def parse_numbers(text: str, default_country_code: str = "34") -> tuple[list[str], list[str]]:
    """Parse free-form text into (valid_numbers, invalid_tokens).

    Numbers may be separated by spaces, commas, semicolons or newlines.
    Duplicates are removed while preserving order. Returns the de-duplicated
    list of E.164-ish numbers and the list of tokens that could not be parsed.
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for token in _SPLIT_RE.split(text or ""):
        token = token.strip()
        if not token:
            continue
        normalized = _normalize_one(token, default_country_code)
        if normalized is None:
            invalid.append(token)
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        valid.append(normalized)

    return valid, invalid


def dial_string(number: str) -> str:
    """Return the number as Asterisk wants it in PJSIP/<n>@endpoint (no '+')."""
    return number.lstrip("+")
