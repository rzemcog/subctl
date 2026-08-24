from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .errors import ValidationError

DEFAULT_CONFIG_PATH = Path("/etc/subctl/config.yaml")
DEFAULT_USERS_PATH = Path("/etc/subctl/users.yaml")
DEFAULT_STATE_DIR = Path("/var/lib/subctl")
DEFAULT_OUTPUT_DIR = DEFAULT_STATE_DIR / "public"


@dataclass(frozen=True)
class ProviderConfig:
    upstream_url: str
    shared_token: str
    refresh_interval_seconds: int


@dataclass(frozen=True)
class PublicConfig:
    base_url: str
    output_dir: Path


@dataclass(frozen=True)
class RenderConfig:
    profile_update_interval_seconds: int
    provider_update_interval_seconds: int
    healthcheck_url: str
    healthcheck_interval_seconds: int
    healthcheck_timeout_milliseconds: int
    healthcheck_max_failed_times: int
    healthcheck_tolerance_milliseconds: int
    healthcheck_lazy: bool
    provider_exclude_keywords: tuple[str, ...]
    include_private: bool
    include_provider: bool
    provider_first: bool
    private_prefix: str
    provider_prefix: str


@dataclass(frozen=True)
class GatewayConfig:
    private_upstream_url: str
    controller_secret: str
    output_path: Path
    output_owner: str
    output_group: str
    socks_port: int
    controller_port: int
    base_default: str
    physical_interface: str
    external_ui_path: Path
    tun_output_path: Path
    dns_nameservers: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    provider: ProviderConfig
    public: PublicConfig
    render: RenderConfig
    gateway: GatewayConfig | None
    state_dir: Path
    config_path: Path


def load_config(
    path: Path,
    *,
    state_dir: Path | None = None,
    output_dir: Path | None = None,
    settings_path: Path | None = None,
    settings_override: dict[str, Any] | None = None,
) -> AppConfig:
    path = Path(path)
    data = _load_yaml_mapping(path, label="config")
    resolved_state_dir = Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR
    if settings_path is None and state_dir is not None:
        candidate = resolved_state_dir / "ui" / "settings.yaml"
        settings_path = candidate if candidate.exists() else None
    if settings_path is not None and Path(settings_path).exists():
        overlay = _load_yaml_mapping(Path(settings_path), label="UI settings")
        data = _merge_mapping(data, validate_settings_overlay(overlay))
    if settings_override is not None:
        data = _merge_mapping(data, validate_settings_overlay(settings_override))
    required_paths = [
        "provider.upstream_url",
        "provider.shared_token",
        "provider.refresh_interval_seconds",
        "public.base_url",
        "render.profile_update_interval_seconds",
        "render.provider_update_interval_seconds",
        "render.healthcheck_url",
        "render.healthcheck_interval_seconds",
        "render.healthcheck_timeout_milliseconds",
        "render.healthcheck_max_failed_times",
        "render.healthcheck_tolerance_milliseconds",
        "render.healthcheck_lazy",
    ]
    if output_dir is None:
        required_paths.append("public.output_dir")
    missing = _missing_paths(data, required_paths)
    if missing:
        raise ValidationError(f"config missing required field(s): {', '.join(missing)}")

    provider_data = data["provider"]
    public_data = data["public"]
    render_data = data["render"]
    gateway_data = data.get("gateway")

    upstream_url = _require_http_url(
        provider_data["upstream_url"],
        "provider.upstream_url",
        secret=True,
    )
    shared_token = validate_token(provider_data["shared_token"], "provider.shared_token")
    base_url = _require_http_url(public_data["base_url"], "public.base_url")
    healthcheck_url = _require_http_url(render_data["healthcheck_url"], "render.healthcheck_url")

    configured_output = public_data.get("output_dir")
    if output_dir is None and not isinstance(configured_output, str):
        raise ValidationError("public.output_dir must be a path string")
    resolved_output_dir = (
        Path(output_dir)
        if output_dir is not None
        else Path(configured_output) if configured_output else DEFAULT_OUTPUT_DIR
    )

    gateway = _load_gateway_config(gateway_data) if gateway_data is not None else None

    return AppConfig(
        provider=ProviderConfig(
            upstream_url=upstream_url,
            shared_token=shared_token,
            refresh_interval_seconds=_positive_int(
                provider_data["refresh_interval_seconds"],
                "provider.refresh_interval_seconds",
            ),
        ),
        public=PublicConfig(base_url=base_url.rstrip("/"), output_dir=resolved_output_dir),
        render=RenderConfig(
            profile_update_interval_seconds=_positive_int(
                render_data["profile_update_interval_seconds"],
                "render.profile_update_interval_seconds",
            ),
            provider_update_interval_seconds=_positive_int(
                render_data["provider_update_interval_seconds"],
                "render.provider_update_interval_seconds",
            ),
            healthcheck_url=healthcheck_url,
            healthcheck_interval_seconds=_positive_int(
                render_data["healthcheck_interval_seconds"],
                "render.healthcheck_interval_seconds",
            ),
            healthcheck_timeout_milliseconds=_positive_int(
                render_data["healthcheck_timeout_milliseconds"],
                "render.healthcheck_timeout_milliseconds",
            ),
            healthcheck_max_failed_times=_positive_int(
                render_data["healthcheck_max_failed_times"],
                "render.healthcheck_max_failed_times",
            ),
            healthcheck_tolerance_milliseconds=_non_negative_int(
                render_data["healthcheck_tolerance_milliseconds"],
                "render.healthcheck_tolerance_milliseconds",
            ),
            healthcheck_lazy=_bool(
                render_data["healthcheck_lazy"],
                "render.healthcheck_lazy",
            ),
            provider_exclude_keywords=_string_list(
                render_data.get("provider_exclude_keywords", []),
                "render.provider_exclude_keywords",
            ),
            include_private=_bool(
                _composition_value(render_data, "include_private", True),
                "render.composition.include_private",
            ),
            include_provider=_bool(
                _composition_value(render_data, "include_provider", True),
                "render.composition.include_provider",
            ),
            provider_first=_bool(
                _composition_value(render_data, "provider_first", False),
                "render.composition.provider_first",
            ),
            private_prefix=_prefix(
                _composition_value(render_data, "private_prefix", "PRIVATE | "),
                "render.composition.private_prefix",
            ),
            provider_prefix=_prefix(
                _composition_value(render_data, "provider_prefix", "PROVIDER | "),
                "render.composition.provider_prefix",
            ),
        ),
        gateway=gateway,
        state_dir=resolved_state_dir,
        config_path=path,
    )


