from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from .atomic import atomic_write_text
from .config import AppConfig, load_config, mask_url, validate_settings_overlay
from .errors import ValidationError
from .refresh import RefreshResult, load_provider_status, refresh_provider
from .registry import (
    User,
    UserRegistry,
    add_user,
    delete_user,
    load_users,
    rotate_user_token,
    update_user,
)
from .render import (
    RenderOptions,
    RenderSummary,
    _load_provider_lines,
    build_mihomo_profile,
    render_subscriptions,
    yaml_output_path,
)


class SubscriptionService:
    """Shared application layer used by the CLI, timer and web UI."""

    def __init__(
        self,
        *,
        config_path: Path,
        users_path: Path,
        state_dir: Path | None = None,
        output_dir: Path | None = None,
        lock_path: Path | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.users_path = Path(users_path)
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.lock_path = Path(lock_path) if lock_path is not None else self._default_lock_path()

    def _default_lock_path(self) -> Path:
        if self.state_dir is not None:
            return self.state_dir / "ui" / "refresh.lock"
        return Path("/var/lib/subctl/ui/refresh.lock")

    @property
    def settings_path(self) -> Path:
        state_dir = self.state_dir or Path("/var/lib/subctl")
        return state_dir / "ui" / "settings.yaml"

    @property
    def settings_versions_dir(self) -> Path:
        return self.settings_path.parent / "settings-versions"

    @property
    def settings_draft_path(self) -> Path:
        return self.settings_path.parent / "settings-draft.yaml"

    def _config(self, *, settings_override: dict[str, Any] | None = None) -> AppConfig:
        return load_config(
            self.config_path,
            state_dir=self.state_dir,
            output_dir=self.output_dir,
            settings_path=self.settings_path,
            settings_override=settings_override,
        )

    def _registry(self, *, allow_missing: bool = False) -> UserRegistry:
        return load_users(self.users_path, allow_missing=allow_missing)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def list_users(self) -> list[User]:
        registry = self._registry(allow_missing=True)
        return [registry.users[name] for name in sorted(registry.users)]

    def get_user(self, name: str) -> User:
        user = self._registry(allow_missing=True).users.get(name)
        if user is None:
            raise ValidationError(f"unknown user: {name}")
        return user

    def get_user_by_token(self, token: str) -> User | None:
        registry = self._registry(allow_missing=True)
        return next((user for user in registry.users.values() if user.token == token), None)

    def create_user(self, *, name: str, xui_subscription: str) -> User:
        with self.locked():
            return add_user(self.users_path, name=name, xui_sub_url=xui_subscription)

    def update_user(
        self,
        *,
        current_name: str,
        new_name: str | None = None,
        xui_subscription: str | None = None,
    ) -> User:
        with self.locked():
            return update_user(
                self.users_path,
                current_name=current_name,
                new_name=new_name,
                xui_sub_url=xui_subscription,
            )

    def rotate_user(self, *, name: str) -> User:
        with self.locked():
            existing = self.get_user(name)
            updated = rotate_user_token(self.users_path, name=name)
            self._remove_public_files(existing)
            return updated

    def delete_user(self, *, name: str) -> User:
        with self.locked():
            existing = delete_user(self.users_path, name=name)
            self._remove_public_files(existing)
            return existing

    def refresh_provider(self) -> RefreshResult:
        with self.locked():
            return refresh_provider(self._config())

    def render_user(self, *, name: str) -> RenderSummary:
        with self.locked():
            return render_subscriptions(
                self._config(),
                self._registry(),
                options=RenderOptions(mode="all", user_name=name),
            )

    def render_all(self) -> RenderSummary:
        with self.locked():
            return render_subscriptions(
                self._config(),
                self._registry(),
                options=RenderOptions(mode="all"),
            )

    def user_view(self, user: User, *, reveal_upstream: bool = False, store: Any = None) -> dict[str, object]:
        config = self._config()
        result: dict[str, object] = {
            "name": user.name,
            "token_masked": user.masked_token,
            "yaml_url": f"{config.public.base_url}/s/{user.token}.yaml",
            "raw_url": f"{config.public.base_url}/s/{user.token}.raw",
            "status": self.artifact_status(user),
        }
        if store is not None and hasattr(store, "user_activity"):
            activity = store.user_activity(user.name)
            if activity.get("render", {}).get("status") == "not_generated":
                persisted = self._persisted_render_status(user.name)
                if persisted is not None:
                    activity["render"] = persisted
            result["activity"] = activity
        if reveal_upstream:
            result["xui_subscription"] = user.xui_subscription
        return result

    def artifact_status(self, user: User) -> dict[str, Any]:
        config = self._config()
        artifacts: dict[str, Any] = {}
        for format_name, path in (
            ("yaml", yaml_output_path(config, user)),
            ("raw", config.public.output_dir / "s" / f"{user.token}.raw"),
        ):
            if path.exists():
                stat = path.stat()
                artifacts[format_name] = {
                    "present": True,
                    "path": str(path),
                    "updated_at": _file_time(path),
                    "size": stat.st_size,
                    "sha256": _sha256(path),
                }
            else:
                artifacts[format_name] = {
                    "present": False,
                    "path": str(path),
                    "updated_at": None,
                    "size": 0,
                    "sha256": None,
                }
        timestamps = [item["updated_at"] for item in artifacts.values() if item["updated_at"]]
        return {
            "ready": all(item["present"] for item in artifacts.values()),
            "last_regenerated_at": max(timestamps) if timestamps else None,
            "artifacts": artifacts,
        }

    def _persisted_render_status(self, user_name: str) -> dict[str, Any] | None:
        path = (self.state_dir or Path("/var/lib/subctl")) / "ui" / "render-status.json"
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, yaml.YAMLError):
            return None
        users = value.get("users") if isinstance(value, dict) else None
        status = users.get(user_name) if isinstance(users, dict) else None
        if not isinstance(status, dict):
            return None
        return {
            "status": status.get("status", "not_generated"),
            "job_id": None,
            "started_at": None,
            "finished_at": status.get("finished_at"),
            "error": status.get("error"),
            "config_version": status.get("config_version"),
        }

    def provider_status(self) -> dict[str, object]:
        config = self._config()
        decoded = config.state_dir / "cache" / "provider.decoded"
        public = config.public.output_dir / "feeds" / "provider" / config.provider.shared_token
        status: dict[str, object] = {
            "cache_present": decoded.exists(),
            "public_feed_present": public.exists(),
            "node_count": 0,
            "cache_updated_at": _file_time(decoded),
            "provider_settings": {
                "upstream_url": config.provider.upstream_url,
                "refresh_interval_seconds": config.provider.refresh_interval_seconds,
                "exclude_keywords": list(config.render.provider_exclude_keywords),
            },
            "shared_provider_count": 1,
            "shared_provider_note": "Сейчас используется один общий provider; источник можно менять в настройках.",
        }
        if decoded.exists():
            from .provider import validate_provider_subscription

            feed = validate_provider_subscription(decoded.read_text(encoding="utf-8"))
            status["node_count"] = len(feed.uris)
        status["refresh"] = load_provider_status(config)
        return status

    def settings(self) -> dict[str, Any]:
        config = self._config()
        stored = self._read_settings_document()
        return {
            "version": _int_or_zero(stored.get("version")),
            "published_at": stored.get("published_at"),
            "settings": _settings_payload(config),
            "draft": self._read_draft(),
            "versions": self.settings_versions(),
        }

    def settings_versions(self) -> list[dict[str, Any]]:
        versions: list[dict[str, Any]] = []
        if not self.settings_versions_dir.exists():
            return versions
        for path in sorted(self.settings_versions_dir.glob("*.yaml"), reverse=True):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                versions.append(
                    {
                        "version": _int_or_zero(document.get("version")),
                        "published_at": document.get("published_at"),
                        "rolled_back_from": document.get("rolled_back_from"),
                    }
                )
            except (OSError, yaml.YAMLError):
                continue
        return versions[:30]

    def save_settings_draft(self, value: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_settings_overlay(value)
        candidate = self._config(settings_override=normalized)
        draft = _settings_payload(candidate)
        atomic_write_text(
            self.settings_draft_path,
            yaml.safe_dump(draft, sort_keys=False, allow_unicode=True),
            mode=0o600,
        )
        return draft

    def preview_settings(self, value: dict[str, Any] | None = None, *, user_name: str | None = None) -> dict[str, Any]:
        normalized = validate_settings_overlay(value or {})
        config = self._config(settings_override=normalized)
        users = self.list_users()
        selected_user = self.get_user(user_name) if user_name else (users[0] if users else None)
        profile: dict[str, Any] | None = None
        yaml_text = None
        raw: dict[str, Any] = {
            "include_private": config.render.include_private,
            "include_provider": config.render.include_provider,
            "order": ["provider", "private"] if config.render.provider_first else ["private", "provider"],
            "private_prefix": config.render.private_prefix,
            "provider_prefix": config.render.provider_prefix,
            "provider_nodes": 0,
            "personal_nodes": "получается при генерации; URL скрыт",
        }
        try:
            raw["provider_nodes"] = len(_load_provider_lines(config))
        except ValidationError:
            raw["provider_nodes"] = None
        if selected_user is not None:
            profile = _mask_preview(build_mihomo_profile(config, selected_user))
            yaml_text = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
        return {
            "user": selected_user.name if selected_user else None,
            "settings": _settings_payload(config),
            "yaml": yaml_text,
            "profile": profile,
            "raw": raw,
            "secrets_hidden": True,
        }

    def publish_settings(self, value: dict[str, Any], *, rolled_back_from: int | None = None) -> dict[str, Any]:
        with self.locked():
            return self._publish_settings(value, rolled_back_from=rolled_back_from)

    def _publish_settings(self, value: dict[str, Any], *, rolled_back_from: int | None = None) -> dict[str, Any]:
        normalized = validate_settings_overlay(value)
        candidate = self._config(settings_override=normalized)
        current = self._read_settings_document()
        version = _int_or_zero(current.get("version")) + 1
        document: dict[str, Any] = {
            "version": version,
            "published_at": _now(),
            **_settings_payload(candidate),
        }
        if rolled_back_from is not None:
            document["rolled_back_from"] = rolled_back_from
        content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        atomic_write_text(self.settings_path, content, mode=0o600)
        self.settings_versions_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.settings_versions_dir / f"{version:06d}.yaml", content, mode=0o600)
        self.settings_draft_path.unlink(missing_ok=True)
        return document

    def rollback_settings(self, version: int) -> dict[str, Any]:
        path = self.settings_versions_dir / f"{version:06d}.yaml"
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise ValidationError(f"settings version not found: {version}") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise ValidationError("settings version cannot be read") from exc
        return self.publish_settings(value, rolled_back_from=version)

    def _read_settings_document(self) -> dict[str, Any]:
        try:
            value = yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, OSError, yaml.YAMLError):
            return {}
        return value if isinstance(value, dict) else {}

    def _read_draft(self) -> dict[str, Any] | None:
        try:
            value = yaml.safe_load(self.settings_draft_path.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, OSError, yaml.YAMLError):
            return None
        return value if isinstance(value, dict) else None

    def _remove_public_files(self, user: User) -> None:
        config = self._config()
        for suffix in (".yaml", ".raw"):
            (config.public.output_dir / "s" / f"{user.token}{suffix}").unlink(missing_ok=True)


