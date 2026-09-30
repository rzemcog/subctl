#!/usr/bin/env python3
"""Build, verify, and operate immutable subctl production releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Iterable


RELEASE_ROOT = Path(os.environ.get("SUBCTL_RELEASE_ROOT", "/var/lib/subctl/releases"))
SOURCE_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.environ.get("SUBCTL_APP_DIR", "/root/subctl"))
VENV_DIR = Path(os.environ.get("SUBCTL_VENV_DIR", "/opt/subctl/venv"))
CONFIG_FILE = Path(os.environ.get("SUBCTL_CONFIG_FILE", "/etc/subctl/config.yaml"))
UNIT_DIR = Path("/etc/systemd/system")
CADDY_FILE = Path("/etc/caddy/Caddyfile")
WEB_SERVICE = "subctl-web.service"
REFRESH_SERVICE = "subctl-refresh.service"
REFRESH_TIMER = "subctl-refresh.timer"
REQUIRED_UNITS = (WEB_SERVICE, REFRESH_SERVICE, REFRESH_TIMER)
FORMAT_VERSION = 1
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")


class ReleaseError(RuntimeError):
    """A release is invalid or an operation could not be completed safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_id_for(commit: str, bundle_sha256: str) -> str:
    return f"{commit}-{bundle_sha256}"


