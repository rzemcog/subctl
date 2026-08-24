from __future__ import annotations

from pathlib import Path

import pytest

from subctl.errors import ValidationError
from subctl.provider import validate_provider_body

FIXTURES = Path(__file__).parent / "fixtures"

EXPECTED_CANONICAL = (
    "vless://11111111-1111-4111-8111-111111111111@example.com:443?encryption=none#VLESS Node\n"
    "trojan://secret@example.net:443#Trojan Node\n"
)
EXPECTED_BASE64 = (
    "dmxlc3M6Ly8xMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTFAZXhhbXBsZS5jb206NDQzP2VuY3J5"
    "cHRpb249bm9uZSNWTEVTUyBOb2RlCnRyb2phbjovL3NlY3JldEBleGFtcGxlLm5ldDo0NDMjVHJvamFuIE5vZGUK"
)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_valid_base64_provider_body_returns_canonical_outputs():
    result = validate_provider_body(read_fixture("provider_valid_base64.txt"))

    assert result.plain == EXPECTED_CANONICAL
    assert result.encoded == EXPECTED_BASE64
    assert result.uris == tuple(EXPECTED_CANONICAL.rstrip("\n").split("\n"))


def test_valid_plain_provider_body_returns_canonical_outputs():
    result = validate_provider_body(read_fixture("provider_valid_plain.txt"))

    assert result.plain == EXPECTED_CANONICAL
    assert result.encoded == EXPECTED_BASE64


def test_plain_provider_body_normalizes_line_endings_and_whitespace():
    body = (
        "  vless://11111111-1111-4111-8111-111111111111@example.com:443?encryption=none#VLESS Node\r\n"
        "\r\n"
        "trojan://secret@example.net:443#Trojan Node  \r\n"
    )

    result = validate_provider_body(body)

    assert result.plain == EXPECTED_CANONICAL
    assert result.encoded == EXPECTED_BASE64


def test_empty_provider_body_is_rejected():
    with pytest.raises(ValidationError, match="empty|at least one URI"):
        validate_provider_body(read_fixture("provider_empty.txt"))


def test_invalid_base64_and_plain_garbage_is_rejected():
    with pytest.raises(ValidationError, match="valid Base64|non-URI"):
        validate_provider_body(read_fixture("provider_invalid_garbage.txt"))


def test_unsupported_scheme_is_rejected():
    with pytest.raises(ValidationError, match="unsupported URI scheme"):
        validate_provider_body(read_fixture("provider_unsupported_scheme.txt"))


def test_mixed_feed_with_unsupported_line_is_rejected():
    with pytest.raises(ValidationError, match="unsupported URI scheme"):
        validate_provider_body(read_fixture("provider_mixed.txt"))


def test_canonical_output_is_stable_for_plain_and_base64_inputs():
    plain_result = validate_provider_body(read_fixture("provider_valid_plain.txt"))
    base64_result = validate_provider_body(read_fixture("provider_valid_base64.txt"))

    assert plain_result == base64_result
    assert plain_result.plain == EXPECTED_CANONICAL
    assert plain_result.encoded == EXPECTED_BASE64
