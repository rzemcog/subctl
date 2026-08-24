from __future__ import annotations

import base64
import grp
import ipaddress
import json
import pwd
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yaml

from .atomic import atomic_write_text
from .config import AppConfig, require_gateway_config
from .errors import RenderError, UpstreamError, ValidationError
from .provider import validate_provider_subscription
from .public_files import PUBLIC_FILE_MODE, ensure_public_directory
from .registry import User, UserRegistry

DEFAULT_PERSONAL_FETCH_TIMEOUT_SECONDS = 20
PRIVATE_PREFIX = "PRIVATE | "
PROVIDER_PREFIX = "PROVIDER | "
TUN_ROUTE_EXCLUDE_ADDRESSES = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

RenderMode = str
FetchSubscription = Callable[[str, float], bytes]


@dataclass(frozen=True)
class RenderOptions:
    mode: RenderMode = "all"
    user_name: str | None = None
    timeout_seconds: float = DEFAULT_PERSONAL_FETCH_TIMEOUT_SECONDS


@dataclass
class UserRenderResult:
    user_name: str
    rendered: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None


@dataclass
class RenderSummary:
    rendered: int = 0
    skipped: int = 0
    failed: int = 0
    users: list[UserRenderResult] = field(default_factory=list)


@dataclass(frozen=True)
class RenderedUser:
    name: str
    yaml_path: Path


def render_users(config: AppConfig, registry: UserRegistry) -> list[RenderedUser]:
    rendered: list[RenderedUser] = []
    for name in sorted(registry.users):
        user = registry.users[name]
        _render_yaml(config, user)
        rendered.append(RenderedUser(name=name, yaml_path=yaml_output_path(config, user)))
    return rendered


def yaml_output_path(config: AppConfig, user: User) -> Path:
    return config.public.output_dir / "s" / f"{user.token}.yaml"


def render_user_yaml(config: AppConfig, user: User) -> str:
    return yaml.safe_dump(build_mihomo_profile(config, user), sort_keys=False, allow_unicode=True)


def build_mihomo_profile(config: AppConfig, user: User) -> dict:
    provider_url = f"{config.public.base_url}/feeds/provider/{config.provider.shared_token}"
    return _build_profile(
        config,
        private_url=user.xui_subscription,
        provider_url=provider_url,
        base_default="DIRECT",
    )


def build_gateway_profile(
    config: AppConfig,
    *,
    enable_tun: bool = False,
    tun_route_exclude_addresses: tuple[str, ...] = (),
) -> dict:
    gateway = require_gateway_config(config)
    bootstrap_hosts = _gateway_bootstrap_hosts(
        gateway.private_upstream_url,
        config.provider.upstream_url,
        *(config.public.base_url,) if enable_tun else (),
    )
    profile = _build_profile(
        config,
        private_url=gateway.private_upstream_url,
        provider_url=config.provider.upstream_url,
        base_default=gateway.base_default,
    )
    profile.update(
        {
            "interface-name": gateway.physical_interface,
            "log-level": "warning",
            "listeners": [
                {
                    "name": "local-socks",
                    "type": "socks",
                    "listen": "127.0.0.1",
                    "port": gateway.socks_port,
                }
            ],
            "external-controller": f"127.0.0.1:{gateway.controller_port}",
            "external-ui": str(gateway.external_ui_path),
            "secret": gateway.controller_secret,
            "tun": (
                _gateway_tun_config(tun_route_exclude_addresses)
                if enable_tun
                else {"enable": False}
            ),
            "dns": {
                "enable": True,
                "ipv6": False,
                "enhanced-mode": "redir-host",
                "default-nameserver": list(gateway.dns_nameservers),
                "nameserver": list(gateway.dns_nameservers),
                "proxy-server-nameserver": list(gateway.dns_nameservers),
                "direct-nameserver": list(gateway.dns_nameservers),
                "direct-nameserver-follow-policy": False,
            },
        }
    )
    profile["rules"] = [
        *(f"DOMAIN,{host},DIRECT" for host in bootstrap_hosts),
        *profile["rules"],
    ]
    return profile


