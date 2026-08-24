from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .atomic import atomic_write_text
from .config import _load_yaml_mapping, mask_secret, validate_token
from .errors import ValidationError


@dataclass(frozen=True)
class User:
    name: str
    token: str
    xui_subscription: str

    @property
    def masked_token(self) -> str:
        return mask_secret(self.token)


@dataclass(frozen=True)
class UserRegistry:
    users: dict[str, User]
    path: Path


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def load_users(path: Path, *, allow_missing: bool = False) -> UserRegistry:
    path = Path(path)
    if allow_missing and not path.exists():
        return UserRegistry(users={}, path=path)

    data = _load_yaml_mapping(path, label="users")
    return _parse_users(data, path)


def add_user(
    path: Path,
    *,
    name: str,
    xui_sub_url: str,
    token: str | None = None,
    force: bool = False,
) -> User:
    from .config import _require_http_url

    path = Path(path)
    _validate_user_name(name)
    xui_sub_url = _require_http_url(xui_sub_url, "xui_subscription", secret=True)
    registry = load_users(path, allow_missing=True)
    generated_or_given_token = validate_token(token, "token") if token else generate_token()

    users = dict(registry.users)
    existing = users.get(name)
    if existing and not force:
        raise ValidationError(f"user already exists: {name}")

    final_token = generated_or_given_token if token or existing is None else existing.token
    users[name] = User(name=name, token=final_token, xui_subscription=xui_sub_url)
    _validate_unique_tokens(users)
    write_users(path, users)
    return users[name]


def update_user(
    path: Path,
    *,
    current_name: str,
    new_name: str | None = None,
    xui_sub_url: str | None = None,
) -> User:
    """Update a user while preserving its token unless explicitly rotated."""
    from .config import _require_http_url

    path = Path(path)
    _validate_user_name(current_name)
    if new_name is not None:
        _validate_user_name(new_name)
    registry = load_users(path)
    existing = registry.users.get(current_name)
    if existing is None:
        raise ValidationError(f"unknown user: {current_name}")

    final_name = new_name or current_name
    if final_name != current_name and final_name in registry.users:
        raise ValidationError(f"user already exists: {final_name}")
    final_url = (
        _require_http_url(xui_sub_url, "xui_subscription", secret=True)
        if xui_sub_url is not None
        else existing.xui_subscription
    )
    users = dict(registry.users)
    del users[current_name]
    updated = User(name=final_name, token=existing.token, xui_subscription=final_url)
    users[final_name] = updated
    _validate_unique_tokens(users)
    write_users(path, users)
    return updated


def rotate_user_token(path: Path, *, name: str) -> User:
    """Replace a user's token while retaining its name and upstream URL."""
    path = Path(path)
    _validate_user_name(name)
    registry = load_users(path)
    existing = registry.users.get(name)
    if existing is None:
        raise ValidationError(f"unknown user: {name}")
    users = dict(registry.users)
    rotated = User(name=name, token=generate_token(), xui_subscription=existing.xui_subscription)
    users[name] = rotated
    _validate_unique_tokens(users)
    write_users(path, users)
    return rotated


def delete_user(path: Path, *, name: str) -> User:
    """Remove a user from the registry and return the removed record."""
    path = Path(path)
    _validate_user_name(name)
    registry = load_users(path)
    existing = registry.users.get(name)
    if existing is None:
        raise ValidationError(f"unknown user: {name}")
    users = dict(registry.users)
    del users[name]
    write_users(path, users)
    return existing


def write_users(path: Path, users: dict[str, User]) -> None:
    payload = {
        "users": {
            name: {
                "token": user.token,
                "xui_subscription": user.xui_subscription,
            }
            for name, user in sorted(users.items())
        }
    }
    content = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    )
    mode = 0o600 if not Path(path).exists() and os.name == "posix" else None
    atomic_write_text(Path(path), content, mode=mode)


def _parse_users(data: dict[str, Any], path: Path) -> UserRegistry:
    missing = []
    if "users" not in data:
        missing.append("users")
    if missing:
        raise ValidationError(f"users missing required field(s): {', '.join(missing)}")
    if not isinstance(data["users"], dict):
        raise ValidationError("users.users must be a mapping")

    parsed: dict[str, User] = {}
    field_errors: list[str] = []
    for name, values in data["users"].items():
        if not isinstance(name, str) or not name:
            field_errors.append("users.<name> must be a non-empty string key")
            continue
        if not isinstance(values, dict):
            field_errors.append(f"users.{name} must be a mapping")
            continue
        for field in ("token", "xui_subscription"):
            if field not in values or values[field] is None:
                field_errors.append(f"users.{name}.{field}")
    if field_errors:
        raise ValidationError(f"users missing or invalid required field(s): {', '.join(field_errors)}")

    from .config import _require_http_url

    for name, values in data["users"].items():
        _validate_user_name(name)
        token = validate_token(values["token"], f"users.{name}.token")
        xui_subscription = _require_http_url(
            values["xui_subscription"],
            f"users.{name}.xui_subscription",
            secret=True,
        )
        parsed[name] = User(name=name, token=token, xui_subscription=xui_subscription)

    _validate_unique_tokens(parsed)
    return UserRegistry(users=parsed, path=path)


def _validate_unique_tokens(users: dict[str, User]) -> None:
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for name, user in users.items():
        owner = seen.get(user.token)
        if owner:
            duplicates.append(f"{owner}, {name}")
        seen[user.token] = name
    if duplicates:
        raise ValidationError(f"user tokens must be unique: {'; '.join(duplicates)}")


def _validate_user_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValidationError("user name must be a non-empty string")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    if any(ch not in allowed for ch in name):
        raise ValidationError("user name may contain only letters, digits, '.', '_' and '-'")
