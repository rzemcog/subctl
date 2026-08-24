from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from .errors import ValidationError

SUPPORTED_PROVIDER_SCHEMES = frozenset({"vless", "vmess", "trojan", "ss", "ssr", "hysteria2", "hy2"})


@dataclass(frozen=True)
class ProviderFeed:
    plain: str
    encoded: str
    uris: tuple[str, ...]

    @property
    def plain_text(self) -> str:
        return self.plain

    @property
    def base64_text(self) -> str:
        return self.encoded


def validate_provider_body(body: bytes | str) -> ProviderFeed:
    """Validate an upstream provider response without performing network I/O."""
    text = _body_to_text(body)
    plain_error: ValidationError | None = None

    if _looks_like_uri_list(text):
        try:
            return _validate_plain_provider(text)
        except ValidationError as exc:
            plain_error = exc

    try:
        decoded = _decode_base64_provider(text)
    except ValidationError as base64_error:
        if plain_error is not None:
            raise plain_error from base64_error
        raise base64_error

    return _validate_plain_provider(decoded)


def validate_provider_subscription(body: bytes | str) -> ProviderFeed:
    return validate_provider_body(body)


def _body_to_text(body: bytes | str) -> str:
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("provider body must be UTF-8 text or Base64 encoded UTF-8") from exc
    if isinstance(body, str):
        return body
    raise ValidationError("provider body must be bytes or text")


def _decode_base64_provider(text: str) -> str:
    compact = "".join(text.split())
    if not compact:
        raise ValidationError("provider feed is empty")
    remainder = len(compact) % 4
    if remainder == 1:
        raise ValidationError("provider feed is not valid Base64 or a plain URI list")
    padded = compact + ("=" * ((4 - remainder) % 4))
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("provider feed is not valid Base64 or a plain URI list") from exc
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "provider feed is not valid Base64/plain URI list or contains invalid proxy entries; "
            "Base64 payload must decode to UTF-8 text"
        ) from exc


def _validate_plain_provider(text: str) -> ProviderFeed:
    lines = _canonical_lines(text.splitlines())
    if not lines:
        raise ValidationError("provider feed must contain at least one URI")

    unsupported = sorted({_scheme_for_line(line) for line in lines if _scheme_for_line(line) not in SUPPORTED_PROVIDER_SCHEMES})
    if unsupported:
        raise ValidationError(
            "provider feed contains unsupported URI scheme(s): " + ", ".join(unsupported)
        )

    plain = "\n".join(lines) + "\n"
    encoded = base64.b64encode(plain.encode("utf-8")).decode("ascii")
    return ProviderFeed(plain=plain, encoded=encoded, uris=tuple(lines))


def _canonical_lines(lines: Iterable[str]) -> tuple[str, ...]:
    return tuple(line.strip() for line in lines if line.strip())


def _looks_like_uri_list(text: str) -> bool:
    lines = _canonical_lines(text.splitlines())
    return any(urlsplit(line).scheme for line in lines)


def _scheme_for_line(line: str) -> str:
    scheme = urlsplit(line).scheme.lower()
    if not scheme:
        raise ValidationError(f"provider feed contains a non-URI line: {line[:32]}")
    return scheme
