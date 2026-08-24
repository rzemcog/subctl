import grp
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import VALID_ALICE_TOKEN, VALID_PROVIDER_TOKEN, assert_secret_not_printed
from subctl.config import load_config
from subctl.errors import RenderError, ValidationError
from subctl.registry import load_users
from subctl.render import build_gateway_profile, render_gateway, render_user_yaml


PRIVATE_URL = "https://panel.example.com/sub/gateway-private-secret"
CONTROLLER_SECRET = "controller-secret-0123456789-abcdef"


@pytest.fixture
def gateway_config_data(config_data, tmp_path):
    uid = os.getuid()
    gid = os.getgid()
    config_data["gateway"] = {
        "private_upstream_url": PRIVATE_URL,
        "controller_secret": CONTROLLER_SECRET,
        "output_path": str(tmp_path / "private" / "gateway.yaml"),
        "output_owner": pwd.getpwuid(uid).pw_name,
        "output_group": grp.getgrgid(gid).gr_name,
        "socks_port": 10808,
        "controller_port": 19090,
        "base_default": "PROXY",
        "physical_interface": "eth-test0",
        "external_ui_path": "/var/lib/mihomo/ui/current",
    }
    return config_data


@pytest.fixture
def gateway_config_path(write_yaml, gateway_config_data):
    return write_yaml("gateway-config.yaml", gateway_config_data)


def test_gateway_profile_uses_direct_upstreams_and_shared_routing(
    gateway_config_path, users_path
):
    config = load_config(gateway_config_path)
    profile = build_gateway_profile(config)
    public_profile = yaml.safe_load(
        render_user_yaml(config, load_users(users_path).users["alice"])
    )

    assert profile["proxy-providers"]["private"]["url"] == PRIVATE_URL
    assert profile["proxy-providers"]["provider"]["url"] == config.provider.upstream_url
    assert config.public.base_url not in yaml.safe_dump(profile)
    assert VALID_PROVIDER_TOKEN not in yaml.safe_dump(profile)
    assert VALID_ALICE_TOKEN not in yaml.safe_dump(profile)
    assert profile["proxy-groups"] == [
        {**group, "proxies": ["PROXY", "DIRECT"]}
        if group["name"] == "BASE"
        else group
        for group in public_profile["proxy-groups"]
    ]
    assert profile["rules"][2:] == public_profile["rules"]

    providers = profile["proxy-providers"]
    assert providers["private"]["interval"] == 3600
    assert providers["provider"]["interval"] == 900
    for provider in providers.values():
        assert provider["health-check"] == {
            "enable": True,
            "url": "https://www.gstatic.com/generate_204",
            "interval": 15,
            "timeout": 3000,
            "lazy": True,
        }

    groups = {group["name"]: group for group in profile["proxy-groups"]}
    assert list(groups) == ["PRIVATE", "PROVIDER-AUTO", "AUTO", "PROXY", "BASE"]
    assert groups["PROVIDER-AUTO"]["max-failed-times"] == 2
    assert groups["PROVIDER-AUTO"]["tolerance"] == 50
    assert groups["BASE"]["proxies"] == ["PROXY", "DIRECT"]
    assert profile["rules"][-1] == "MATCH,BASE"


def test_gateway_profile_has_loopback_listeners_secret_and_disabled_tun(
    gateway_config_path,
):
    profile = build_gateway_profile(load_config(gateway_config_path))

    assert profile["listeners"] == [
        {"name": "local-socks", "type": "socks", "listen": "127.0.0.1", "port": 10808}
    ]
    assert profile["external-controller"] == "127.0.0.1:19090"
    assert profile["external-ui"] == "/var/lib/mihomo/ui/current"
    assert profile["secret"] == CONTROLLER_SECRET
    assert profile["tun"] == {"enable": False}
    assert profile["interface-name"] == "eth-test0"
    assert profile["log-level"] == "warning"
    assert profile["dns"] == {
        "enable": True,
        "ipv6": False,
        "enhanced-mode": "redir-host",
        "default-nameserver": ["1.1.1.1", "8.8.8.8"],
        "nameserver": ["1.1.1.1", "8.8.8.8"],
        "proxy-server-nameserver": ["1.1.1.1", "8.8.8.8"],
        "direct-nameserver": ["1.1.1.1", "8.8.8.8"],
        "direct-nameserver-follow-policy": False,
    }


