from __future__ import annotations

import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest


RELEASE_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "release.py"
INSTALLER_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "install.sh"
SMOKE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke-caddy.py"
_SPEC = importlib.util.spec_from_file_location("subctl_release", RELEASE_SCRIPT)
release = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(release)
_SMOKE_SPEC = importlib.util.spec_from_file_location("subctl_smoke", SMOKE_SCRIPT)
smoke = importlib.util.module_from_spec(_SMOKE_SPEC)
assert _SMOKE_SPEC.loader is not None
_SMOKE_SPEC.loader.exec_module(smoke)


def make_staging_dir(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    files = {
        "package/subctl-0.1.0-py3-none-any.whl": b"application wheel",
        "wheelhouse/pyyaml-6.0.2-py3-none-any.whl": b"dependency wheel",
        "systemd/subctl-web.service": b"[Service]\nExecStart=/opt/subctl/venv/bin/subctl web\n",
        "systemd/subctl-refresh.service": b"[Service]\nType=oneshot\n",
        "systemd/subctl-refresh.timer": b"[Timer]\nOnCalendar=hourly\n",
        "Caddyfile": b"example.org { respond /health 200 }\n",
    }
    for relative, content in files.items():
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return staging


def test_release_installer_selects_caddyfile_adapter_for_staged_config():
    installer = INSTALLER_SCRIPT.read_text(encoding="utf-8")

    assert 'caddy validate --adapter caddyfile --config "$staged_caddy"' in installer


def test_smoke_connect_override_resolves_only_public_hostname(monkeypatch):
    calls = []

    def fake_getaddrinfo(host, *args, **kwargs):
        calls.append((host, args, kwargs))
        return []

    monkeypatch.setattr(smoke.socket, "getaddrinfo", fake_getaddrinfo)
    smoke.install_connect_ip_override("subscriptions.example", "127.0.0.1")

    smoke.socket.getaddrinfo("subscriptions.example", 443, type=1)
    smoke.socket.getaddrinfo("other.example", 80)

    assert calls == [
        ("127.0.0.1", (443,), {"type": 1}),
        ("other.example", (80,), {}),
    ]


def test_smoke_connect_override_rejects_non_ip_address():
    with pytest.raises(ValueError):
        smoke.install_connect_ip_override("subscriptions.example", "localhost")


def test_smoke_control_endpoint_requires_empty_204():
    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b""

    class FakeOpener:
        def open(self, request, timeout):
            assert request.full_url == "https://subscriptions.example/.well-known/subctl-control/204"
            assert timeout == 15
            return FakeResponse()

    smoke.check_empty_204(
        "https://subscriptions.example/.well-known/subctl-control/204",
        FakeOpener(),
    )


def test_smoke_control_endpoint_rejects_redirects_without_following_them():
    handler = smoke.NoRedirectHandler()
    request = urllib.request.Request("https://subscriptions.example/control")

    assert handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://other.example/204",
    ) is None
    control_opener = smoke._without_redirects(urllib.request.build_opener())
    redirect_handlers = [
        item
        for item in control_opener.handlers
        if isinstance(item, urllib.request.HTTPRedirectHandler)
    ]
    assert len(redirect_handlers) == 1
    assert isinstance(redirect_handlers[0], smoke.NoRedirectHandler)


def test_smoke_control_endpoint_reports_redirect_response():
    class RedirectingOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://other.example/204"},
                None,
            )

    with pytest.raises(SystemExit, match="control endpoint redirected"):
        smoke.check_empty_204(
            "https://subscriptions.example/.well-known/subctl-control/204",
            RedirectingOpener(),
        )


def test_smoke_can_skip_new_control_route_during_fallback(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "public:\n  base_url: https://subscriptions.example\n"
        "provider:\n  shared_token: synthetic-token\n",
        encoding="utf-8",
    )
    users_path = tmp_path / "users.yaml"
    users_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SUBCTL_CONFIG", str(config_path))
    monkeypatch.setenv("SUBCTL_USERS", str(users_path))
    monkeypatch.delenv("SUBCTL_SMOKE_CONNECT_IP", raising=False)
    monkeypatch.setenv("SUBCTL_SMOKE_CHECK_CONTROL_ENDPOINT", "0")
    requests = []
    monkeypatch.setattr(smoke, "fetch", lambda url, opener=None: requests.append(url))
    monkeypatch.setattr(
        smoke,
        "check_empty_204",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fallback must not require the new route")),
    )

    smoke.main()

    assert requests == ["https://subscriptions.example/feeds/provider/synthetic-token"]