def require_gateway_config(config: AppConfig) -> GatewayConfig:
    if config.gateway is None:
        raise ValidationError(
            "config missing required gateway section for render-gateway"
        )
    return config.gateway


def _load_gateway_config(value: Any) -> GatewayConfig:
    if not isinstance(value, dict):
        raise ValidationError("gateway must be a mapping")
    required = [
        "private_upstream_url",
        "controller_secret",
        "output_path",
        "output_owner",
        "output_group",
        "socks_port",
        "controller_port",
        "base_default",
        "physical_interface",
    ]
    missing = [f"gateway.{field}" for field in required if value.get(field) is None]
    if missing:
        raise ValidationError(f"config missing required field(s): {', '.join(missing)}")

    output_path = value["output_path"]
    if not isinstance(output_path, str) or not output_path.strip():
        raise ValidationError("gateway.output_path must be a path string")
    output_owner = _non_empty_string(value["output_owner"], "gateway.output_owner")
    output_group = _non_empty_string(value["output_group"], "gateway.output_group")
    secret = value["controller_secret"]
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValidationError("gateway.controller_secret must be at least 32 characters")
    socks_port = _port(value["socks_port"], "gateway.socks_port")
    controller_port = _port(value["controller_port"], "gateway.controller_port")
    if socks_port == controller_port:
        raise ValidationError("gateway SOCKS and controller ports must be different")
    base_default = value["base_default"]
    if base_default not in {"DIRECT", "PROXY"}:
        raise ValidationError("gateway.base_default must be DIRECT or PROXY")
    physical_interface = _non_empty_string(
        value["physical_interface"], "gateway.physical_interface"
    )
    if len(physical_interface.encode("utf-8")) > 15 or not re.fullmatch(
        r"[A-Za-z0-9_.:-]+", physical_interface
    ):
        raise ValidationError("gateway.physical_interface must be a valid interface name")
    external_ui_path = value.get(
        "external_ui_path", "/var/lib/mihomo/ui/current"
    )
    if not isinstance(external_ui_path, str) or not external_ui_path.strip():
        raise ValidationError("gateway.external_ui_path must be a path string")
    external_ui_path = Path(external_ui_path)
    if not external_ui_path.is_absolute():
        raise ValidationError("gateway.external_ui_path must be an absolute path")
    tun_output_path = value.get("tun_output_path", "/etc/mihomo/config-tun.yaml")
    if not isinstance(tun_output_path, str) or not tun_output_path.strip():
        raise ValidationError("gateway.tun_output_path must be a path string")
    tun_output_path = Path(tun_output_path)
    if not tun_output_path.is_absolute():
        raise ValidationError("gateway.tun_output_path must be an absolute path")
    dns_nameservers = _string_list(
        value.get("dns_nameservers", ["1.1.1.1", "8.8.8.8"]),
        "gateway.dns_nameservers",
    )

    return GatewayConfig(
        private_upstream_url=_require_http_url(
            value["private_upstream_url"],
            "gateway.private_upstream_url",
            secret=True,
        ),
        controller_secret=secret,
        output_path=Path(output_path),
        output_owner=output_owner,
        output_group=output_group,
        socks_port=socks_port,
        controller_port=controller_port,
        base_default=base_default,
        physical_interface=physical_interface,
        external_ui_path=external_ui_path,
        tun_output_path=tun_output_path,
        dns_nameservers=dns_nameservers,
    )


