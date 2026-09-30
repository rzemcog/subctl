#!/usr/bin/env python3
"""Read-only smoke check for public subscription endpoints."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def install_connect_ip_override(public_host: str, connect_ip: str) -> None:
    address = str(ipaddress.ip_address(connect_ip))
    original_getaddrinfo = socket.getaddrinfo

    def resolve(host: str, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        if host == public_host:
            host = address
        return original_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = resolve


def fetch(url: str, opener: urllib.request.OpenerDirector | None = None) -> int:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "subctl-smoke/1",
            "X-Subctl-Internal-Check": "1",
        },
    )
    try:
        open_url = opener.open if opener is not None else urllib.request.urlopen
        with open_url(request, timeout=15) as response:
            body = response.read()
            status = response.status
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit("public subscription endpoint is unreachable") from exc
    if status != 200 or not body:
        raise SystemExit(f"public subscription endpoint returned HTTP {status}")
    return len(body)


def main() -> None:
    config_path = Path(os.environ.get("SUBCTL_CONFIG", "/etc/subctl/config.yaml"))
    users_path = Path(os.environ.get("SUBCTL_USERS", "/var/lib/subctl/registry/users.yaml"))
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    users = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
    base_url = config["public"]["base_url"].rstrip("/")
    public_host = urlsplit(base_url).hostname
    if not public_host:
        raise SystemExit("public base URL has no hostname")
    connect_ip = os.environ.get("SUBCTL_SMOKE_CONNECT_IP")
    opener = None
    if connect_ip:
        install_connect_ip_override(public_host, connect_ip)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    checks = [("provider", f"{base_url}/feeds/provider/{config['provider']['shared_token']}")]
    for name, user in sorted((users.get("users") or {}).items()):
        token = user["token"]
        checks.extend(((f"{name}:yaml", f"{base_url}/s/{token}.yaml"), (f"{name}:raw", f"{base_url}/s/{token}.raw")))
    for _, url in checks:
        fetch(url, opener)
    print(f"public subscription smoke: passed ({len(checks)} endpoints)")


if __name__ == "__main__":
    main()
