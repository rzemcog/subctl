import base64
import stat
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from conftest import VALID_PROVIDER_TOKEN, assert_no_traceback, assert_secret_not_printed
from subctl.config import load_config
from subctl.errors import SubctlError
from subctl.refresh import refresh_provider


PROXY_LINES = "ss://YWVzLTEyOC1nY206cGFzcw@example.com:443#node-1\n"


def test_refresh_provider_success_writes_cache_and_public_files(
    tmp_path, config_path, cli_paths, run_subctl
):
    body = base64.b64encode(PROXY_LINES.encode("utf-8"))

    with _http_server(status=200, body=body) as upstream_url:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            config_text.replace("https://provider.example/subscription", upstream_url),
            encoding="utf-8",
        )

        result = run_subctl(*cli_paths, "refresh-provider")

    assert result.returncode == 0, result.stderr
    assert "provider refreshed" in result.stdout
    assert upstream_url not in result.stdout + result.stderr
    assert_secret_not_printed(result, VALID_PROVIDER_TOKEN)

    state_dir = tmp_path / "state"
    public_dir = tmp_path / "public"
    expected_base64 = base64.b64encode(PROXY_LINES.encode("utf-8")).decode("ascii")
    assert (state_dir / "cache/provider.raw").read_text(encoding="utf-8") == expected_base64
    assert (state_dir / "cache/provider.decoded").read_text(encoding="utf-8") == PROXY_LINES
    assert (
        public_dir / "feeds/provider" / VALID_PROVIDER_TOKEN
    ).read_text(encoding="utf-8") == expected_base64
    assert stat.S_IMODE((public_dir / "feeds").stat().st_mode) == 0o755
    assert stat.S_IMODE((public_dir / "feeds/provider").stat().st_mode) == 0o755
    assert (
        stat.S_IMODE((public_dir / "feeds/provider" / VALID_PROVIDER_TOKEN).stat().st_mode)
        == 0o644
    )


def test_refresh_provider_http_error_does_not_create_files(tmp_path, config_path):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )

    def fetcher(url, timeout):
        raise urllib.error.HTTPError(url, 503, "unavailable", hdrs=None, fp=None)

    with pytest.raises(SubctlError, match="HTTP status 503"):
        refresh_provider(config, fetch_provider=fetcher)

    assert not (tmp_path / "state/cache/provider.raw").exists()
    assert not (tmp_path / "public/feeds/provider" / VALID_PROVIDER_TOKEN).exists()


def test_refresh_provider_timeout_does_not_create_files(tmp_path, config_path):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )

    def fetcher(url, timeout):
        raise TimeoutError

    with pytest.raises(SubctlError, match="timeout"):
        refresh_provider(config, fetch_provider=fetcher)

    assert not (tmp_path / "state/cache/provider.decoded").exists()


def test_refresh_provider_invalid_body_does_not_create_files(tmp_path, config_path):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )

    with pytest.raises(SubctlError, match="valid Base64|non-URI|UTF-8"):
        refresh_provider(config, fetch_provider=lambda url, timeout: b"not a subscription")

    assert not (tmp_path / "state/cache/provider.raw").exists()
    assert not (tmp_path / "public/feeds/provider" / VALID_PROVIDER_TOKEN).exists()


def test_refresh_provider_fetch_failure_keeps_existing_files(tmp_path, config_path):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    existing = _write_existing_files(tmp_path)

    def fetcher(url, timeout):
        raise OSError("connection failed")

    with pytest.raises(SubctlError, match="provider fetch failed"):
        refresh_provider(config, fetch_provider=fetcher)

    _assert_existing_files_unchanged(existing)


def test_refresh_provider_write_failure_rolls_back_existing_files(
    tmp_path, config_path, monkeypatch
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    existing = _write_existing_files(tmp_path)

    import subctl.refresh as refresh_module

    real_replace = refresh_module.os.replace

    failed = False

    def flaky_replace(src, dst):
        nonlocal failed
        if not failed and str(dst).endswith(VALID_PROVIDER_TOKEN):
            failed = True
            raise OSError("disk is full")
        return real_replace(src, dst)

    monkeypatch.setattr(refresh_module.os, "replace", flaky_replace)

    with pytest.raises(SubctlError, match="writing files"):
        refresh_provider(config, fetch_provider=lambda url, timeout: PROXY_LINES.encode("utf-8"))

    _assert_existing_files_unchanged(existing)


def test_refresh_provider_first_run_write_failure_leaves_no_partial_files(
    tmp_path, config_path, monkeypatch
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )

    import subctl.refresh as refresh_module

    real_replace = refresh_module.os.replace

    failed = False

    def flaky_replace(src, dst):
        nonlocal failed
        if not failed and str(dst).endswith(VALID_PROVIDER_TOKEN):
            failed = True
            raise OSError("disk is full")
        return real_replace(src, dst)

    monkeypatch.setattr(refresh_module.os, "replace", flaky_replace)

    with pytest.raises(SubctlError, match="writing files"):
        refresh_provider(config, fetch_provider=lambda url, timeout: PROXY_LINES.encode("utf-8"))

    assert not (tmp_path / "state/cache/provider.raw").exists()
    assert not (tmp_path / "state/cache/provider.decoded").exists()
    assert not (tmp_path / "public/feeds/provider" / VALID_PROVIDER_TOKEN).exists()


def test_refresh_provider_cli_http_error_hides_secrets(
    tmp_path, config_path, cli_paths, run_subctl
):
    with _http_server(status=500, body=b"nope") as upstream_url:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            config_text.replace("https://provider.example/subscription", upstream_url),
            encoding="utf-8",
        )

        result = run_subctl(*cli_paths, "refresh-provider")

    assert result.returncode != 0
    assert_no_traceback(result)
    assert upstream_url not in result.stdout + result.stderr
    assert_secret_not_printed(result, VALID_PROVIDER_TOKEN)
    assert "HTTP status 500" in result.stderr


def _write_existing_files(tmp_path):
    paths = {
        tmp_path / "state/cache/provider.raw": "old raw",
        tmp_path / "state/cache/provider.decoded": "old decoded",
        tmp_path / "public/feeds/provider" / VALID_PROVIDER_TOKEN: "old public",
    }
    for path, content in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return paths


def _assert_existing_files_unchanged(paths):
    for path, content in paths.items():
        assert path.read_text(encoding="utf-8") == content


class _StaticResponseHandler(BaseHTTPRequestHandler):
    status = 200
    body = b""

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):
        return


class _http_server:
    def __init__(self, *, status, body):
        self._status = status
        self._body = body
        self._server = None
        self._thread = None

    def __enter__(self):
        class Handler(_StaticResponseHandler):
            status = self._status
            body = self._body

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}/subscription"

    def __exit__(self, exc_type, exc, tb):
        self._server.shutdown()
        self._thread.join()