def _load_yaml_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} file not found: {path}") from exc
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or exc.__class__.__name__
        raise ValidationError(f"{label} YAML is invalid: {problem}") from exc
    except OSError as exc:
        raise ValidationError(f"cannot read {label} file {path}: {exc.strerror}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValidationError(f"{label} YAML must be a mapping at top level")
    return data


_SETTINGS_PROVIDER_FIELDS = {"upstream_url", "refresh_interval_seconds"}
_SETTINGS_RENDER_FIELDS = {
    "profile_update_interval_seconds",
    "provider_update_interval_seconds",
    "healthcheck_url",
    "healthcheck_interval_seconds",
    "healthcheck_timeout_milliseconds",
    "healthcheck_max_failed_times",
    "healthcheck_tolerance_milliseconds",
    "healthcheck_lazy",
    "provider_exclude_keywords",
}
_SETTINGS_COMPOSITION_FIELDS = {
    "include_private",
    "include_provider",
    "provider_first",
    "private_prefix",
    "provider_prefix",
}


def validate_settings_overlay(value: Any) -> dict[str, Any]:
    """Validate and normalize the non-secret settings allowed from the UI."""
    if not isinstance(value, dict):
        raise ValidationError("UI settings must be a mapping")
    allowed_top = {"version", "published_at", "rolled_back_from", "provider", "render"}
    unknown_top = sorted(set(value) - allowed_top)
    if unknown_top:
        raise ValidationError(f"UI settings contain unsupported field(s): {', '.join(unknown_top)}")

    result: dict[str, Any] = {}
    provider = value.get("provider")
    if provider is not None:
        if not isinstance(provider, dict):
            raise ValidationError("UI settings provider must be a mapping")
        unknown = sorted(set(provider) - _SETTINGS_PROVIDER_FIELDS)
        if unknown:
            raise ValidationError(f"UI settings provider contains unsupported field(s): {', '.join(unknown)}")
        result["provider"] = deepcopy(provider)

    render = value.get("render")
    if render is not None:
        if not isinstance(render, dict):
            raise ValidationError("UI settings render must be a mapping")
        unknown = sorted(set(render) - _SETTINGS_RENDER_FIELDS - {"composition"})
        if unknown:
            raise ValidationError(f"UI settings render contains unsupported field(s): {', '.join(unknown)}")
        result_render = {key: deepcopy(render[key]) for key in render if key in _SETTINGS_RENDER_FIELDS}
        composition = render.get("composition")
        if composition is not None:
            if not isinstance(composition, dict):
                raise ValidationError("UI settings render.composition must be a mapping")
            unknown = sorted(set(composition) - _SETTINGS_COMPOSITION_FIELDS)
            if unknown:
                raise ValidationError(
                    "UI settings render.composition contains unsupported field(s): "
                    + ", ".join(unknown)
                )
            result_render["composition"] = deepcopy(composition)
        result["render"] = result_render
    return result


def _merge_mapping(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_mapping(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _composition_value(render_data: dict[str, Any], key: str, default: Any) -> Any:
    composition = render_data.get("composition")
    if isinstance(composition, dict) and key in composition:
        return composition[key]
    return render_data.get(key, default)


def _prefix(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValidationError(f"{field} must be a string up to 80 characters")
    return value


def validate_token(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a URL-safe token string")
    if len(value) < 32:
        raise ValidationError(f"{field} must be at least 32 characters")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if any(ch not in allowed for ch in value):
        raise ValidationError(f"{field} must contain only URL-safe token characters")
    return value


def mask_secret(value: str, *, visible: int = 4) -> str:
    if len(value) <= visible * 2:
        return "***"
    return f"{value[:visible]}...{value[-visible:]}"


def mask_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return "***"
    return f"{parsed.scheme}://{parsed.netloc}/..."


def _require_http_url(value: Any, field: str, *, secret: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an http or https URL")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        shown = mask_url(value) if secret else value
        raise ValidationError(f"{field} must be an http or https URL: {shown}")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{field} must be a non-negative integer")
    return value


def _port(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValidationError(f"{field} must be an integer between 1 and 65535")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be a boolean")
    return value


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{field} must be a list of non-empty strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{field} must be a list of non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise ValidationError(f"{field} must not contain duplicates")
    return normalized


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _missing_paths(data: dict[str, Any], paths: list[str]) -> list[str]:
    missing: list[str] = []
    for dotted in paths:
        current: Any = data
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current or current[part] is None:
                missing.append(dotted)
                break
            current = current[part]
    return missing
