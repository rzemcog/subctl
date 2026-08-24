from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from conftest import (
    VALID_ALICE_TOKEN,
    VALID_BOB_TOKEN,
    VALID_PROVIDER_TOKEN,
    assert_no_traceback,
    assert_secret_not_printed,
)


PERSONAL_BODY = b"ss://YWVzLTEyOC1nY206cGFzcw@personal.example:443#personal\n"
PROVIDER_BODY = "trojan://pass@provider.example:443#provider\n"


def test_render_help_documents_modes(run_subctl):
    result = run_subctl("render", "--help")

    assert result.returncode == 0
    assert "--all" in result.stdout
    assert "--yaml-only" in result.stdout
    assert "--raw-only" in result.stdout
    assert "--user" in result.stdout


def test_render_default_all_and_user_mode(
    tmp_path, config_path, users_path, cli_paths, run_subctl
):
    _write_provider_cache(tmp_path)
    with _http_server(status=200, body=PERSONAL_BODY) as personal_url:
        _replace_user_urls(users_path, personal_url)

        result = run_subctl(*cli_paths, "render", "--all", "--user", "alice")

    assert result.returncode == 0, result.stderr
    assert "render summary: rendered=2 skipped=0 failed=0" in result.stdout
    assert (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.yaml").exists()
    assert (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw").exists()
    assert not (tmp_path / "public/s" / f"{VALID_BOB_TOKEN}.yaml").exists()
    assert not (tmp_path / "public/s" / f"{VALID_BOB_TOKEN}.raw").exists()
    assert personal_url not in result.stdout + result.stderr
    assert_secret_not_printed(result, VALID_ALICE_TOKEN, VALID_PROVIDER_TOKEN)


def test_render_yaml_only_does_not_require_provider_cache(cli_paths, tmp_path, run_subctl):
    result = run_subctl(*cli_paths, "render", "--yaml-only")

    assert result.returncode == 0, result.stderr
    assert "render summary: rendered=2 skipped=2 failed=0" in result.stdout
    assert (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.yaml").exists()
    assert not (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw").exists()


def test_render_raw_only_does_not_write_yaml(
    tmp_path, users_path, cli_paths, run_subctl
):
    _write_provider_cache(tmp_path)
    with _http_server(status=200, body=PERSONAL_BODY) as personal_url:
        _replace_user_urls(users_path, personal_url)

        result = run_subctl(*cli_paths, "render", "--raw-only", "--user", "alice")

    assert result.returncode == 0, result.stderr
    assert "render summary: rendered=1 skipped=1 failed=0" in result.stdout
    assert not (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.yaml").exists()
    assert (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw").exists()


def test_render_missing_provider_cache_exits_validation_without_secrets(
    cli_paths, run_subctl
):
    result = run_subctl(*cli_paths, "render", "--raw-only", "--user", "alice")

    assert result.returncode == 1
    assert_no_traceback(result)
    assert "refresh-provider" in result.stderr
    assert "https://provider.example/subscription" not in result.stdout + result.stderr
    assert_secret_not_printed(result, VALID_ALICE_TOKEN, VALID_PROVIDER_TOKEN)


def test_render_personal_network_failure_returns_upstream_code_and_keeps_raw(
    tmp_path, users_path, cli_paths, run_subctl
):
    _write_provider_cache(tmp_path)
    raw_path = tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("old raw", encoding="utf-8")
    with _http_server(status=500, body=b"nope") as personal_url:
        _replace_user_urls(users_path, personal_url)

        result = run_subctl(*cli_paths, "render", "--raw-only", "--user", "alice")

    assert result.returncode == 2
    assert "render summary: rendered=0 skipped=0 failed=1" in result.stdout
    assert raw_path.read_text(encoding="utf-8") == "old raw"
    assert personal_url not in result.stdout + result.stderr
    assert_secret_not_printed(result, VALID_ALICE_TOKEN, VALID_PROVIDER_TOKEN)


def test_render_partial_failure_returns_partial_code(
    tmp_path, users_path, cli_paths, run_subctl
):
    _write_provider_cache(tmp_path)
    with _selective_http_server() as base_url:
        _replace_user_urls(users_path, base_url)

        result = run_subctl(*cli_paths, "render", "--raw-only")

    assert result.returncode == 4
    assert "render summary: rendered=1 skipped=1 failed=1" in result.stdout
    assert "alice" in result.stderr
    assert (tmp_path / "public/s" / f"{VALID_BOB_TOKEN}.raw").exists()
    assert not (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw").exists()


def test_render_default_mode_is_all(
    tmp_path, users_path, cli_paths, run_subctl
):
    _write_provider_cache(tmp_path)
    with _http_server(status=200, body=PERSONAL_BODY) as personal_url:
        _replace_user_urls(users_path, personal_url)

        result = run_subctl(*cli_paths, "render", "--user", "alice")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.yaml").exists()
    assert (tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw").exists()


def _write_provider_cache(tmp_path):
    path = tmp_path / "state/cache/provider.decoded"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROVIDER_BODY, encoding="utf-8")


def _replace_user_urls(users_path, base_url):
    text = users_path.read_text(encoding="utf-8")
    text = text.replace("https://panel.example.com/sub/alice", f"{base_url}/alice")
    text = text.replace("https://panel.example.com/sub/bob", f"{base_url}/bob")
    users_path.write_text(text, encoding="utf-8")


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


class _selective_http_server(_http_server):
    def __init__(self):
        super().__init__(status=200, body=b"")

    def __enter__(self):
        class Handler(_StaticResponseHandler):
            def do_GET(self):
                if self.path.endswith("/alice"):
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"nope")
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(PERSONAL_BODY)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}/subscription"
