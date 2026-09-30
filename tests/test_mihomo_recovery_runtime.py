from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import threading
import time
from contextlib import ExitStack, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import pytest
import yaml

from conftest import VALID_ALICE_TOKEN
from subctl.config import load_config
from subctl.registry import load_users
from subctl.render import build_mihomo_profile


MIHOMO_BIN = os.environ.get("MIHOMO_BIN")
pytestmark = pytest.mark.skipif(
    not MIHOMO_BIN,
    reason="set MIHOMO_BIN to run the Mihomo recovery integration checks",
)


class _TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/probe":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.server.traffic_seen_via.append(
            self.headers.get("X-Integration-Proxy", "DIRECT")
        )
        body = b"target-response"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return

    do_HEAD = do_GET


class _ForwardProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests_seen += 1
        if not self.server.healthy:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        target = urlsplit(self.path)
        if target.scheme != "http" or not target.hostname:
            host = self.headers.get("Host", "")
            target = urlsplit(f"http://{host}{self.path}")
        if target.scheme != "http" or not target.hostname:
            self.send_error(400)
            return

        connection = http.client.HTTPConnection(
            target.hostname, target.port or 80, timeout=3
        )
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() not in {"connection", "proxy-connection", "host"}
        }
        headers["X-Integration-Proxy"] = self.server.proxy_name
        try:
            connection.request(
                "GET",
                target.path or "/",
                headers=headers,
            )
            upstream = connection.getresponse()
            body = upstream.read()
            self.send_response(upstream.status)
            content_type = upstream.getheader("Content-Type")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (OSError, http.client.HTTPException):
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
        finally:
            connection.close()

    def do_CONNECT(self):
        self.server.requests_seen += 1
        self.close_connection = True
        if not self.server.healthy:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200, "Connection Established")
        self.end_headers()
        request_line = self.rfile.readline(65536).decode("iso-8859-1").strip()
        if not request_line:
            return
        method, request_target, _version = request_line.split(" ", 2)
        request_headers = {}
        while True:
            line = self.rfile.readline(65536)
            if not line or line in {b"\r\n", b"\n"}:
                break
            name, separator, value = line.decode("iso-8859-1").partition(":")
            if separator and name.casefold() not in {
                "connection",
                "proxy-connection",
                "host",
            }:
                request_headers[name] = value.strip()

        host, _, port_text = self.path.rpartition(":")
        if not host or not port_text.isdigit():
            return
        target = urlsplit(request_target)
        if target.scheme == "http" and target.hostname:
            host = target.hostname
            port_text = str(target.port or 80)
            path = target.path or "/"
            if target.query:
                path += f"?{target.query}"
        else:
            path = request_target
        request_headers["X-Integration-Proxy"] = self.server.proxy_name
        connection = http.client.HTTPConnection(host, int(port_text), timeout=3)
        try:
            connection.request(method, path, headers=request_headers)
            upstream = connection.getresponse()
            body = upstream.read()
            content_type = upstream.getheader("Content-Type")
            response = [
                f"HTTP/1.1 {upstream.status} {upstream.reason}\r\n",
                f"Content-Length: {len(body)}\r\n",
                "Connection: close\r\n",
            ]
            if content_type:
                response.append(f"Content-Type: {content_type}\r\n")
            response.append("\r\n")
            self.connection.sendall("".join(response).encode("iso-8859-1") + body)
        except (OSError, http.client.HTTPException):
            with suppress(OSError):
                self.connection.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
        finally:
            connection.close()

    def log_message(self, format, *args):
        return

    do_HEAD = do_GET


class _FeedHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.requests_seen.append(path)
        self.server.transports_seen.append(
            (path, self.headers.get("X-Integration-Proxy", "DIRECT"))
        )
        if path == "/live" and self.server.fail_live:
            self.server.live_failures += 1
            self.server.responses_seen.append((path, 503))
            body = b"temporary provider failure"
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/private" and self.server.fail_private:
            self.server.private_failures += 1
            self.server.responses_seen.append((path, 503))
            body = b"temporary private provider failure"
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/live":
            node_name = "live-fixture"
            proxy_port = self.server.proxy_ports["live"]
        elif path == "/private":
            node_name = "private-fixture"
            proxy_port = self.server.proxy_ports["private"]
        else:
            self.send_error(404)
            return

        body = yaml.safe_dump(
            {
                "proxies": [
                    {
                        "name": node_name,
                        "type": "http",
                        "server": "127.0.0.1",
                        "port": proxy_port,
                    }
                ]
            }
        ).encode("utf-8")
        self.server.responses_seen.append((path, 200))
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


