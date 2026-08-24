#!/usr/bin/env python3
"""Read-only health check for a local Mihomo controller and UI."""

from __future__ import annotations

import http.client
import os
from pathlib import Path
import yaml


def request(port: int, secret: str | None, method: str, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    headers = {"Authorization": f"Bearer {secret}" } if secret else {}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, payload


def main() -> None:
    config_path = Path(os.environ.get("SUBCTL_CONFIG", "/etc/subctl/config.yaml"))
    gateway = (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}).get("gateway") or {}
    port = int(gateway.get("controller_port", os.environ.get("MIHOMO_CONTROLLER_PORT", "19090")))
    secret = gateway.get("controller_secret")
    unauth_status, _ = request(port, None, "GET", "/version")
    if secret and unauth_status != 401:
        raise SystemExit(f"Mihomo controller auth check returned HTTP {unauth_status}")
    ui_status, ui_body = request(port, None, "GET", "/ui/")
    if ui_status != 200 or b"<html" not in ui_body.lower():
        raise SystemExit("Mihomo UI is not reachable")
    if secret:
        for path in ("/proxies", "/providers/proxies", "/connections"):
            status, _ = request(port, secret, "GET", path)
            if status != 200:
                raise SystemExit(f"Mihomo controller check failed for {path}: HTTP {status}")
        status, payload = request(port, secret, "GET", "/proxies")
        if status == 200:
            data = yaml.safe_load(payload.decode("utf-8")) or {}
            groups = set((data.get("proxies") or {}).keys())
            required = {"PRIVATE", "PROVIDER-AUTO", "AUTO", "PROXY", "BASE"}
            if not required <= groups:
                missing = ", ".join(sorted(required - groups))
                raise SystemExit(f"Mihomo profile is missing groups: {missing}")
    print("Mihomo UI/controller health: passed")


if __name__ == "__main__":
    main()
