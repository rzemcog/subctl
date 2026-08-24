from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG_PATH, DEFAULT_STATE_DIR, DEFAULT_USERS_PATH, load_config
from .errors import RenderError, SubctlError, UpstreamError, ValidationError
from .refresh import refresh_provider
from .render import RenderOptions, format_summary, render_gateway, render_subscriptions
from .registry import add_user, load_users


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subctl",
        description="Manage generated-file VPN subscriptions.",
    )
    parser.add_argument("--version", action="version", version=f"subctl {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"config YAML path (default for config-dependent commands: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--users", type=Path, default=DEFAULT_USERS_PATH, help="users YAML path")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="state directory")
    parser.add_argument("--output-dir", type=Path, default=None, help="public output directory")

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-users", help="list configured users")
    list_parser.set_defaults(func=_cmd_list_users)

    add_parser = subparsers.add_parser("add-user", help="add or update a user")
    add_parser.add_argument("--name", required=True, help="user name")
    add_parser.add_argument("--xui-sub-url", required=True, help="3x-ui subscription URL")
    add_parser.add_argument("--token", help="explicit URL-safe token")
    add_parser.add_argument("--force", action="store_true", help="update existing user")
    add_parser.set_defaults(func=_cmd_add_user)

    refresh_parser = subparsers.add_parser(
        "refresh-provider",
        help="download, validate, and publish the shared provider feed",
    )
    refresh_parser.set_defaults(func=_cmd_refresh_provider)

    render_parser = subparsers.add_parser("render", help="render user subscription files")
    mode_group = render_parser.add_mutually_exclusive_group()
    mode_group.add_argument("--all", action="store_true", help="render YAML and raw outputs (default)")
    mode_group.add_argument("--yaml-only", action="store_true", help="render only Mihomo YAML outputs")
    mode_group.add_argument("--raw-only", action="store_true", help="render only raw Base64 outputs")
    render_parser.add_argument("--user", help="render only the named user")
    render_parser.set_defaults(func=_cmd_render)

    gateway_parser = subparsers.add_parser(
        "render-gateway", help="render the private Mihomo gateway profile"
    )
    gateway_parser.add_argument("--output", type=Path, help="override gateway output path")
    gateway_parser.add_argument(
        "--tun",
        action="store_true",
        help="render the cutover profile with Mihomo TUN enabled",
    )
    gateway_parser.add_argument(
        "--tun-route-exclude-address",
        action="append",
        default=[],
        metavar="IP_OR_CIDR",
        help="runtime IP/CIDR to keep outside the Mihomo TUN route (repeatable)",
    )
    gateway_parser.set_defaults(func=_cmd_render_gateway)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValidationError as exc:
        print(f"subctl: error: {exc}", file=sys.stderr)
        return 1
    except UpstreamError as exc:
        print(f"subctl: error: {exc}", file=sys.stderr)
        return 2
    except RenderError as exc:
        print(f"subctl: error: {exc}", file=sys.stderr)
        return 3
    except SubctlError as exc:
        print(f"subctl: error: {exc}", file=sys.stderr)
        return 3


def _cmd_list_users(args: argparse.Namespace) -> int:
    _load_config_for_future_commands(args)
    registry = load_users(args.users)
    for user in registry.users.values():
        print(f"{user.name}\t{user.masked_token}")
    return 0


def _cmd_add_user(args: argparse.Namespace) -> int:
    _load_config_for_future_commands(args)
    user = add_user(
        args.users,
        name=args.name,
        xui_sub_url=args.xui_sub_url,
        token=args.token,
        force=args.force,
    )
    print(f"{user.name}\t{user.masked_token}")
    return 0


def _cmd_refresh_provider(args: argparse.Namespace) -> int:
    config_path = args.config if args.config is not None else DEFAULT_CONFIG_PATH
    config = load_config(config_path, state_dir=args.state_dir, output_dir=args.output_dir)
    refresh_provider(config)
    print("provider refreshed")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    config_path = args.config if args.config is not None else DEFAULT_CONFIG_PATH
    config = load_config(config_path, state_dir=args.state_dir, output_dir=args.output_dir)
    registry = load_users(args.users)
    summary = render_subscriptions(
        config,
        registry,
        options=RenderOptions(mode=_render_mode_from_args(args), user_name=args.user),
    )
    print(format_summary(summary))
    failed_users = [user for user in summary.users if user.failed]
    for user in failed_users:
        print(f"render failed for user {user.user_name}: {user.error}", file=sys.stderr)
    if not summary.failed:
        return 0
    if summary.rendered:
        return 4
    if any(
        user.error and ("fetch failed" in user.error or "subscription is invalid" in user.error)
        for user in failed_users
    ):
        return 2
    return 3


def _cmd_render_gateway(args: argparse.Namespace) -> int:
    config_path = args.config if args.config is not None else DEFAULT_CONFIG_PATH
    config = load_config(config_path, state_dir=args.state_dir, output_dir=args.output_dir)
    target = render_gateway(
        config,
        output=args.output,
        enable_tun=args.tun,
        tun_route_exclude_addresses=tuple(args.tun_route_exclude_address),
    )
    print(f"gateway profile rendered: {target}")
    return 0


def _render_mode_from_args(args: argparse.Namespace) -> str:
    if args.yaml_only:
        return "yaml-only"
    if args.raw_only:
        return "raw-only"
    return "all"


def _load_config_for_future_commands(args: argparse.Namespace):
    if args.config is None:
        return None
    return load_config(args.config, state_dir=args.state_dir, output_dir=args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
