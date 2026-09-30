from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


RELEASE_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "release.py"
_SPEC = importlib.util.spec_from_file_location("subctl_release", RELEASE_SCRIPT)
release = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(release)


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