def test_gateway_external_ui_path_has_safe_default(
    gateway_config_data, write_yaml
):
    del gateway_config_data["gateway"]["external_ui_path"]
    config = load_config(write_yaml("gateway-default-ui.yaml", gateway_config_data))

    assert config.gateway is not None
    assert config.gateway.external_ui_path == Path("/var/lib/mihomo/ui/current")
    assert config.gateway.tun_output_path == Path("/etc/mihomo/config-tun.yaml")


def test_gateway_tun_profile_is_explicit_and_preserves_direct_bootstrap(
    gateway_config_path,
):
    config = load_config(gateway_config_path)
    profile = build_gateway_profile(config, enable_tun=True)

    assert profile["tun"] == {
        "enable": True,
        "stack": "mixed",
        "device": "mihomo0",
        "auto-route": True,
        "auto-redirect": True,
        "auto-detect-interface": False,
        "strict-route": True,
        "dns-hijack": ["any:53", "tcp://any:53"],
        "route-exclude-address": [
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            "100.64.0.0/10",
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        ],
    }
    assert profile["rules"][:3] == [
        "DOMAIN,panel.example.com,DIRECT",
        "DOMAIN,provider.example,DIRECT",
        "DOMAIN,sub.example.com,DIRECT",
    ]
    assert profile["tun"] != {"enable": False}



def test_gateway_tun_profile_appends_runtime_management_excludes(
    gateway_config_path,
):
    config = load_config(gateway_config_path)
    profile = build_gateway_profile(
        config,
        enable_tun=True,
        tun_route_exclude_addresses=("203.0.113.7", "198.51.100.0/24"),
    )

    assert profile["tun"]["route-exclude-address"][-2:] == [
        "203.0.113.7/32",
        "198.51.100.0/24",
    ]


def test_gateway_tun_profile_rejects_invalid_runtime_exclude(
    gateway_config_path,
):
    config = load_config(gateway_config_path)

    with pytest.raises(ValidationError, match="invalid TUN route exclude"):
        build_gateway_profile(
            config, enable_tun=True, tun_route_exclude_addresses=("not-an-ip",)
        )


def test_gateway_external_ui_path_must_be_absolute(
    gateway_config_data, write_yaml
):
    gateway_config_data["gateway"]["external_ui_path"] = "relative/ui"
    path = write_yaml("gateway-relative-ui.yaml", gateway_config_data)

    with pytest.raises(
        ValidationError, match="gateway.external_ui_path must be an absolute path"
    ):
        load_config(path)


def test_gateway_profile_bootstraps_upstream_hosts_direct_only(
    gateway_config_path, users_path
):
    config = load_config(gateway_config_path)
    profile = build_gateway_profile(config)

    assert profile["rules"][:2] == [
        "DOMAIN,panel.example.com,DIRECT",
        "DOMAIN,provider.example,DIRECT",
    ]
    assert profile["rules"][-1] == "MATCH,BASE"

    public_profile = yaml.safe_load(
        render_user_yaml(config, load_users(users_path).users["alice"])
    )
    assert "interface-name" not in public_profile
    assert "log-level" not in public_profile
    assert "external-controller" not in public_profile
    assert "external-ui" not in public_profile
    assert "secret" not in public_profile
    assert not any(
        rule.startswith("DOMAIN,panel.example.com,") for rule in public_profile["rules"]
    )
    assert not any(
        rule.startswith("DOMAIN,provider.example,") for rule in public_profile["rules"]
    )


def test_render_gateway_is_atomic_and_mode_0600(gateway_config_path):
    config = load_config(gateway_config_path)
    target = render_gateway(config)
    first = target.read_text(encoding="utf-8")
    target.chmod(0o644)

    assert render_gateway(config) == target
    assert target.read_text(encoding="utf-8") == first
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_uid == os.getuid()
    assert target.stat().st_gid == os.getgid()
    assert not list(target.parent.glob(".*.tmp"))


