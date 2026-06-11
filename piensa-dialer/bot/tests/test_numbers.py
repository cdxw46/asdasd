from app.numbers import dial_string, parse_numbers


def test_parse_basic_separators():
    valid, invalid = parse_numbers("+34600111222, +34600333444\n+34600555666")
    assert valid == ["+34600111222", "+34600333444", "+34600555666"]
    assert invalid == []


def test_parse_dedup_preserves_order():
    valid, _ = parse_numbers("+34600111222 +34600111222 +34600333444")
    assert valid == ["+34600111222", "+34600333444"]


def test_parse_00_prefix_becomes_plus():
    valid, _ = parse_numbers("0034600111222")
    assert valid == ["+34600111222"]


def test_local_number_gets_default_country_code():
    valid, _ = parse_numbers("600111222", default_country_code="34")
    assert valid == ["+34600111222"]


def test_full_international_not_double_prefixed():
    valid, _ = parse_numbers("+34600111222", default_country_code="34")
    assert valid == ["+34600111222"]


def test_invalid_tokens_collected():
    # Pure-letter words act as separators; digit-ish tokens that fail
    # validation (too short) are reported as invalid.
    valid, invalid = parse_numbers("hola 12 +34600111222 abc")
    assert valid == ["+34600111222"]
    assert "12" in invalid


def test_dial_string_strips_plus():
    assert dial_string("+34600111222") == "34600111222"
