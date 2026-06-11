"""Parseo y normalizacion de numeros de telefono desde texto libre / CSV."""
from __future__ import annotations

import re


def parse_numbers(text: str, default_country_code: str = "34") -> list[str]:
    """Extrae numeros de telefono de un texto arbitrario.

    Acepta numeros separados por saltos de linea, comas, espacios, ';' o
    tabuladores (incluye contenido tipico de CSV). Normaliza a formato
    internacional SIN el '+' (ej. 34680540787), que es lo que espera el
    dial string de Asterisk hacia el trunk.

    - "+34 680 54 07 87"  -> "34680540787"
    - "0034680540787"     -> "34680540787"
    - "680540787"         -> "34680540787" (anade prefijo por defecto)
    - "911234567"         -> "34911234567"

    Elimina duplicados conservando el orden de aparicion. Descarta lo que
    claramente no sea un numero marcable.
    """
    # Detecta secuencias "tipo telefono": un '+' opcional seguido de digitos
    # que pueden llevar espacios, guiones, puntos o parentesis como separadores
    # de grupo (ej. "+34 680 54 07 87"). Asi no partimos un numero espaciado.
    candidates = re.findall(r"\+?\d[\d \t.()\-]{4,}\d", text)
    result: list[str] = []
    seen: set[str] = set()

    for raw in candidates:
        num = _normalize(raw, default_country_code)
        if num and num not in seen:
            seen.add(num)
            result.append(num)
    return result


def _normalize(raw: str, default_country_code: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None

    has_plus = raw.startswith("+")
    # Quitar todo lo que no sea digito o '+'
    digits = re.sub(r"[^\d+]", "", raw)
    digits = digits.lstrip("+")

    if not digits.isdigit():
        return None

    # Prefijo internacional con 00 -> equivalente a '+'
    if digits.startswith("00"):
        digits = digits[2:]
        has_plus = True

    # Numero demasiado corto para ser valido
    if len(digits) < 6:
        return None

    if has_plus:
        # Ya viene en internacional
        return digits

    # Sin '+': si parece nacional, anadimos el prefijo por defecto.
    if digits.startswith(default_country_code) and len(digits) > len(default_country_code) + 5:
        return digits
    return f"{default_country_code}{digits}"