@pytest.mark.parametrize(("scheme", "users"), [("http", "{}"), ("https", "{}")])
def test_smoke_base_url_scheme_is_checked_before_requests(monkeypatch, tmp_path, scheme, users):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"public:\n  base_url: {scheme}://subscriptions.example\n"
        "provider:\n  shared_token: synthetic-token\n",
        encoding="utf-8",
    )
    users_path = tmp_path / "users.yaml"
    users_path.write_text(users, encoding="utf-8")
    monkeypatch.setenv("SUBCTL_CONFIG", str(config_path))
    monkeypatch.setenv("SUBCTL_USERS", str(users_path))
    monkeypatch.delenv("SUBCTL_SMOKE_CONNECT_IP", raising=False)
    requests = []
    monkeypatch.setattr(smoke, "fetch", lambda url, opener=None: requests.append(("fetch", url)))
    monkeypatch.setattr(
        smoke,
        "check_empty_204",
        lambda url, opener=None: requests.append(("control", url)),
    )

    if scheme == "http":
        with pytest.raises(SystemExit, match="HTTPS hostname"):
            smoke.main()
        assert requests == []
    else:
        # An empty registry still schedules the independent control endpoint check.
        smoke.main()
        assert requests == [
            ("fetch", "https://subscriptions.example/feeds/provider/synthetic-token"),
            ("control", "https://subscriptions.example/.well-known/subctl-control/204"),
        ]


def test_runtime_verifier_pins_public_smoke_to_loopback(monkeypatch, tmp_path):
    calls = []
    package_info = {"application_version": "0.1.0"}
    monkeypatch.setattr(release, "_is_active", lambda unit: True)
    monkeypatch.setattr(release, "_verify_installed_assets", lambda manifest: None)
    monkeypatch.setattr(release, "_verify_installed_application", lambda bundle: package_info)
    monkeypatch.setattr(release, "_check_http_health", lambda: None)
    monkeypatch.setattr(release, "_run", lambda command, **kwargs: calls.append((command, kwargs.get("env"))))

    result = release._verify_runtime(tmp_path, {}, include_smoke=True)

    assert result == package_info
    assert calls[-1][1]["SUBCTL_SMOKE_CONNECT_IP"] == "127.0.0.1"
    assert calls[-1][1]["SUBCTL_SMOKE_CHECK_CONTROL_ENDPOINT"] == "1"