def _start_server(stack: ExitStack, handler, **attributes):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    for name, value in attributes.items():
        setattr(server, name, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stack.callback(thread.join, timeout=2)
    stack.callback(server.server_close)
    stack.callback(server.shutdown)
    return server


def _api_json(base_url: str, path: str, *, method: str = "GET", body=None):
    encoded_body = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{base_url}{path}",
        data=encoded_body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=3) as response:
        payload = response.read()
        return json.loads(payload) if payload else None


def _wait_for(predicate, process, description: str, *, timeout: float = 20):
    deadline = time.monotonic() + timeout
    last_value = None
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Mihomo exited while waiting for {description}")
        try:
            last_value = predicate()
            last_error = None
            if last_value:
                return last_value
        except Exception as exc:
            last_error = repr(exc)
        time.sleep(0.2)
    log_path = getattr(process, "log_path", None)
    log_tail = ""
    if log_path and log_path.exists():
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-3500:]
    diagnostics = getattr(process, "runtime_diagnostics", "")
    if callable(diagnostics):
        diagnostics = diagnostics()
    pytest.fail(
        f"Mihomo did not reach {description}; last={last_value!r}, "
        f"error={last_error!r}, diagnostics={diagnostics!r}, logs={log_tail!r}"
    )


def _group(base_url: str, name: str):
    return _api_json(base_url, f"/proxies/{quote(name, safe='')}")


def _force_provider_update(base_url: str, name: str) -> int:
    request = Request(
        f"{base_url}/providers/proxies/{quote(name, safe='')}",
        data=b"",
        method="PUT",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def _provider_branch(base_url: str):
    selected = _group(base_url, "PROVIDER-AUTO").get("now")
    for branch, name in (
        ("live", "PROVIDER-AUTO-LIVE"),
        ("seed", "PROVIDER-AUTO-SEED"),
    ):
        if selected != name:
            continue
        group = _group(base_url, name)
        if group.get("alive") and group.get("now") not in {None, "REJECT"}:
            return branch
    return None


def _request_via_mihomo(mixed_port: int, target_port: int):
    connection = http.client.HTTPConnection("127.0.0.1", mixed_port, timeout=5)
    try:
        connection.request(
            "GET",
            f"http://127.0.0.1:{target_port}/traffic",
            headers={"Host": f"127.0.0.1:{target_port}"},
        )
        response = connection.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException):
        return None, b""
    finally:
        connection.close()


def _runtime_profile(config_data, write_yaml, provider_state_dir, users_path, origin_url):
    config_data["render"].update(
        {
            "healthcheck_url": origin_url,
            "healthcheck_interval_seconds": 2,
            "healthcheck_timeout_milliseconds": 1000,
            "healthcheck_max_failed_times": 1,
            "healthcheck_lazy": False,
        }
    )
    config_path = write_yaml("mihomo-recovery-config.yaml", config_data)
    config = load_config(config_path, state_dir=provider_state_dir)
    user = load_users(users_path).users["alice"]
    profile = build_mihomo_profile(config, user)
    profile["rules"] = ["MATCH,PROVIDER-AUTO"]
    for provider_name in ("provider", "private"):
        provider = profile["proxy-providers"][provider_name]
        provider["interval"] = 3600
        provider["health-check"].update(
            {"url": origin_url, "interval": 2, "timeout": 1000, "lazy": False}
        )
    profile["proxy-providers"]["provider"]["proxy"] = "FETCH-AUTO-PROVIDER"
    profile["proxy-providers"]["private"]["proxy"] = "FETCH-PRIVATE"
    profile["proxy-providers"]["provider-seed"]["health-check"].update(
        {"url": origin_url, "interval": 2, "timeout": 1000, "lazy": False}
    )
    for group in profile["proxy-groups"]:
        if "url" in group:
            group.update(
                {
                    "url": origin_url,
                    "interval": 2,
                    "timeout": 1000,
                    "max-failed-times": 2,
                    "lazy": False,
                    "expected-status": 204,
                }
            )
    return profile