def _normalize_tun_route_excludes(extra: tuple[str, ...]) -> tuple[str, ...]:
    addresses = list(TUN_ROUTE_EXCLUDE_ADDRESSES)
    seen = set(addresses)
    for raw in extra:
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError("TUN route exclude must be a non-empty IP or CIDR")
        try:
            network = ipaddress.ip_network(raw.strip(), strict=False)
        except ValueError as exc:
            raise ValidationError(
                f"invalid TUN route exclude address: {raw}"
            ) from exc
        normalized = str(network)
        if normalized not in seen:
            addresses.append(normalized)
            seen.add(normalized)
    return tuple(addresses)


def _gateway_tun_config(extra: tuple[str, ...] = ()) -> dict:
    return {
        "enable": True,
        "stack": "mixed",
        "device": "mihomo0",
        "auto-route": True,
        "auto-redirect": True,
        "auto-detect-interface": False,
        "strict-route": True,
        "dns-hijack": ["any:53", "tcp://any:53"],
        "route-exclude-address": list(_normalize_tun_route_excludes(extra)),
    }


def _gateway_bootstrap_hosts(*urls: str) -> tuple[str, ...]:
    hosts: list[str] = []
    for url in urls:
        host = urlsplit(url).hostname
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def render_gateway(
    config: AppConfig,
    *,
    output: Path | None = None,
    enable_tun: bool = False,
    tun_route_exclude_addresses: tuple[str, ...] = (),
) -> Path:
    gateway = require_gateway_config(config)
    target = Path(output) if output is not None else (
        gateway.tun_output_path if enable_tun else gateway.output_path
    )
    if _is_within(target, config.public.output_dir):
        raise ValidationError("gateway output must be outside public.output_dir")
    uid, gid = _resolve_gateway_ownership(
        gateway.output_owner, gateway.output_group
    )
    content = yaml.safe_dump(
        build_gateway_profile(
            config,
            enable_tun=enable_tun,
            tun_route_exclude_addresses=tun_route_exclude_addresses,
        ),
        sort_keys=False,
        allow_unicode=True,
    )
    try:
        atomic_write_text(target, content, mode=0o600, owner_uid=uid, owner_gid=gid)
    except OSError as exc:
        raise RenderError(f"failed to write gateway profile: {target}") from exc
    return target


def _resolve_gateway_ownership(owner: str, group: str) -> tuple[int, int]:
    try:
        uid = pwd.getpwnam(owner).pw_uid
    except KeyError as exc:
        raise ValidationError(
            f"gateway.output_owner account does not exist: {owner}"
        ) from exc
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise ValidationError(
            f"gateway.output_group does not exist: {group}"
        ) from exc
    return uid, gid