def _safe_relative_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError(f"release payload is not a regular directory: {root}")
    found: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseError(f"symlinks are not allowed in a release: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if path.relative_to(root).as_posix() == "manifest.json":
            continue
        if not path.is_file():
            raise ReleaseError(f"release contains a non-regular file: {path.relative_to(root)}")
        found.append(path.relative_to(root))
    return sorted(found, key=lambda path: path.as_posix())


def _validate_layout(root: Path) -> list[Path]:
    files = _safe_relative_files(root)
    names = {path.as_posix() for path in files}
    app_wheels = [path for path in files if path.parts[0] == "package" and path.suffix == ".whl"]
    dependency_wheels = [path for path in files if path.parts[0] == "wheelhouse" and path.suffix == ".whl"]
    if len(app_wheels) != 1 or len(app_wheels[0].parts) != 2:
        raise ReleaseError("release must contain exactly one wheel under package/")
    if not dependency_wheels or any(len(path.parts) != 2 for path in dependency_wheels):
        raise ReleaseError("release must contain flat dependency wheels under wheelhouse/")
    required = {f"systemd/{name}" for name in REQUIRED_UNITS} | {"Caddyfile"}
    if not required.issubset(names):
        missing = sorted(required - names)
        raise ReleaseError(f"release is missing required infrastructure files: {', '.join(missing)}")
    allowed = {"Caddyfile"}
    for path in files:
        parts = path.parts
        if len(parts) != 2 or parts[0] not in {"package", "wheelhouse", "systemd"}:
            if path.as_posix() not in allowed:
                raise ReleaseError(f"unexpected release file: {path.as_posix()}")
        elif parts[0] == "systemd" and parts[1] not in REQUIRED_UNITS:
            raise ReleaseError(f"unexpected systemd file: {path.as_posix()}")
        elif parts[0] == "package" and path != app_wheels[0]:
            raise ReleaseError(f"unexpected package file: {path.as_posix()}")
        elif parts[0] == "wheelhouse" and path.suffix != ".whl":
            raise ReleaseError(f"unexpected dependency artifact: {path.as_posix()}")
    return files


def _bundle_records(root: Path, files: Iterable[Path]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for relative in files:
        path = root / relative
        records[relative.as_posix()] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return records


def _bundle_sha256(records: dict[str, dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for name, record in sorted(records.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _manifest_for(
    commit: str,
    records: dict[str, dict[str, Any]],
    *,
    historical_runtime_relation: str | None,
    build_environment: dict[str, str] | None,
    built_at: str | None,
) -> dict[str, Any]:
    bundle_hash = _bundle_sha256(records)
    app_path = next(name for name in records if name.startswith("package/"))
    return {
        "format_version": FORMAT_VERSION,
        "release_id": release_id_for(commit, bundle_hash),
        "git_commit": commit,
        "source_provenance": "git archive",
        "historical_runtime_relation": historical_runtime_relation,
        "bundle_sha256": bundle_hash,
        "built_at_utc": built_at or utc_now(),
        "build_environment": build_environment or {},
        "application": {
            "filename": Path(app_path).name,
            "sha256": records[app_path]["sha256"],
        },
        "dependency_wheels": [
            {"filename": Path(name).name, "sha256": record["sha256"]}
            for name, record in sorted(records.items())
            if name.startswith("wheelhouse/")
        ],
        "systemd": {
            name: records[f"systemd/{name}"]["sha256"] for name in REQUIRED_UNITS
        },
        "caddyfile_sha256": records["Caddyfile"]["sha256"],
        "files": records,
    }


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            path.chmod(0o444)
    root.chmod(0o555)


def _make_writable_and_remove(root: Path) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o644)
    root.chmod(0o755)
    shutil.rmtree(root, ignore_errors=True)


def publish_bundle(
    release_root: Path,
    git_commit: str,
    source_dir: Path,
    *,
    historical_runtime_relation: str | None = None,
    build_environment: dict[str, str] | None = None,
    built_at: str | None = None,
) -> Path:
    """Publish a validated payload once; an identical existing bundle is reused."""
    git_commit = git_commit.lower()
    if not COMMIT_RE.fullmatch(git_commit):
        raise ReleaseError("Git commit must be a full 40-character SHA")
    if historical_runtime_relation not in (None, "inferred"):
        raise ReleaseError("historical runtime relation can only be recorded as inferred")
    source_dir = source_dir.resolve(strict=True)
    files = _validate_layout(source_dir)
    records = _bundle_records(source_dir, files)
    manifest = _manifest_for(
        git_commit,
        records,
        historical_runtime_relation=historical_runtime_relation,
        build_environment=build_environment,
        built_at=built_at,
    )

    release_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    target = release_root / manifest["release_id"]
    if target.exists():
        existing = verify_bundle(target, expected_commit=git_commit)
        for key in ("bundle_sha256", "historical_runtime_relation"):
            if existing.get(key) != manifest.get(key):
                raise ReleaseError(f"existing release has conflicting {key}; refusing to overwrite")
        return target

    staged = Path(tempfile.mkdtemp(prefix=".staging-", dir=release_root))
    try:
        for relative in files:
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_dir / relative, destination)
        (staged / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        _make_read_only(staged)
        try:
            os.rename(staged, target)
        except FileExistsError:
            existing = verify_bundle(target, expected_commit=git_commit)
            if existing.get("bundle_sha256") != manifest["bundle_sha256"]:
                raise ReleaseError("release ID collision with different bundle contents")
            _make_writable_and_remove(staged)
            return target
    except Exception:
        _make_writable_and_remove(staged)
        raise
    return target


def verify_bundle(release_dir: Path, *, expected_commit: str | None = None) -> dict[str, Any]:
    """Validate bundle shape, every payload hash, commit binding, and release ID."""
    if release_dir.is_symlink():
        raise ReleaseError("release directory cannot be a symlink")
    release_dir = release_dir.resolve(strict=True)
    manifest_path = release_dir / "manifest.json"
    if not release_dir.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ReleaseError("release directory and manifest must be regular files/directories")
    if os.name == "posix":
        protected_paths = [release_dir, manifest_path, *release_dir.rglob("*")]
        for path in protected_paths:
            if path.is_dir() or path.is_file():
                if stat.S_IMODE(path.stat().st_mode) & 0o222:
                    raise ReleaseError(f"release contains a writable path: {path.relative_to(release_dir.parent)}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read release manifest: {exc}") from exc
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ReleaseError("unsupported release manifest format")
    commit = manifest.get("git_commit", "")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise ReleaseError("manifest does not contain a full Git commit SHA")
    if expected_commit and commit != expected_commit.lower():
        raise ReleaseError(f"release commit {commit} does not match expected {expected_commit}")
    relation = manifest.get("historical_runtime_relation")
    if relation not in (None, "inferred"):
        raise ReleaseError("invalid historical runtime relation in manifest")
    files = _validate_layout(release_dir)
    actual = _bundle_records(release_dir, files)
    if actual != manifest.get("files"):
        raise ReleaseError("release file inventory or SHA256 mismatch")
    bundle_hash = _bundle_sha256(actual)
    release_id = release_id_for(commit, bundle_hash)
    if bundle_hash != manifest.get("bundle_sha256"):
        raise ReleaseError("bundle hash mismatch")
    if manifest.get("release_id") != release_id or release_dir.name != release_id:
        raise ReleaseError("release ID does not match commit and bundle content")
    app_path = next(name for name in actual if name.startswith("package/"))
    if manifest.get("application") != {
        "filename": Path(app_path).name,
        "sha256": actual[app_path]["sha256"],
    }:
        raise ReleaseError("application wheel metadata does not match its artifact")
    expected_dependencies = [
        {"filename": Path(name).name, "sha256": record["sha256"]}
        for name, record in sorted(actual.items())
        if name.startswith("wheelhouse/")
    ]
    if manifest.get("dependency_wheels") != expected_dependencies:
        raise ReleaseError("dependency wheel metadata does not match its artifacts")
    expected_units = {name: actual[f"systemd/{name}"]["sha256"] for name in REQUIRED_UNITS}
    if manifest.get("systemd") != expected_units:
        raise ReleaseError("systemd metadata does not match its snapshots")
    if manifest.get("caddyfile_sha256") != actual["Caddyfile"]["sha256"]:
        raise ReleaseError("Caddyfile metadata does not match its snapshot")
    return manifest


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Run an operation without echoing potentially sensitive command output."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReleaseError(f"cannot execute {Path(command[0]).name}: {exc}") from exc
    if result.returncode:
        raise ReleaseError(f"{Path(command[0]).name} failed with exit status {result.returncode}")
    return result.stdout.strip()


def _git_output(*args: str, cwd: Path | None = None) -> str:
    return _run(["git", *args], cwd=cwd or SOURCE_ROOT)


def _ensure_source_commit(commit: str, *, require_head: bool) -> None:
    if not COMMIT_RE.fullmatch(commit):
        raise ReleaseError("--commit must be a full 40-character Git SHA")
    if _git_output("status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("source checkout is dirty; refusing to build a release")
    _git_output("cat-file", "-e", f"{commit}^{{commit}}")
    if require_head and _git_output("rev-parse", "HEAD") != commit:
        raise ReleaseError("build commit must equal the clean source checkout HEAD")
    _run(["git", "merge-base", "--is-ancestor", commit, "origin/main"], cwd=SOURCE_ROOT)


def _extract_git_archive(commit: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=SOURCE_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if archive.returncode:
        raise ReleaseError(f"git archive failed with exit status {archive.returncode}")
    destination_resolved = destination.resolve()
    with tarfile.open(fileobj=__import__("io").BytesIO(archive.stdout), mode="r:") as tar:
        members = tar.getmembers()
        for member in members:
            candidate = (destination_resolved / member.name).resolve()
            if candidate != destination_resolved and destination_resolved not in candidate.parents:
                raise ReleaseError("Git archive contains a path outside the build directory")
            if not (member.isfile() or member.isdir()):
                raise ReleaseError("Git archive contains a link or special file")
        tar.extractall(destination_resolved, members=members)


def _render_caddy(template: bytes, domain: str) -> bytes:
    if not DOMAIN_RE.fullmatch(domain):
        raise ReleaseError("domain may contain only letters, digits, dots, and hyphens")
    rendered = template.replace(b"{$SUBCTL_DOMAIN}", domain.encode("ascii"))
    if b"{$SUBCTL_DOMAIN}" in rendered:
        raise ReleaseError("Caddy template contains an unresolved domain placeholder")
    return rendered


def _build_environment() -> dict[str, str]:
    def version(command: list[str]) -> str:
        try:
            return subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return "unavailable"

    return {
        "python": sys.version.split()[0],
        "node": version(["node", "--version"]),
        "npm": version(["npm", "--version"]),
        "pip": version([sys.executable, "-m", "pip", "--version"]),
    }


def build_bundle(
    commit: str,
    domain: str,
    *,
    historical_runtime_relation: str | None = None,
    require_head: bool = True,
    release_root: Path = RELEASE_ROOT,
    infrastructure_snapshot: Path | None = None,
) -> Path:
    """Build the UI, app wheel, dependencies, and infrastructure from one Git tree."""
    commit = commit.lower()
    _ensure_source_commit(commit, require_head=require_head)
    with tempfile.TemporaryDirectory(prefix="subctl-release-build-") as temp_name:
        work = Path(temp_name)
        source = work / "source"
        source.mkdir()
        _extract_git_archive(commit, source)
        web_dir = source / "web"
        if (web_dir / "package.json").is_file():
            _run(["npm", "ci", "--prefix", str(web_dir)], cwd=source)
            _run(["npm", "run", "build", "--prefix", str(web_dir)], cwd=source)

        stage = work / "bundle"
        for name in ("package", "wheelhouse", "systemd"):
            (stage / name).mkdir(parents=True, exist_ok=True)
        python = Path(sys.executable)
        _run(
            [str(python), "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(stage / "package"), str(source)],
            cwd=source,
        )
        app_wheels = list((stage / "package").glob("*.whl"))
        if len(app_wheels) != 1:
            raise ReleaseError("package build did not produce exactly one application wheel")
        _run(
            [
                str(python), "-m", "pip", "download", "--only-binary=:all:",
                "--dest", str(stage / "wheelhouse"), str(app_wheels[0]),
            ],
            cwd=source,
        )
        for wheel in (stage / "wheelhouse").glob("*.whl"):
            if wheel.name.lower().startswith("subctl-"):
                wheel.unlink()
        if not list((stage / "wheelhouse").glob("*.whl")):
            raise ReleaseError("dependency resolution produced no runtime wheels")

        if infrastructure_snapshot is not None:
            for name in REQUIRED_UNITS:
                src = infrastructure_snapshot / name
                if not src.is_file():
                    raise ReleaseError(f"infrastructure snapshot is missing {name}")
                shutil.copyfile(src, stage / "systemd" / name)
            caddy_source = infrastructure_snapshot / "Caddyfile"
            if not caddy_source.is_file():
                raise ReleaseError("infrastructure snapshot is missing Caddyfile")
            shutil.copyfile(caddy_source, stage / "Caddyfile")
        else:
            expected_names = {f"deploy/{name}" for name in REQUIRED_UNITS}
            for relative in sorted(expected_names):
                src = source / relative
                if not src.is_file():
                    raise ReleaseError(f"source commit is missing {relative}")
                shutil.copyfile(src, stage / "systemd" / Path(relative).name)
            caddy_template = source / "deploy" / "Caddyfile"
            if not caddy_template.is_file():
                raise ReleaseError("source commit is missing deploy/Caddyfile")
            (stage / "Caddyfile").write_bytes(_render_caddy(caddy_template.read_bytes(), domain))
        return publish_bundle(
            release_root,
            commit,
            stage,
            historical_runtime_relation=historical_runtime_relation,
            build_environment=_build_environment(),
        )


@contextmanager
def deployment_lock(release_root: Path = RELEASE_ROOT):
    release_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    lock_path = release_root / ".deploy.lock"
    stream = lock_path.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError("another release operation holds the deployment lock") from exc
        except ImportError as exc:
            raise ReleaseError("release deployment is supported only on Linux") from exc
        yield
    finally:
        stream.close()


def _require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ReleaseError("run production release operations as root")


def _command_ok(command: list[str]) -> bool:
    try:
        return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _systemctl(*args: str) -> None:
    _run(["systemctl", *args])


def _is_active(unit: str) -> bool:
    return _command_ok(["systemctl", "is-active", "--quiet", unit])


def _atomic_json(path: Path, data: dict[str, Any], *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o444 if immutable else 0o644)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseError(f"{path.name} must contain a JSON object")
    return data


def _load_release(release_id: str, release_root: Path = RELEASE_ROOT) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{40}-[0-9a-f]{64}", release_id):
        raise ReleaseError("release ID must contain the full commit and bundle SHA256")
    path = release_root / release_id
    if path.parent.resolve() != release_root.resolve():
        raise ReleaseError("release path escapes the release store")
    bundle, manifest = path, verify_bundle(path)
    try:
        _git_output("cat-file", "-e", f"{manifest['git_commit']}^{{commit}}")
    except ReleaseError as exc:
        raise ReleaseError(f"release Git commit is not present in the source repository: {manifest['git_commit']}") from exc
    return bundle, manifest


def _load_current_release(
    release_root: Path,
) -> tuple[dict[str, Any] | None, Path | None, dict[str, Any] | None]:
    current = _read_json(release_root / "current.json")
    if current is None:
        return None, None, None
    bundle, manifest = _load_release(current.get("release_id", ""), release_root)
    expected = {
        "release_id": bundle.name,
        "git_commit": manifest["git_commit"],
        "package_filename": manifest["application"]["filename"],
        "package_sha256": manifest["application"]["sha256"],
    }
    if any(current.get(key) != value for key, value in expected.items()):
        raise ReleaseError("current.json does not match its immutable release manifest")
    return current, bundle, manifest


def _load_anchor(release_root: Path) -> tuple[dict[str, Any] | None, Path | None, dict[str, Any] | None]:
    anchor = _read_json(release_root / "anchor.json")
    if anchor is None:
        return None, None, None
    if (
        anchor.get("git_commit") != "780fe63d697230f728ec8f3c7584d65b360730f1"
        or anchor.get("historical_runtime_relation") != "inferred"
    ):
        raise ReleaseError("anchor.json does not describe the accepted inferred 780fe63 baseline")
    bundle, manifest = _load_release(anchor.get("release_id", ""), release_root)
    if manifest.get("git_commit") != anchor["git_commit"] or manifest.get("historical_runtime_relation") != "inferred":
        raise ReleaseError("rollback anchor record does not match its immutable release bundle")
    return anchor, bundle, manifest


def _validate_production_prerequisites(
    *, require_active_services: bool = True, require_caddy_active: bool = True
) -> None:
    for label, path in (("production config", CONFIG_FILE), ("runtime Python", VENV_DIR / "bin" / "python")):
        if not path.is_file():
            raise ReleaseError(f"missing {label}: {path}")
    if require_active_services:
        for unit in (WEB_SERVICE, REFRESH_TIMER):
            if not _is_active(unit):
                raise ReleaseError(f"expected {unit} to be active before deployment")
    if _is_active(REFRESH_SERVICE):
        raise ReleaseError(f"{REFRESH_SERVICE} is currently running; wait for it to finish")
    if require_caddy_active and not _is_active("caddy.service"):
        raise ReleaseError("caddy.service is not active")


def _validate_live_snapshot(commit: str, domain: str, snapshot_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="subctl-release-check-") as temp_name:
        source = Path(temp_name) / "source"
        source.mkdir()
        _extract_git_archive(commit, source)
        for name in REQUIRED_UNITS:
            active = UNIT_DIR / name
            expected = source / "deploy" / name
            if not active.is_file() or active.is_symlink() or active.read_bytes() != expected.read_bytes():
                raise ReleaseError(f"active {name} does not match the selected 780fe63 baseline")
            shutil.copyfile(active, snapshot_dir / name)
        active_caddy = CADDY_FILE
        expected_caddy = _render_caddy((source / "deploy" / "Caddyfile").read_bytes(), domain)
        if not active_caddy.is_file() or active_caddy.is_symlink() or active_caddy.read_bytes() != expected_caddy:
            raise ReleaseError("active Caddyfile does not match the selected baseline and domain")
        shutil.copyfile(active_caddy, snapshot_dir / "Caddyfile")
    _run(["systemd-analyze", "verify", *[str(snapshot_dir / name) for name in REQUIRED_UNITS]])
    _run(["caddy", "validate", "--config", str(snapshot_dir / "Caddyfile")])


def bootstrap(commit: str, domain: str, *, release_root: Path = RELEASE_ROOT) -> Path:
    _require_root()
    commit = commit.lower()
    if commit != "780fe63d697230f728ec8f3c7584d65b360730f1":
        raise ReleaseError("the first rollback anchor is fixed to the accepted 780fe63 baseline")
    anchor_path = release_root / "anchor.json"
    if anchor_path.exists():
        anchor, bundle, _ = _load_anchor(release_root)
        if anchor is None or anchor.get("git_commit") != commit or bundle is None:
            raise ReleaseError("an incompatible rollback anchor is already recorded")
        return bundle
    if not CONFIG_FILE.is_file():
        raise ReleaseError(f"missing production config: {CONFIG_FILE}")
    _validate_production_prerequisites()
    with tempfile.TemporaryDirectory(prefix="subctl-release-anchor-") as temp_name:
        snapshot = Path(temp_name) / "infrastructure"
        snapshot.mkdir()
        _validate_live_snapshot(commit, domain, snapshot)
        bundle = build_bundle(
            commit,
            domain,
            historical_runtime_relation="inferred",
            require_head=False,
            release_root=release_root,
            infrastructure_snapshot=snapshot,
        )
    manifest = verify_bundle(bundle, expected_commit=commit)
    _atomic_json(
        anchor_path,
        {
            "release_id": manifest["release_id"],
            "git_commit": commit,
            "historical_runtime_relation": "inferred",
            "note": (
                "780fe63 is the nearest reproducible Git baseline to the production process that was running; "
                "the exact in-memory runtime revision is not proven."
            ),
            "recorded_at_utc": utc_now(),
        },
        immutable=True,
    )
    return bundle


def _verify_installed_application(bundle: Path) -> dict[str, Any]:
    inspect_script = r'''
import json, sys, zipfile
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path

bundle = Path(sys.argv[1])
app_wheel = next((bundle / "package").glob("*.whl"))
dist = metadata.distribution("subctl")
with zipfile.ZipFile(app_wheel) as archive:
    members = [name for name in archive.namelist() if name.startswith("subctl/") and not name.endswith("/")]
    for name in members:
        installed = Path(dist.locate_file(name))
        if not installed.is_file() or installed.read_bytes() != archive.read(name):
            raise SystemExit("installed application files differ from the retained wheel")
    metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
    expected = BytesParser().parsebytes(archive.read(metadata_name))
if dist.version != expected["Version"]:
    raise SystemExit("installed application version differs from retained wheel")
dependencies = []
for wheel in sorted((bundle / "wheelhouse").glob("*.whl")):
    with zipfile.ZipFile(wheel) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        expected = BytesParser().parsebytes(archive.read(name))
    installed = metadata.distribution(expected["Name"])
    if installed.version != expected["Version"]:
        raise SystemExit("installed dependency differs from retained wheelhouse")
    dependencies.append({"name": expected["Name"], "version": expected["Version"]})
print(json.dumps({"application_version": dist.version, "dependencies": dependencies}, sort_keys=True))
'''
    output = _run([str(VENV_DIR / "bin" / "python"), "-c", inspect_script, str(bundle)])
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ReleaseError("runtime provenance check returned invalid data") from exc


def _verify_installed_assets(manifest: dict[str, Any]) -> None:
    for name, expected_hash in manifest["systemd"].items():
        installed = UNIT_DIR / name
        if not installed.is_file() or sha256_file(installed) != expected_hash:
            raise ReleaseError(f"installed systemd unit does not match release: {name}")
    if not CADDY_FILE.is_file() or sha256_file(CADDY_FILE) != manifest["caddyfile_sha256"]:
        raise ReleaseError("installed Caddyfile does not match release")


def _check_http_health() -> None:
    for path in ("/", "/health", "/healthz"):
        request = urllib.request.Request(f"http://127.0.0.1:12790{path}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status != 200:
                    raise ReleaseError(f"local health request {path} returned HTTP {response.status}")
                response.read(4096)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReleaseError(f"local health request failed for {path}") from exc


def _verify_runtime(bundle: Path, manifest: dict[str, Any], *, include_smoke: bool) -> dict[str, Any]:
    if not _is_active(WEB_SERVICE):
        raise ReleaseError(f"{WEB_SERVICE} is not active")
    if not _is_active("caddy.service"):
        raise ReleaseError("caddy.service is not active")
    _verify_installed_assets(manifest)
    _run([str(VENV_DIR / "bin" / "python"), "-m", "pip", "check"])
    package_info = _verify_installed_application(bundle)
    _run(["caddy", "validate", "--config", str(CADDY_FILE)])
    _check_http_health()
    if include_smoke:
        _run([str(VENV_DIR / "bin" / "python"), str(SOURCE_ROOT / "scripts" / "smoke-caddy.py")])
    return package_info


def _write_event(
    release_root: Path,
    *,
    release_id: str,
    manifest: dict[str, Any],
    operation: str,
    restored_from: str | None = None,
) -> None:
    events_dir = release_root / "events"
    events_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    event = {
        "operation_id": uuid.uuid4().hex,
        "operation": operation,
        "release_id": release_id,
        "git_commit": manifest["git_commit"],
        "package_filename": manifest["application"]["filename"],
        "package_sha256": manifest["application"]["sha256"],
        "completed_at_utc": utc_now(),
        "restored_from_release_id": restored_from,
    }
    _atomic_json(events_dir / f"{event['operation_id']}.json", event, immutable=True)


def _mark_active(
    release_root: Path,
    bundle: Path,
    manifest: dict[str, Any],
    *,
    operation: str,
    restored_from: str | None = None,
) -> None:
    _atomic_json(
        release_root / "current.json",
        {
            "release_id": bundle.name,
            "git_commit": manifest["git_commit"],
            "package_filename": manifest["application"]["filename"],
            "package_sha256": manifest["application"]["sha256"],
            "activated_at_utc": utc_now(),
        },
    )
    _write_event(
        release_root,
        release_id=bundle.name,
        manifest=manifest,
        operation=operation,
        restored_from=restored_from,
    )


def _apply_bundle(bundle: Path) -> None:
    _run(["bash", str(SOURCE_ROOT / "deploy" / "install.sh"), "--release-dir", str(bundle)])


def _stop_for_install() -> None:
    if _is_active(REFRESH_SERVICE):
        raise ReleaseError(f"{REFRESH_SERVICE} started during deployment preflight")
    _systemctl("stop", REFRESH_TIMER)
    _systemctl("stop", WEB_SERVICE)


def _resume_timer() -> None:
    _systemctl("enable", "--now", REFRESH_TIMER)


def _run_refresh_and_smoke(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if _is_active(REFRESH_SERVICE):
        raise ReleaseError(f"{REFRESH_SERVICE} is already active; refusing a duplicate refresh")
    _systemctl("reset-failed", REFRESH_SERVICE)
    _systemctl("start", REFRESH_SERVICE)
    result = _run(["systemctl", "show", "--property=Result", "--value", REFRESH_SERVICE])
    if result != "success":
        raise ReleaseError(f"{REFRESH_SERVICE} result was {result or 'unknown'}")
    return _verify_runtime(bundle, manifest, include_smoke=True)


def _restore_fallback(
    fallback_bundle: Path,
    fallback_manifest: dict[str, Any],
    release_root: Path,
    failed_release_id: str,
) -> None:
    try:
        _systemctl("stop", REFRESH_TIMER)
        if _is_active(REFRESH_SERVICE):
            raise ReleaseError(f"{REFRESH_SERVICE} remains active; cannot safely restore")
        _systemctl("stop", WEB_SERVICE)
        _apply_bundle(fallback_bundle)
        _systemctl("restart", WEB_SERVICE)
        _verify_runtime(fallback_bundle, fallback_manifest, include_smoke=True)
        _resume_timer()
        if not _is_active(REFRESH_TIMER):
            raise ReleaseError(f"{REFRESH_TIMER} did not resume after recovery")
        _mark_active(
            release_root,
            fallback_bundle,
            fallback_manifest,
            operation="automatic-rollback",
            restored_from=failed_release_id,
        )
    except Exception as exc:
        try:
            _systemctl("stop", REFRESH_TIMER)
            _systemctl("stop", WEB_SERVICE)
        except Exception:
            pass
        raise ReleaseError(f"fallback restoration failed; web service left stopped: {exc}") from exc


def deploy_release(
    release_id: str,
    *,
    operation: str = "deployment",
    release_root: Path = RELEASE_ROOT,
) -> dict[str, Any]:
    _require_root()
    candidate_bundle, candidate_manifest = _load_release(release_id, release_root)
    current, current_bundle, current_manifest = _load_current_release(release_root)
    if current and current.get("release_id") == release_id:
        return _verify_runtime(candidate_bundle, candidate_manifest, include_smoke=False)

    fallback_id = current_bundle.name if current_bundle else None
    if not fallback_id:
        anchor, anchor_bundle, _ = _load_anchor(release_root)
        fallback_id = anchor_bundle.name if anchor and anchor_bundle else None
    if not fallback_id or fallback_id == release_id:
        raise ReleaseError("no distinct verified rollback release is available")
    fallback_bundle, fallback_manifest = _load_release(fallback_id, release_root)
    _validate_production_prerequisites(
        require_active_services=(operation == "deployment"),
        require_caddy_active=(operation == "deployment"),
    )
    try:
        _stop_for_install()
        _apply_bundle(candidate_bundle)
        _systemctl("restart", WEB_SERVICE)
        package_info = _run_refresh_and_smoke(candidate_bundle, candidate_manifest)
        _resume_timer()
        if not _is_active(REFRESH_TIMER):
            raise ReleaseError(f"{REFRESH_TIMER} is not active after deployment")
        package_info = _verify_runtime(candidate_bundle, candidate_manifest, include_smoke=False)
        _mark_active(release_root, candidate_bundle, candidate_manifest, operation=operation)
        return package_info
    except Exception as exc:
        try:
            _restore_fallback(fallback_bundle, fallback_manifest, release_root, release_id)
        except Exception as recovery_exc:
            raise ReleaseError(f"deployment failed ({exc}); {recovery_exc}") from recovery_exc
        raise ReleaseError(f"deployment failed and fallback {fallback_id} was restored: {exc}") from exc


def rollback_release(release_id: str, *, release_root: Path = RELEASE_ROOT) -> dict[str, Any]:
    current, _, _ = _load_current_release(release_root)
    if current and current.get("release_id") == release_id:
        bundle, manifest = _load_release(release_id, release_root)
        return _verify_runtime(bundle, manifest, include_smoke=False)
    return deploy_release(release_id, operation="rollback", release_root=release_root)


def status(release_root: Path = RELEASE_ROOT) -> dict[str, Any]:
    current, current_bundle, current_manifest = _load_current_release(release_root)
    anchor, _, _ = _load_anchor(release_root)
    try:
        source_head = _git_output("rev-parse", "HEAD")
    except ReleaseError:
        source_head = "unavailable"
    result: dict[str, Any] = {
        "source_checkout": str(SOURCE_ROOT),
        "source_checkout_head": source_head,
        "runtime": str(VENV_DIR),
        "release_root": str(release_root),
        "active_release": current,
        "rollback_anchor": anchor,
        "services": {
            WEB_SERVICE: _is_active(WEB_SERVICE),
            REFRESH_TIMER: _is_active(REFRESH_TIMER),
        },
    }
    if current:
        assert current_bundle is not None and current_manifest is not None
        installed: dict[str, Any]
        try:
            _verify_installed_assets(current_manifest)
            _run([str(VENV_DIR / "bin" / "python"), "-m", "pip", "check"])
            package_info = _verify_installed_application(current_bundle)
            installed = {"matches_release": True, **package_info}
        except ReleaseError as exc:
            installed = {"matches_release": False, "error": str(exc)}
        result["active_manifest"] = {
            "release_id": current_bundle.name,
            "git_commit": current_manifest["git_commit"],
            "historical_runtime_relation": current_manifest["historical_runtime_relation"],
            "package_filename": current_manifest["application"]["filename"],
            "package_sha256": current_manifest["application"]["sha256"],
            "installed_package": installed,
        }
    return result


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage immutable subctl production releases")
    commands = parser.add_subparsers(dest="command", required=True)

    verify_parser = commands.add_parser("verify", help="verify a stored release bundle")
    verify_parser.add_argument("--path", type=Path, required=True)

    build_parser = commands.add_parser("build", help="build and store from a clean pushed Git commit")
    build_parser.add_argument("--commit", required=True)
    build_parser.add_argument("--domain", required=True)

    bootstrap_parser = commands.add_parser("bootstrap", help="store the inferred first rollback anchor")
    bootstrap_parser.add_argument("--commit", required=True)
    bootstrap_parser.add_argument("--domain", required=True)

    deploy_parser = commands.add_parser("deploy", help="deploy a retained immutable release")
    deploy_parser.add_argument("--release-id", required=True)

    rollback_parser = commands.add_parser("rollback", help="restore a retained immutable release")
    rollback_parser.add_argument("--release-id", required=True)

    commands.add_parser("status", help="report active release and source checkout separately")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "verify":
            manifest = verify_bundle(args.path)
            _git_output("cat-file", "-e", f"{manifest['git_commit']}^{{commit}}")
            print(json.dumps({"release_id": manifest["release_id"], "git_commit": manifest["git_commit"]}))
        elif args.command == "status":
            print(json.dumps(status(), sort_keys=True, indent=2))
        elif args.command == "build":
            _require_root()
            with deployment_lock():
                bundle = build_bundle(args.commit, args.domain)
            print(f"stored release {bundle.name}")
        elif args.command == "bootstrap":
            with deployment_lock():
                bundle = bootstrap(args.commit, args.domain)
            print(f"stored inferred rollback anchor {bundle.name}")
        elif args.command == "deploy":
            with deployment_lock():
                info = deploy_release(args.release_id)
            print(f"release {args.release_id} deployed and verified; runtime {info['application_version']}")
        elif args.command == "rollback":
            _require_root()
            with deployment_lock():
                info = rollback_release(args.release_id)
            print(f"release {args.release_id} restored and verified; runtime {info['application_version']}")
        else:
            raise ReleaseError(f"unsupported command: {args.command}")
    except ReleaseError as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