def _start_mihomo(stack: ExitStack, profile: dict, tmp_path: Path):
    profile.update(
        {
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            "external-controller": f"127.0.0.1:{_unused_port()}",
            "port": _unused_port(),
            "socks-port": _unused_port(),
            "mixed-port": _unused_port(),
            "log-level": "debug",
            "dns": {
                "enable": True,
                "ipv6": False,
                "enhanced-mode": "redir-host",
                "nameserver": ["127.0.0.1"],
            },
        }
    )
    home = tmp_path / "mihomo-recovery-home"
    home.mkdir()
    (home / "providers").mkdir()
    config_file = tmp_path / "mihomo-recovery.yaml"
    config_file.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    log_path = tmp_path / "mihomo.log"
    log_stream = log_path.open("wb")
    stack.callback(log_stream.close)
    process = subprocess.Popen(
        [MIHOMO_BIN, "-d", str(home), "-f", str(config_file)],
        stdout=log_stream,
        stderr=subprocess.STDOUT,
    )
    process.log_path = log_path
    stack.callback(_stop_process, process)
    base_url = f"http://127.0.0.1:{profile['external-controller'].split(':')[-1]}"
    return process, base_url, profile, home


def _stop_process(process):
    if process.poll() is None:
        process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def _recovery_fixture_servers(stack: ExitStack):
    target = _start_server(
        stack,
        _TargetHandler,
        traffic_seen_via=[],
    )
    proxies = {}
    for name in ("live", "seed", "private"):
        proxies[name] = _start_server(
            stack,
            _ForwardProxyHandler,
            healthy=True,
            proxy_name=name,
            requests_seen=0,
        )
    feed = _start_server(
        stack,
        _FeedHandler,
        fail_live=False,
        live_failures=0,
        requests_seen=[],
        responses_seen=[],
        transports_seen=[],
        fail_private=False,
        private_failures=0,
        proxy_ports={name: server.server_port for name, server in proxies.items()},
    )
    return target, proxies, feed


def _set_seed_proxy(profile: dict, seed_port: int):
    profile["proxy-providers"]["provider-seed"]["payload"] = [
        {
            "name": "seed-fixture",
            "type": "http",
            "server": "127.0.0.1",
            "port": seed_port,
        }
    ]


def _make_runtime_profile(
    config_data, write_yaml, provider_state_dir, users_path, feed, target, seed_port
):
    origin_url = f"http://127.0.0.1:{target.server_port}/probe"
    profile = _runtime_profile(
        config_data,
        write_yaml,
        provider_state_dir,
        users_path,
        origin_url,
    )
    for name in ("provider", "private"):
        profile["proxy-providers"][name]["url"] = (
            f"http://127.0.0.1:{feed.server_port}"
            + ("/live" if name == "provider" else "/private")
        )
    _set_seed_proxy(profile, seed_port)
    return profile