def _build_profile(
    config: AppConfig,
    *,
    private_url: str,
    provider_url: str,
    base_default: str,
) -> dict:
    health_check = {
        "enable": True,
        "url": config.render.healthcheck_url,
        "interval": config.render.healthcheck_interval_seconds,
        "timeout": config.render.healthcheck_timeout_milliseconds,
        "lazy": config.render.healthcheck_lazy,
    }
    exclude_filter = _provider_exclude_filter(config.render.provider_exclude_keywords)
    providers: dict[str, dict] = {}
    groups: list[dict] = []
    available_groups: list[str] = []

    if config.render.include_private:
        providers["private"] = {
            "type": "http",
            "url": private_url,
            "interval": config.render.profile_update_interval_seconds,
            "path": "./providers/private.yaml",
            "health-check": dict(health_check),
        }
        groups.append({"name": "PRIVATE", "type": "select", "use": ["private"]})
        available_groups.append("PRIVATE")

    if config.render.include_provider:
        providers["provider"] = {
            "type": "http",
            "url": provider_url,
            "interval": config.render.provider_update_interval_seconds,
            "path": "./providers/provider.yaml",
            **({"exclude-filter": exclude_filter} if exclude_filter else {}),
            "health-check": dict(health_check),
        }
        groups.append(
            {
                "name": "PROVIDER-AUTO",
                "type": "url-test",
                "use": ["provider"],
                "url": config.render.healthcheck_url,
                "interval": config.render.healthcheck_interval_seconds,
                "timeout": config.render.healthcheck_timeout_milliseconds,
                "max-failed-times": config.render.healthcheck_max_failed_times,
                "tolerance": config.render.healthcheck_tolerance_milliseconds,
                "lazy": config.render.healthcheck_lazy,
            }
        )
        available_groups.append("PROVIDER-AUTO")

    if config.render.provider_first:
        available_groups.sort(key=lambda value: 0 if value == "PROVIDER-AUTO" else 1)

    if available_groups:
        groups.append(
            {
                "name": "AUTO",
                "type": "fallback" if len(available_groups) > 1 else "select",
                "proxies": available_groups,
                **(
                    {
                        "url": config.render.healthcheck_url,
                        "interval": config.render.healthcheck_interval_seconds,
                        "timeout": config.render.healthcheck_timeout_milliseconds,
                        "max-failed-times": config.render.healthcheck_max_failed_times,
                        "lazy": config.render.healthcheck_lazy,
                    }
                    if len(available_groups) > 1
                    else {}
                ),
            }
        )
        proxy_choices = ["AUTO", *available_groups, "DIRECT"]
    else:
        proxy_choices = ["DIRECT"]

    groups.extend(
        [
            {"name": "PROXY", "type": "select", "proxies": proxy_choices},
            {
                "name": "BASE",
                "type": "select",
                "proxies": [base_default, "PROXY" if base_default == "DIRECT" else "DIRECT"],
            },
        ]
    )
    return {
        "proxy-providers": providers,
        "proxy-groups": groups,
        "rules": [
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
            "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
            "IP-CIDR,::1/128,DIRECT,no-resolve",
            "IP-CIDR,fc00::/7,DIRECT,no-resolve",
            "IP-CIDR,fe80::/10,DIRECT,no-resolve",
            "MATCH,BASE",
        ],
    }


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def render_subscriptions(
    config: AppConfig,
    registry: UserRegistry,
    *,
    options: RenderOptions | None = None,
    fetch_subscription: FetchSubscription | None = None,
) -> RenderSummary:
    options = options or RenderOptions()
    _validate_mode(options.mode)
    users = _select_users(registry, options.user_name)
    provider_lines = _load_provider_lines(config) if _mode_includes_raw(options.mode) else ()
    fetcher = fetch_subscription or _fetch_url
    summary = RenderSummary()

    for user in users:
        result = UserRenderResult(user_name=user.name)
        skipped = 0
        try:
            if _mode_includes_yaml(options.mode):
                _render_yaml(config, user)
                result.rendered += 1
            else:
                skipped += 1

            if _mode_includes_raw(options.mode):
                _render_raw(
                    config,
                    user,
                    provider_lines,
                    fetch_subscription=fetcher,
                    timeout_seconds=options.timeout_seconds,
                )
                result.rendered += 1
            else:
                skipped += 1
            result.skipped += skipped
        except (UpstreamError, RenderError, ValidationError) as exc:
            result.failed += 1
            result.error = str(exc)

        summary.users.append(result)
        summary.rendered += result.rendered
        summary.skipped += result.skipped
        summary.failed += result.failed

    _write_render_status(config, summary)
    return summary


def format_summary(summary: RenderSummary) -> str:
    return (
        "render summary: "
        f"rendered={summary.rendered} skipped={summary.skipped} failed={summary.failed}"
    )


def _validate_mode(mode: RenderMode) -> None:
    if mode not in {"all", "yaml-only", "raw-only"}:
        raise ValidationError(f"unknown render mode: {mode}")


def _select_users(registry: UserRegistry, user_name: str | None) -> list[User]:
    if user_name is None:
        return [registry.users[name] for name in sorted(registry.users)]
    user = registry.users.get(user_name)
    if user is None:
        raise ValidationError(f"unknown user: {user_name}")
    return [user]


def _mode_includes_yaml(mode: RenderMode) -> bool:
    return mode in {"all", "yaml-only"}


def _mode_includes_raw(mode: RenderMode) -> bool:
    return mode in {"all", "raw-only"}


def _render_yaml(config: AppConfig, user: User) -> None:
    target = yaml_output_path(config, user)
    content = render_user_yaml(config, user)
    try:
        ensure_public_directory(target.parent)
        atomic_write_text(target, content, mode=PUBLIC_FILE_MODE)
    except OSError as exc:
        raise RenderError(f"failed to write YAML for user {user.name}") from exc