def test_replace_failure_preserves_previous_gateway(
    gateway_config_path, monkeypatch
):
    config = load_config(gateway_config_path)
    target = render_gateway(config)
    previous = target.read_bytes()

    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(RenderError, match="failed to write gateway profile"):
        render_gateway(config)

    assert target.read_bytes() == previous
    assert not list(target.parent.glob(".*.tmp"))


def test_chown_failure_preserves_previous_gateway(
    gateway_config_path, monkeypatch
):
    config = load_config(gateway_config_path)
    target = render_gateway(config)
    previous = target.read_bytes()

    def fail_chown(path, uid, gid):
        raise PermissionError("synthetic chown failure")

    monkeypatch.setattr(os, "chown", fail_chown)

    with pytest.raises(RenderError, match="failed to write gateway profile"):
        render_gateway(config)

    assert target.read_bytes() == previous
    assert not list(target.parent.glob(".*.tmp"))


def test_invalid_config_preserves_previous_gateway(
    gateway_config_path, gateway_config_data, write_yaml
):
    config = load_config(gateway_config_path)
    target = render_gateway(config)
    previous = target.read_bytes()
    gateway_config_data["gateway"]["base_default"] = "INVALID"
    invalid_path = write_yaml("invalid-gateway.yaml", gateway_config_data)

    with pytest.raises(ValidationError, match="base_default"):
        load_config(invalid_path)

    assert target.read_bytes() == previous


def test_cli_render_gateway_redacts_secrets(gateway_config_path, tmp_path):
    output = tmp_path / "overridden" / "gateway.yaml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "subctl.cli",
            "--config",
            str(gateway_config_path),
            "render-gateway",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.exists()
    assert_secret_not_printed(result, PRIVATE_URL, CONTROLLER_SECRET, VALID_PROVIDER_TOKEN)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_gateway_secrets_do_not_leak_into_public_profile(
    gateway_config_path, users_path
):
    config = load_config(gateway_config_path)
    output = render_user_yaml(config, load_users(users_path).users["alice"])

    assert PRIVATE_URL not in output
    assert CONTROLLER_SECRET not in output
    assert config.provider.upstream_url not in output


def test_render_gateway_rejects_public_output(gateway_config_path, tmp_path):
    config = load_config(gateway_config_path)

    with pytest.raises(ValidationError, match="outside public.output_dir"):
        render_gateway(config, output=tmp_path / "public" / "gateway.yaml")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("private_upstream_url", "not-a-url/private-secret", "private_upstream_url"),
        ("controller_secret", "short", "at least 32"),
        ("output_path", 42, "path string"),
        ("output_owner", "", "non-empty string"),
        ("output_group", 42, "non-empty string"),
        ("socks_port", 0, "between 1 and 65535"),
        ("controller_port", 65536, "between 1 and 65535"),
        ("base_default", "AUTO", "DIRECT or PROXY"),
        ("physical_interface", "bad interface name", "valid interface name"),
    ],
)
def test_gateway_config_validation(
    gateway_config_data, write_yaml, field, value, message
):
    gateway_config_data["gateway"][field] = value
    path = write_yaml(f"invalid-{field}.yaml", gateway_config_data)

    with pytest.raises(ValidationError, match=message) as exc_info:
        load_config(path)

    assert PRIVATE_URL not in str(exc_info.value)
    assert CONTROLLER_SECRET not in str(exc_info.value)


def test_render_gateway_requires_gateway_section(config_path):
    with pytest.raises(ValidationError, match="gateway section"):
        render_gateway(load_config(config_path))


def test_gateway_ports_must_be_distinct(gateway_config_data, write_yaml):
    gateway_config_data["gateway"]["controller_port"] = 10808
    path = write_yaml("same-ports.yaml", gateway_config_data)

    with pytest.raises(ValidationError, match="must be different"):
        load_config(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("output_owner", "subctl-owner-does-not-exist", "account does not exist"),
        ("output_group", "subctl-group-does-not-exist", "group does not exist"),
    ],
)
def test_gateway_ownership_resolution_errors_are_actionable(
    gateway_config_data, write_yaml, field, value, message
):
    gateway_config_data["gateway"][field] = value
    config = load_config(write_yaml(f"unknown-{field}.yaml", gateway_config_data))

    with pytest.raises(ValidationError, match=message):
        render_gateway(config)