def test_live_health_transition_seed_recovery_and_fail_closed_fetch_routes(
    tmp_path, config_data, write_yaml, provider_state_dir, users_path
):
    with ExitStack() as stack:
        target, proxies, feed = _recovery_fixture_servers(stack)
        profile = _make_runtime_profile(
            config_data,
            write_yaml,
            provider_state_dir,
            users_path,
            feed,
            target,
            proxies["seed"].server_port,
        )
        process, base_url, profile, home = _start_mihomo(stack, profile, tmp_path)
        process.runtime_diagnostics = lambda: {
            "feed_requests": feed.requests_seen,
            "live_proxy_requests": proxies["live"].requests_seen,
            "seed_proxy_requests": proxies["seed"].requests_seen,
            "private_proxy_requests": proxies["private"].requests_seen,
        }
        mixed_port = profile["mixed-port"]

        _wait_for(
            lambda: "live-fixture" in _group(base_url, "PROVIDER-AUTO-LIVE").get("all", []),
            process,
            "LIVE provider membership",
        )
        _wait_for(
            lambda: "seed-fixture" in _group(base_url, "PROVIDER-AUTO-SEED").get("all", []),
            process,
            "SEED provider membership",
        )
        _wait_for(
            lambda: _provider_branch(base_url) == "live",
            process,
            "PROVIDER-AUTO to select LIVE while healthy",
        )

        status, body = _request_via_mihomo(mixed_port, target.server_port)
        provider_groups = [
            (name, _group(base_url, name))
            for name in ("PROVIDER-AUTO", "PROVIDER-AUTO-LIVE", "PROVIDER-AUTO-SEED")
        ]
        assert (status, body) == (200, b"target-response"), (
            f"status={status!r}, body={body!r}, "
            f"provider_groups={provider_groups!r}, "
            f"live_proxy_requests={proxies['live'].requests_seen}, "
            f"target_routes={target.traffic_seen_via}, "
            f"logs={process.log_path.read_text(encoding='utf-8', errors='replace')[-3000:]!r}"
        )
        assert target.traffic_seen_via[-1] == "live"

        proxies["live"].healthy = False
        _wait_for(
            lambda: _provider_branch(base_url) == "seed",
            process,
            "PROVIDER-AUTO to select SEED after LIVE becomes unhealthy",
        )
        status, body = _request_via_mihomo(mixed_port, target.server_port)
        assert (status, body) == (200, b"target-response")
        assert target.traffic_seen_via[-1] == "seed"

        proxies["live"].healthy = True
        _wait_for(
            lambda: _provider_branch(base_url) == "live",
            process,
            "PROVIDER-AUTO to return to recovered LIVE",
        )
        status, body = _request_via_mihomo(mixed_port, target.server_port)
        assert (status, body) == (200, b"target-response")
        assert target.traffic_seen_via[-1] == "live"

        direct_traffic_before = target.traffic_seen_via.count("DIRECT")
        for proxy in proxies.values():
            proxy.healthy = False
        _wait_for(
            lambda: (
                _group(base_url, "PROVIDER-AUTO-LIVE").get("alive") is False
                and _group(base_url, "PROVIDER-AUTO-SEED").get("alive") is False
            ),
            process,
            "both ordinary provider groups to report unhealthy",
        )
        status, _ = _request_via_mihomo(mixed_port, target.server_port)
        assert status is None or status >= 400
        assert target.traffic_seen_via.count("DIRECT") == direct_traffic_before
        provider_auto = _group(base_url, "PROVIDER-AUTO")
        assert "DIRECT" not in provider_auto.get("all", [])
        assert provider_auto.get("now") != "DIRECT"

        _wait_for(
            lambda: _group(base_url, "FETCH-PRIVATE").get("now") == "DIRECT",
            process,
            "FETCH-PRIVATE to use DIRECT after SEED becomes unhealthy",
        )
        _wait_for(
            lambda: _group(base_url, "FETCH-AUTO-PROVIDER").get("now") == "DIRECT",
            process,
            "FETCH-AUTO-PROVIDER to use DIRECT after PRIVATE and SEED become unhealthy",
        )
        assert "DIRECT" in _group(base_url, "FETCH-PRIVATE").get("all", [])
        assert "DIRECT" in _group(base_url, "FETCH-AUTO-PROVIDER").get("all", [])

        previous_live_updates = feed.responses_seen.count(("/live", 200))
        assert _force_provider_update(base_url, "provider") in {200, 204}
        _wait_for(
            lambda: feed.responses_seen.count(("/live", 200)) > previous_live_updates,
            process,
            "LIVE provider fetch to complete through the final DIRECT fallback",
        )
        previous_private_updates = feed.responses_seen.count(("/private", 200))
        assert _force_provider_update(base_url, "private") in {200, 204}
        _wait_for(
            lambda: feed.responses_seen.count(("/private", 200)) > previous_private_updates,
            process,
            "PRIVATE provider fetch to complete through the final DIRECT fallback",
        )


def test_cold_start_without_http_provider_caches_uses_inline_seed(
    tmp_path, config_data, write_yaml, provider_state_dir, users_path
):
    with ExitStack() as stack:
        target, proxies, feed = _recovery_fixture_servers(stack)
        feed.fail_live = True
        feed.fail_private = True
        profile = _make_runtime_profile(
            config_data,
            write_yaml,
            provider_state_dir,
            users_path,
            feed,
            target,
            proxies["seed"].server_port,
        )
        process, base_url, profile, home = _start_mihomo(stack, profile, tmp_path)

        provider_cache = home / "providers" / "provider.yaml"
        private_cache = home / "providers" / "private.yaml"
        assert not provider_cache.exists()
        assert not private_cache.exists()

        _wait_for(
            lambda: ("/live", 503) in feed.responses_seen
            and ("/private", 503) in feed.responses_seen,
            process,
            "both empty HTTP providers to fail on cold start",
        )
        assert ("/live", "seed") in feed.transports_seen
        assert ("/private", "seed") in feed.transports_seen
        assert not provider_cache.exists()
        assert not private_cache.exists()

        _wait_for(
            lambda: _provider_branch(base_url) == "seed",
            process,
            "PROVIDER-AUTO to choose the inline SEED before either HTTP cache exists",
        )
        status, body = _request_via_mihomo(profile["mixed-port"], target.server_port)
        assert (status, body) == (200, b"target-response")
        assert target.traffic_seen_via[-1] == "seed"

        feed.fail_private = False
        assert _force_provider_update(base_url, "private") in {200, 204}
        _wait_for(
            lambda: ("/private", 200) in feed.responses_seen
            and "private-fixture" in _group(base_url, "PRIVATE").get("all", []),
            process,
            "PRIVATE to recover through the inline SEED",
        )
        assert ("/private", "seed") in feed.transports_seen
        _wait_for(
            lambda: _group(base_url, "FETCH-AUTO-PROVIDER").get("now") == "PRIVATE",
            process,
            "LIVE fetch route to prefer recovered PRIVATE over healthy SEED",
        )

        feed.fail_live = False
        assert _force_provider_update(base_url, "provider") in {200, 204}
        _wait_for(
            lambda: ("/live", 200) in feed.responses_seen
            and "live-fixture" in _group(base_url, "PROVIDER-AUTO-LIVE").get("all", []),
            process,
            "LIVE to recover through the now-available PRIVATE group",
        )
        assert ("/live", "private") in feed.transports_seen
        _wait_for(
            lambda: _provider_branch(base_url) == "live",
            process,
            "PROVIDER-AUTO to prefer LIVE after its recovery",
        )