def _render_raw(
    config: AppConfig,
    user: User,
    provider_lines: tuple[str, ...],
    *,
    fetch_subscription: FetchSubscription,
    timeout_seconds: float,
) -> None:
    personal_feed = _fetch_personal_feed(user, fetch_subscription, timeout_seconds)
    private_lines = (
        tuple(
            _prefix_uri_fragment(line, config.render.private_prefix)
            for line in personal_feed.uris
        )
        if config.render.include_private
        else ()
    )
    prefixed_provider = tuple(
        _prefix_uri_fragment(line, config.render.provider_prefix) for line in provider_lines
    ) if config.render.include_provider else ()
    raw_parts = (private_lines, prefixed_provider)
    if config.render.provider_first:
        raw_parts = (prefixed_provider, private_lines)
    plain = "\n".join(line for part in raw_parts for line in part) + "\n"
    encoded = base64.b64encode(plain.encode("utf-8")).decode("ascii")
    target = config.public.output_dir / "s" / f"{user.token}.raw"
    try:
        ensure_public_directory(target.parent)
        atomic_write_text(target, encoded, mode=PUBLIC_FILE_MODE)
    except OSError as exc:
        raise RenderError(f"failed to write raw subscription for user {user.name}") from exc


def _load_provider_lines(config: AppConfig) -> tuple[str, ...]:
    path = config.state_dir / "cache" / "provider.decoded"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationError(
            "provider cache is missing; run `subctl refresh-provider` before rendering raw output"
        ) from exc
    except OSError as exc:
        raise ValidationError("provider cache cannot be read") from exc
    feed = validate_provider_subscription(text)
    keywords = config.render.provider_exclude_keywords
    return tuple(uri for uri in feed.uris if not _uri_name_contains(uri, keywords))


def _fetch_personal_feed(
    user: User,
    fetch_subscription: FetchSubscription,
    timeout_seconds: float,
):
    try:
        body = fetch_subscription(user.xui_subscription, timeout_seconds)
    except urllib.error.HTTPError as exc:
        raise UpstreamError(f"personal subscription fetch failed for user {user.name}: HTTP status {exc.code}") from exc
    except TimeoutError as exc:
        raise UpstreamError(f"personal subscription fetch failed for user {user.name}: timeout") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise UpstreamError(f"personal subscription fetch failed for user {user.name}: timeout") from exc
        raise UpstreamError(f"personal subscription fetch failed for user {user.name}") from exc
    except OSError as exc:
        raise UpstreamError(f"personal subscription fetch failed for user {user.name}") from exc

    try:
        return validate_provider_subscription(body)
    except ValidationError as exc:
        raise UpstreamError(f"personal subscription is invalid for user {user.name}") from exc


def _fetch_url(url: str, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "subctl/0.1"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise UpstreamError(f"personal subscription fetch failed: HTTP status {status}")
        return response.read()


def _prefix_uri_fragment(uri: str, prefix: str) -> str:
    parts = urlsplit(uri)
    if not parts.fragment:
        return uri
    fragment = quote(prefix + unquote(parts.fragment), safe="")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, fragment))


def _provider_exclude_filter(keywords: tuple[str, ...]) -> str | None:
    if not keywords:
        return None
    alternatives = "|".join(re.escape(keyword) for keyword in keywords)
    return f"(?i)(?:{alternatives})"


def _uri_name_contains(uri: str, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return False
    name = unquote(urlsplit(uri).fragment).casefold()
    return any(keyword.casefold() in name for keyword in keywords)


def _write_render_status(config: AppConfig, summary: RenderSummary) -> None:
    """Persist timer/CLI renders as well as web renders for the UI."""
    path = config.state_dir / "ui" / "render-status.json"
    existing: dict[str, object] = {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            existing = value
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    users = existing.get("users") if isinstance(existing.get("users"), dict) else {}
    now = datetime.now(timezone.utc).isoformat()
    config_version = _settings_version(config)
    for result in summary.users:
        users[result.user_name] = {
            "status": "failed" if result.failed else "succeeded",
            "rendered": result.rendered,
            "failed": result.failed,
            "error": result.error,
            "finished_at": now,
            "config_version": config_version,
        }
    content = json.dumps({"users": users}, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, content, mode=0o600)
    except OSError:
        # Rendering itself has already completed; observability must not make
        # the subscription command fail because the optional state file is
        # unavailable.
        pass


def _settings_version(config: AppConfig) -> int:
    path = config.state_dir / "ui" / "settings.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        version = value.get("version") if isinstance(value, dict) else 0
        return version if isinstance(version, int) and version >= 0 else 0
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return 0
