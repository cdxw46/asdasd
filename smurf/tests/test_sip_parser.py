from __future__ import annotations

from core.sip import (
    MAX_CONTENT_LENGTH,
    SIPMessage,
    parse_sip_message,
    split_sip_messages,
)


def _register_message(content_length: int = 0, body: str = "") -> bytes:
    return (
        "REGISTER sip:smurf.local SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-1\r\n"
        "From: <sip:1000@smurf.local>;tag=abc\r\n"
        "To: <sip:1000@smurf.local>\r\n"
        "Call-Id: test-call-id\r\n"
        "Cseq: 1 REGISTER\r\n"
        f"Content-Length: {content_length}\r\n"
        "\r\n"
        f"{body}"
    ).encode("utf-8")


def test_parse_single_message_success() -> None:
    message = _register_message()
    parsed = parse_sip_message(message)
    assert parsed.is_request is True
    assert parsed.method == "REGISTER"
    assert parsed.get("Call-Id") == "test-call-id"


def test_parse_requires_mandatory_headers() -> None:
    raw = (
        "REGISTER sip:smurf.local SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-1\r\n"
        "From: <sip:1000@smurf.local>;tag=abc\r\n"
        "Call-Id: missing-to\r\n"
        "Cseq: 1 REGISTER\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    ).encode("utf-8")
    try:
        parse_sip_message(raw)
    except ValueError as exc:
        assert "missing_required_header" in str(exc)
    else:
        raise AssertionError("Expected missing_required_header validation failure")


def test_split_sip_messages_multiple_frames() -> None:
    first = _register_message()
    second = _register_message()
    stream = first + second
    messages, remainder = split_sip_messages(stream)
    assert len(messages) == 2
    assert remainder == b""


def test_split_sip_messages_handles_fragment_remainder() -> None:
    message = _register_message()
    truncated = message[:-10]
    messages, remainder = split_sip_messages(truncated)
    assert messages == []
    assert remainder == truncated


def test_split_sip_messages_rejects_huge_content_length() -> None:
    huge = MAX_CONTENT_LENGTH + 1
    raw = (
        "REGISTER sip:smurf.local SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-1\r\n"
        "From: <sip:1000@smurf.local>;tag=abc\r\n"
        "To: <sip:1000@smurf.local>\r\n"
        "Call-Id: huge-body\r\n"
        "Cseq: 1 REGISTER\r\n"
        f"Content-Length: {huge}\r\n"
        "\r\n"
    ).encode("utf-8")
    try:
        split_sip_messages(raw)
    except ValueError as exc:
        assert "content_length_out_of_bounds" in str(exc)
    else:
        raise AssertionError("Expected content length bounds validation failure")

