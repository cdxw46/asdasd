"""Tests del parser de numeros (no requiere Asterisk ni red)."""
from phone_numbers import parse_numbers


def test_basic_variants():
    text = """
    +34 680 54 07 87
    0034911234567
    600111222
    """
    assert parse_numbers(text) == ["34680540787", "34911234567", "34600111222"]


def test_dedupe_and_order():
    text = "34680540787, 680540787\n911234567; 911234567"
    assert parse_numbers(text) == ["34680540787", "34911234567"]


def test_csv_like():
    text = "nombre,telefono\nJuan,+34911234567\nAna,680540787\n"
    nums = parse_numbers(text)
    assert "34911234567" in nums
    assert "34680540787" in nums


def test_default_country_code():
    assert parse_numbers("612345678", default_country_code="34") == ["34612345678"]
    assert parse_numbers("612345678", default_country_code="351") == ["351612345678"]


def test_international_plus_kept():
    assert parse_numbers("+447911123456") == ["447911123456"]


def test_junk_ignored():
    assert parse_numbers("hola mundo abc 12") == []


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
