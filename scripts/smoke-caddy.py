#!/usr/bin/env python3
"""Read-only smoke check for public subscription endpoints."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path

import yaml


def fetch(url: str) -> int:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "subctl-smoke/1",
            "X-Subctl-Internal-Check": "1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
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
    checks = [("provider", f"{base_url}/feeds/provider/{config['provider']['shared_token']}")]
    for name, user in sorted((users.get("users") or {}).items()):
        token = user["token"]
        checks.extend(((f"{name}:yaml", f"{base_url}/s/{token}.yaml"), (f"{name}:raw", f"{base_url}/s/{token}.raw")))
    for _, url in checks:
        fetch(url)
    print(f"public subscription smoke: passed ({len(checks)} endpoints)")


if __name__ == "__main__":
    main()