def _settings_payload(config: AppConfig) -> dict[str, Any]:
    return {
        "provider": {
            "upstream_url": config.provider.upstream_url,
            "refresh_interval_seconds": config.provider.refresh_interval_seconds,
        },
        "render": {
            "profile_update_interval_seconds": config.render.profile_update_interval_seconds,
            "provider_update_interval_seconds": config.render.provider_update_interval_seconds,
            "healthcheck_url": config.render.healthcheck_url,
            "healthcheck_interval_seconds": config.render.healthcheck_interval_seconds,
            "healthcheck_timeout_milliseconds": config.render.healthcheck_timeout_milliseconds,
            "healthcheck_max_failed_times": config.render.healthcheck_max_failed_times,
            "healthcheck_tolerance_milliseconds": config.render.healthcheck_tolerance_milliseconds,
            "healthcheck_lazy": config.render.healthcheck_lazy,
            "provider_exclude_keywords": list(config.render.provider_exclude_keywords),
            "composition": {
                "include_private": config.render.include_private,
                "include_provider": config.render.include_provider,
                "provider_first": config.render.provider_first,
                "private_prefix": config.render.private_prefix,
                "provider_prefix": config.render.provider_prefix,
            },
        },
    }


def _mask_preview(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: _mask_preview(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [_mask_preview(item, key=key) for item in value]
    if isinstance(value, str) and (key in {"url", "private_url", "provider_url"} or "secret" in key or "token" in key):
        return mask_url(value) if key.endswith("url") or key == "url" else "***"
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_time(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summary_message(summary: RenderSummary) -> str:
    return f"rendered={summary.rendered} skipped={summary.skipped} failed={summary.failed}"
