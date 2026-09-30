from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest
import yaml

from subctl.config import load_config
from subctl.registry import load_users
from subctl.render import build_mihomo_profile


MIHOMO_BIN = os.environ.get("MIHOMO_BIN")
pytestmark = pytest.mark.skipif(
    not MIHOMO_BIN,
    reason="set MIHOMO_BIN to run the local Mihomo runtime integration check",
)


class _FeedHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen_hosts.append(self.headers.get("Host"))
        self.server.seen_user_agents.append(self.headers.get("User-Agent"))
        payload = {
            "proxies": [
                {
                    "name": "fixture-seed",
                    "type": "socks5",
                    "server": "127.0.0.1",
                    "port": 9,
                }
            ]
        }
        body = yaml.safe_dump(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _api_json(base_url: str, path: str, *, method: str = "GET", body=None):
    encoded_body = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{base_url}{path}",
        data=encoded_body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=2) as response:
        payload = response.read()
        return json.loads(payload) if payload else None


def _wait_for_group(base_url: str, name: str, member: str, process) -> dict:
    deadline = time.monotonic() + 20
    path = f"/proxies/{quote(name, safe='')}"
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Mihomo exited before exposing {name}; exit={process.returncode}")
        try:
            group = _api_json(base_url, path)
        except Exception as exc:  # the controller is not ready yet
            last_error = exc
        else:
            if member in group.get("all", []):
                return group
        time.sleep(0.1)
    pytest.fail(f"Mihomo did not expose a member in {name}: {last_error!r}")


def test_mihomo_loads_generated_profile_resolves_provider_host_and_supports_pin_lifecycle(
    tmp_path, config_data, write_yaml, provider_state_dir, users_path
):
    feed_host = "feed.fixture.test"
    config_data["mihomo"] = {"hosts": {feed_host: "127.0.0.1"}}
    config_path = write_yaml("mihomo-runtime-config.yaml", config_data)
    config = load_config(config_path, state_dir=provider_state_dir)
    user = load_users(users_path).users["alice"]
    profile = build_mihomo_profile(config, user)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    server.seen_hosts = []
    server.seen_user_agents = []
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    feed_url = f"http://{feed_host}:{server.server_port}/live"
    for provider_name in ("provider", "private"):
        provider = profile["proxy-providers"][provider_name]
        provider["url"] = feed_url
        provider["proxy"] = "DIRECT"

    profile.update(
        {
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            "external-controller": f"127.0.0.1:{_unused_port()}",
            "port": _unused_port(),
            "socks-port": _unused_port(),
            "mixed-port": _unused_port(),
            "dns": {
                "enable": True,
                "ipv6": False,
                "enhanced-mode": "redir-host",
                "nameserver": ["127.0.0.1"],
            },
        }
    )
    home = tmp_path / "mihomo-home"
    home.mkdir()
    (home / "providers").mkdir()
    config_file = tmp_path / "profile.yaml"
    config_file.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    process = subprocess.Popen(
        [MIHOMO_BIN, "-d", str(home), "-f", str(config_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{profile['external-controller'].split(':')[-1]}"
    try:
        live_member = "fixture-seed"
        live = _wait_for_group(base_url, "PROVIDER-AUTO-LIVE", live_member, process)
        seed = _wait_for_group(
            base_url,
            "PROVIDER-AUTO-SEED",
            "fixture-seed",
            process,
        )
        assert live["type"] == "URLTest"
        assert seed["type"] == "URLTest"
        assert isinstance(live["now"], str)
        assert isinstance(seed["now"], str)

        for name, member in (
            ("PROVIDER-AUTO-LIVE", live_member),
            ("PROVIDER-AUTO-SEED", "fixture-seed"),
        ):
            path = f"/proxies/{quote(name, safe='')}"
            before = _api_json(base_url, path)
            assert member in before["all"]
            assert not before.get("fixed")
            assert _api_json(base_url, path, method="PUT", body={"name": member}) is None
            pinned = _api_json(base_url, path)
            assert pinned["fixed"] == member
            assert pinned["now"] == member
            assert _api_json(base_url, path, method="DELETE") is None
            unpinned = _api_json(base_url, path)
            assert not unpinned.get("fixed")
            assert isinstance(unpinned["now"], str)

        global_group = _api_json(base_url, "/proxies/GLOBAL")
        expected_groups = [group["name"] for group in profile["proxy-groups"] if group["name"] != "GLOBAL"]
        assert set(global_group["all"]) == set(expected_groups)
        assert all(name != "fixture-seed" for name in global_group["all"])
        assert "BOOTSTRAP-SEED |" not in config_file.read_text(encoding="utf-8")
        assert "LIVE | " not in config_file.read_text(encoding="utf-8")
        assert "SEED | " not in config_file.read_text(encoding="utf-8")
        assert f"{feed_host}:{server.server_port}" in server.seen_hosts
        assert any(
            user_agent
            and "Mozilla/5.0" in user_agent
            and "Chrome/154.0.0.0" in user_agent
            for user_agent in server.seen_user_agents
        )
    finally:
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