def test_runtime_verifier_can_skip_new_control_route_for_fallback_smoke(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(release, "_is_active", lambda unit: True)
    monkeypatch.setattr(release, "_verify_installed_assets", lambda manifest: None)
    monkeypatch.setattr(release, "_verify_installed_application", lambda bundle: {})
    monkeypatch.setattr(release, "_check_http_health", lambda: None)
    monkeypatch.setattr(release, "_run", lambda command, **kwargs: calls.append(kwargs.get("env")))

    release._verify_runtime(
        tmp_path,
        {},
        include_smoke=True,
        include_control_endpoint=False,
    )

    assert calls[-1]["SUBCTL_SMOKE_CONNECT_IP"] == "127.0.0.1"
    assert calls[-1]["SUBCTL_SMOKE_CHECK_CONTROL_ENDPOINT"] == "0"


def test_restore_fallback_does_not_require_new_control_route(monkeypatch, tmp_path):
    verification_calls = []
    monkeypatch.setattr(release, "_systemctl", lambda *_args: None)
    monkeypatch.setattr(release, "_is_active", lambda unit: unit == release.REFRESH_TIMER)
    monkeypatch.setattr(release, "_apply_bundle", lambda _bundle: None)
    monkeypatch.setattr(
        release,
        "_verify_runtime",
        lambda _bundle, _manifest, **kwargs: verification_calls.append(kwargs),
    )
    monkeypatch.setattr(release, "_resume_timer", lambda: None)
    monkeypatch.setattr(release, "_mark_active", lambda *_args, **_kwargs: None)

    release._restore_fallback(tmp_path / "fallback", {}, tmp_path / "releases", "failed-release")

    assert verification_calls == [{"include_smoke": True, "include_control_endpoint": False}]


def test_http_health_retries_transient_startup_failure(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, _size):
            return b"ok"

    requests = []
    delays = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        if len(requests) == 1:
            raise urllib.error.URLError("connection refused")
        return Response()

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(release.time, "sleep", delays.append)

    release._check_http_health()

    assert [url for url, _ in requests] == [
        "http://127.0.0.1:12790/",
        "http://127.0.0.1:12790/",
        "http://127.0.0.1:12790/health",
        "http://127.0.0.1:12790/healthz",
    ]
    assert all(timeout <= 2 for _, timeout in requests)
    assert delays == [0.25]


def test_http_health_fails_fast_for_client_error(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(release.ReleaseError, match="returned HTTP 404"):
        release._check_http_health()

    assert requests == ["http://127.0.0.1:12790/"]


def test_http_health_retries_server_error(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, _size):
            return b"ok"

    requests = []
    delays = []

    def fake_urlopen(request, timeout):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return Response()

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(release.time, "sleep", delays.append)

    release._check_http_health()

    assert requests == [
        "http://127.0.0.1:12790/",
        "http://127.0.0.1:12790/",
        "http://127.0.0.1:12790/health",
        "http://127.0.0.1:12790/healthz",
    ]
    assert delays == [0.25]


def test_bundle_binds_commit_to_immutable_artifact_and_hashes(tmp_path):
    staging = make_staging_dir(tmp_path)

    bundle = release.publish_bundle(
        tmp_path / "releases",
        "a" * 40,
        staging,
        historical_runtime_relation="inferred",
    )

    manifest = release.verify_bundle(bundle, expected_commit="a" * 40)
    assert manifest["git_commit"] == "a" * 40
    assert manifest["historical_runtime_relation"] == "inferred"
    assert manifest["application"]["sha256"]
    assert manifest["bundle_sha256"]
    assert release.release_id_for("a" * 40, manifest["bundle_sha256"]) == bundle.name
    assert (bundle / "package" / manifest["application"]["filename"]).is_file()
    assert not (bundle / "package" / manifest["application"]["filename"]).stat().st_mode & 0o222


def test_inferred_rollback_anchor_accepts_any_full_commit_sha(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    release_root.mkdir()
    commit = "a" * 40
    anchor = {
        "release_id": "selected-release",
        "git_commit": commit,
        "historical_runtime_relation": "inferred",
    }
    (release_root / "anchor.json").write_text(json.dumps(anchor), encoding="utf-8")
    bundle = release_root / "selected-release"
    manifest = {"git_commit": commit, "historical_runtime_relation": "inferred"}
    monkeypatch.setattr(release, "_load_release", lambda release_id, root: (bundle, manifest))

    loaded_anchor, anchor_bundle, loaded_manifest = release._load_anchor(release_root)

    assert loaded_anchor == anchor
    assert anchor_bundle == bundle
    assert loaded_manifest == manifest


def test_repeating_identical_publish_reuses_existing_bundle(tmp_path):
    release_root = tmp_path / "releases"
    first = release.publish_bundle(
        release_root,
        "b" * 40,
        make_staging_dir(tmp_path / "first"),
        historical_runtime_relation="inferred",
    )
    second = release.publish_bundle(
        release_root,
        "b" * 40,
        make_staging_dir(tmp_path / "second"),
        historical_runtime_relation="inferred",
    )

    assert second == first
    assert len(list(release_root.glob("*-*"))) == 1


def test_bundle_verification_rejects_modified_artifact(tmp_path):
    bundle = release.publish_bundle(
        tmp_path / "releases",
        "c" * 40,
        make_staging_dir(tmp_path),
    )
    artifact = next((bundle / "package").glob("*.whl"))
    artifact.chmod(0o644)
    artifact.write_bytes(b"modified after publication")
    artifact.chmod(0o444)

    with pytest.raises(release.ReleaseError, match="SHA256|bundle hash"):
        release.verify_bundle(bundle)


def test_failed_candidate_verification_selects_saved_fallback(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    fallback = release.publish_bundle(
        release_root, "d" * 40, make_staging_dir(tmp_path / "fallback")
    )
    candidate_stage = make_staging_dir(tmp_path / "candidate")
    app = candidate_stage / "package" / "subctl-0.1.0-py3-none-any.whl"
    app.write_bytes(b"candidate application wheel")
    candidate = release.publish_bundle(release_root, "e" * 40, candidate_stage)
    fallback_manifest = release.verify_bundle(fallback)
    candidate_manifest = release.verify_bundle(candidate)
    release._atomic_json(
        release_root / "current.json",
        {
            "release_id": fallback.name,
            "git_commit": fallback_manifest["git_commit"],
            "package_filename": fallback_manifest["application"]["filename"],
            "package_sha256": fallback_manifest["application"]["sha256"],
        },
    )
    restored = []
    monkeypatch.setattr(release, "_git_output", lambda *args, **kwargs: "")
    monkeypatch.setattr(release, "_require_root", lambda: None)
    monkeypatch.setattr(release, "_validate_production_prerequisites", lambda **kwargs: None)
    monkeypatch.setattr(release, "_validate_release_assets", lambda bundle: None)
    monkeypatch.setattr(release, "_stop_for_install", lambda: None)
    monkeypatch.setattr(release, "_apply_bundle", lambda bundle: None)
    monkeypatch.setattr(release, "_run_refresh_and_smoke", lambda *args: (_ for _ in ()).throw(release.ReleaseError("smoke failed")))
    monkeypatch.setattr(
        release,
        "_restore_fallback",
        lambda bundle, manifest, root, failed: restored.append((bundle.name, manifest["git_commit"], failed)),
    )

    with pytest.raises(release.ReleaseError, match="fallback .* was restored"):
        release.deploy_release(candidate.name, release_root=release_root)

    assert restored == [(fallback.name, fallback_manifest["git_commit"], candidate.name)]


def test_rollback_installs_retained_release_without_rebuilding(tmp_path, monkeypatch):
    release_root = tmp_path / "releases"
    retained_stage = make_staging_dir(tmp_path / "retained")
    retained_stage.joinpath("package", "subctl-0.1.0-py3-none-any.whl").write_bytes(b"retained wheel")
    retained = release.publish_bundle(release_root, "f" * 40, retained_stage)
    active = release.publish_bundle(
        release_root, "1" * 40, make_staging_dir(tmp_path / "active")
    )
    active_manifest = release.verify_bundle(active)
    retained_manifest = release.verify_bundle(retained)
    release._atomic_json(
        release_root / "current.json",
        {
            "release_id": active.name,
            "git_commit": active_manifest["git_commit"],
            "package_filename": active_manifest["application"]["filename"],
            "package_sha256": active_manifest["application"]["sha256"],
        },
    )
    installed = []
    monkeypatch.setattr(release, "_git_output", lambda *args, **kwargs: "")
    monkeypatch.setattr(release, "_require_root", lambda: None)
    monkeypatch.setattr(release, "_validate_production_prerequisites", lambda **kwargs: None)
    monkeypatch.setattr(release, "_validate_release_assets", lambda bundle: None)
    monkeypatch.setattr(release, "_stop_for_install", lambda: None)
    monkeypatch.setattr(release, "_apply_bundle", lambda bundle: installed.append(bundle.name))
    monkeypatch.setattr(release, "_systemctl", lambda *args: None)
    monkeypatch.setattr(release, "_run_refresh_and_smoke", lambda *args: {"application_version": "0.1.0"})
    monkeypatch.setattr(release, "_resume_timer", lambda: None)
    monkeypatch.setattr(release, "_is_active", lambda unit: True)
    monkeypatch.setattr(release, "_verify_runtime", lambda *args, **kwargs: {"application_version": "0.1.0"})
    monkeypatch.setattr(release, "build_bundle", lambda *args, **kwargs: pytest.fail("rollback rebuilt source"))

    release.rollback_release(retained.name, release_root=release_root)

    assert installed == [retained.name]
    assert json.loads((release_root / "current.json").read_text(encoding="utf-8"))["release_id"] == retained.name
    assert retained_manifest["application"]["sha256"] != active_manifest["application"]["sha256"]
