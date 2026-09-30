from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from .errors import ValidationError
from .provider import validate_provider_subscription


@dataclass(frozen=True)
class ProviderSeedSnapshot:
    proxies: tuple[dict[str, Any], ...]
    generated_at: str
    source_updated_at: str | None


def load_provider_seed(
    path: Path, exclude_keywords: tuple[str, ...] = ()
) -> ProviderSeedSnapshot:
    """Read the validated subctl cache and convert supported URIs to Mihomo maps."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationError(
            "provider cache is missing; refresh the provider before rendering a SEED snapshot"
        ) from exc
    except OSError as exc:
        raise ValidationError("provider cache cannot be read for a SEED snapshot") from exc

    try:
        feed = validate_provider_subscription(text)
    except ValidationError:
        raise ValidationError("provider cache is invalid for a SEED snapshot") from None
    selected = tuple(
        uri
        for uri in feed.uris
        if not _uri_name_contains(uri, exclude_keywords)
    )
    if not selected:
        raise ValidationError("provider cache has no nodes after applying provider exclusions")

    proxies_list: list[dict[str, Any]] = []
    name_counts: dict[str, int] = {}
    for index, uri in enumerate(selected, 1):
        proxy = _proxy_from_uri(uri, index)
        base_name = proxy["name"]
        name_counts[base_name] = name_counts.get(base_name, 0) + 1
        if name_counts[base_name] > 1:
            proxy["name"] = f"{base_name} ({name_counts[base_name]})"
        proxies_list.append(proxy)
    proxies = tuple(proxies_list)
    source_updated_at: str | None
    try:
        source_updated_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        source_updated_at = None
    return ProviderSeedSnapshot(
        proxies=proxies,
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_updated_at=source_updated_at,
    )


def _proxy_from_uri(uri: str, index: int) -> dict[str, Any]:
    try:
        scheme = urlsplit(uri).scheme.lower()
        if scheme == "vless":
            return _vless_proxy(uri, index)
        if scheme == "trojan":
            return _trojan_proxy(uri, index)
    except (ValueError, UnicodeError):
        pass
    raise ValidationError(
        f"provider cache contains a node that cannot be represented in SEED (line {index})"
    ) from None


def _vless_proxy(uri: str, index: int) -> dict[str, Any]:
    try:
        parts = urlsplit(uri)
        if parts.scheme.lower() != "vless":
            raise ValueError
        server = parts.hostname
        port = parts.port
        uuid = unquote(parts.username or "")
        if not server or port is None or not uuid or parts.password is not None:
            raise ValueError

        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
        params: dict[str, str] = {}
        for key, value in pairs:
            if key in params:
                raise ValueError
            params[key] = value
        supported_keys = {
            "encryption",
            "flow",
            "fp",
            "headerType",
            "pbk",
            "security",
            "sid",
            "sni",
            "type",
        }
        if set(params) - supported_keys:
            raise ValueError
        if params.get("type", "tcp").lower() != "tcp":
            raise ValueError
        if params.get("headerType", "none").lower() != "none":
            raise ValueError
        if params.get("encryption", "none").lower() not in {"", "none"}:
            raise ValueError

        security = params.get("security", "none").lower()
        if security not in {"none", "tls", "reality"}:
            raise ValueError
        if security == "reality" and not params.get("pbk"):
            raise ValueError
        if security != "reality" and ("pbk" in params or "sid" in params):
            raise ValueError

        label = _safe_label(unquote(parts.fragment)) or f"node-{index}"
        proxy: dict[str, Any] = {
            "name": label,
            "type": "vless",
            "server": server,
            "port": port,
            "uuid": uuid,
            "network": "tcp",
            "encryption": "",
        }
        if params.get("flow"):
            proxy["flow"] = params["flow"]
        if security in {"tls", "reality"}:
            proxy["tls"] = True
            if params.get("sni"):
                proxy["servername"] = params["sni"]
            if params.get("fp"):
                proxy["client-fingerprint"] = params["fp"]
        if security == "reality":
            proxy["reality-opts"] = {
                "public-key": params["pbk"],
                "short-id": params.get("sid", ""),
            }
        return proxy
    except (ValueError, UnicodeError):
        raise


def _trojan_proxy(uri: str, index: int) -> dict[str, Any]:
    parts = urlsplit(uri)
    if parts.scheme.lower() != "trojan":
        raise ValueError
    server = parts.hostname
    port = parts.port
    password = unquote(parts.username or "")
    if not server or port is None or not password or parts.password is not None:
        raise ValueError

    params: dict[str, str] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False):
        if key in params:
            raise ValueError
        params[key] = value
    if set(params) - {"sni", "alpn", "allowInsecure", "fp"}:
        raise ValueError

    label = _safe_label(unquote(parts.fragment)) or f"node-{index}"
    proxy: dict[str, Any] = {
        "name": label,
        "type": "trojan",
        "server": server,
        "port": port,
        "password": password,
        "tls": True,
    }
    if params.get("sni"):
        proxy["servername"] = params["sni"]
    if params.get("fp"):
        proxy["client-fingerprint"] = params["fp"]
    if params.get("alpn"):
        proxy["alpn"] = [value.strip() for value in params["alpn"].split(",") if value.strip()]
    if params.get("allowInsecure", "false").lower() in {"true", "1", "yes"}:
        proxy["skip-cert-verify"] = True
    elif params.get("allowInsecure", "false").lower() not in {"false", "0", "no"}:
        raise ValueError
    return proxy


def _safe_label(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return " ".join(cleaned.split())[:100]


def _uri_name_contains(uri: str, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return False
    name = unquote(urlsplit(uri).fragment).casefold()
    return any(keyword.casefold() in name for keyword in keywords)