def test_failed_live_provider_refresh_preserves_working_runtime_cache(
    tmp_path, config_data, write_yaml, provider_state_dir, users_path
):
    with ExitStack() as stack:
        target, proxies, feed = _recovery_fixture_servers(stack)
        profile = _make_runtime_profile(
            config_data,
            write_yaml,
            provider_state_dir,
            users_path,
            feed,
            target,
            proxies["seed"].server_port,
        )
        process, base_url, profile, home = _start_mihomo(stack, profile, tmp_path)
        process.runtime_diagnostics = lambda: {
            "feed_requests": feed.requests_seen,
            "live_proxy_requests": proxies["live"].requests_seen,
            "seed_proxy_requests": proxies["seed"].requests_seen,
            "private_proxy_requests": proxies["private"].requests_seen,
        }
        mixed_port = profile["mixed-port"]

        _wait_for(
            lambda: "live-fixture" in _group(base_url, "PROVIDER-AUTO-LIVE").get("all", []),
            process,
            "initial LIVE provider cache",
        )
        _wait_for(
            lambda: _provider_branch(base_url) == "live",
            process,
            "LIVE selection before provider update failure",
        )
        _wait_for(
            lambda: _group(base_url, "PROVIDER-AUTO-LIVE").get("alive") is True,
            process,
            "healthy LIVE group before provider update failure",
        )
        cache_file = home / "providers" / "provider.yaml"
        _wait_for(lambda: cache_file.exists(), process, "persisted LIVE provider file")
        before = cache_file.read_bytes()
        assert b"live-fixture" in before

        proxies["private"].healthy = False
        proxies["seed"].healthy = False
        _wait_for(
            lambda: _group(base_url, "FETCH-AUTO-PROVIDER").get("now") == "DIRECT",
            process,
            "provider updater recovery route before forced failure",
        )
        feed.fail_live = True
        previous_failures = feed.live_failures
        _force_provider_update(base_url, "provider")
        _wait_for(
            lambda: feed.live_failures > previous_failures,
            process,
            "a recorded failed LIVE provider update",
        )
        assert ("/live", 503) in feed.responses_seen

        assert cache_file.read_bytes() == before
        live = _group(base_url, "PROVIDER-AUTO-LIVE")
        assert "live-fixture" in live.get("all", [])
        _wait_for(
            lambda: _provider_branch(base_url) == "live"
            and _group(base_url, "PROVIDER-AUTO-LIVE").get("alive") is True,
            process,
            "cached LIVE node remaining healthy after a failed update",
        )
        status, body = _request_via_mihomo(mixed_port, target.server_port)
        assert (status, body) == (200, b"target-response"), (
            f"status={status!r}, body={body!r}, "
            f"provider_auto={_group(base_url, 'PROVIDER-AUTO')!r}, "
            f"live_group={_group(base_url, 'PROVIDER-AUTO-LIVE')!r}, "
            f"live_proxy_requests={proxies['live'].requests_seen}, "
            f"target_routes={target.traffic_seen_via}, "
            f"logs={process.log_path.read_text(encoding='utf-8', errors='replace')[-3000:]!r}"
        )
        assert target.traffic_seen_via[-1] == "live"
