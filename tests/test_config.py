import pytest
import yaml

from subctl.config import load_config, validate_settings_overlay
from subctl.errors import ValidationError
from conftest import (
    VALID_ALICE_TOKEN,
    VALID_PROVIDER_TOKEN,
    assert_no_traceback,
    assert_secret_not_printed,
)


def test_config_and_users_happy_path_list_users(run_subctl, cli_paths):
    result = run_subctl(*cli_paths, "list-users")

    assert result.returncode == 0, result.stderr
    assert "alice" in result.stdout
    assert "bob" in result.stdout
    assert "https://provider.example/subscription" not in result.stdout
    assert VALID_ALICE_TOKEN not in result.stdout


def test_invalid_yaml_reports_clean_error(run_subctl, tmp_path, config_path, cli_paths):
    broken_users = tmp_path / "broken-users.yaml"
    broken_users.write_text("users:\n  alice: [not closed\n", encoding="utf-8")

    result = run_subctl(
        "--config",
        config_path,
        "--users",
        broken_users,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "list-users",
    )

    assert result.returncode != 0
    assert_no_traceback(result)
    assert "yaml" in (result.stderr + result.stdout).lower()


def test_missing_required_config_fields_are_listed(
    run_subctl, tmp_path, write_yaml, users_path
):
    config = write_yaml(
        "missing-config.yaml",
        {
            "provider": {"refresh_interval_seconds": 900},
            "public": {},
            "render": {},
        },
    )

    result = run_subctl(
        "--config",
        config,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "list-users",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert_no_traceback(result)
    assert "provider.upstream_url" in output
    assert "provider.shared_token" in output
    assert "public.base_url" in output


@pytest.mark.parametrize(
    ("section", "field", "value", "expected"),
    [
        ("provider", "upstream_url", "ftp://provider.example/sub", "upstream_url"),
        ("provider", "shared_token", "too-short", "shared_token"),
        ("public", "base_url", "file:///tmp/public", "base_url"),
        ("render", "healthcheck_url", "ssh://health.example/check", "healthcheck_url"),
        ("render", "provider_download_proxy", "", "provider_download_proxy"),
        ("render", "provider_download_proxy", 123, "provider_download_proxy"),
        ("render", "provider_download_proxy", "DIRECT\nMATCH,PROXY", "provider_download_proxy"),
        ("render", "healthcheck_timeout_milliseconds", 0, "healthcheck_timeout_milliseconds"),
        ("render", "healthcheck_max_failed_times", 0, "healthcheck_max_failed_times"),
        ("render", "healthcheck_tolerance_milliseconds", -1, "healthcheck_tolerance_milliseconds"),
        ("render", "healthcheck_lazy", "true", "healthcheck_lazy"),
        ("render", "provider_exclude_keywords", "Киев", "provider_exclude_keywords"),
        ("render", "provider_exclude_keywords", [""], "provider_exclude_keywords"),
        (
            "render",
            "provider_exclude_keywords",
            ["Москва", "москва"],
            "provider_exclude_keywords",
        ),
    ],
)
def test_invalid_config_values_are_rejected(
    run_subctl,
    tmp_path,
    write_yaml,
    config_data,
    users_path,
    section,
    field,
    value,
    expected,
):
    config_data[section][field] = value
    config = write_yaml(f"invalid-{section}-{field}.yaml", config_data)

    result = run_subctl(
        "--config",
        config,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "list-users",
    )

    assert result.returncode != 0
    assert_no_traceback(result)
    assert expected in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda users: users["users"]["alice"].__setitem__(
                "xui_subscription", "mailto:alice@example.com"
            ),
            "xui_subscription",
        ),
        (
            lambda users: users["users"]["alice"].__setitem__("token", "short"),
            "token",
        ),
        (
            lambda users: users["users"]["alice"].pop("xui_subscription"),
            "xui_subscription",
        ),
        (
            lambda users: users["users"]["bob"].__setitem__(
                "token", users["users"]["alice"]["token"]
            ),
            "token",
        ),
    ],
)
def test_invalid_users_values_are_rejected(
    run_subctl, tmp_path, write_yaml, config_path, users_data, mutate, expected
):
    mutate(users_data)
    users = write_yaml("invalid-users.yaml", users_data)

    result = run_subctl(
        "--config",
        config_path,
        "--users",
        users,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "list-users",
    )

    assert result.returncode != 0
    assert_no_traceback(result)
    assert expected in (result.stdout + result.stderr)


def test_validation_errors_do_not_print_full_secrets(
    run_subctl, tmp_path, write_yaml, config_data, users_data
):
    config_data["provider"]["shared_token"] = VALID_PROVIDER_TOKEN
    users_data["users"]["alice"]["xui_subscription"] = "ftp://panel.example.com/sub/alice"
    config = write_yaml("config.yaml", config_data)
    users = write_yaml("users.yaml", users_data)

    result = run_subctl(
        "--config",
        config,
        "--users",
        users,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "list-users",
    )

    assert result.returncode != 0
    assert_secret_not_printed(result, VALID_PROVIDER_TOKEN, VALID_ALICE_TOKEN)


def test_examples_are_valid_yaml():
    for path in ("examples/config.yaml", "examples/users.yaml"):
        with open(path, encoding="utf-8") as handle:
            assert yaml.safe_load(handle)


def test_mihomo_hosts_are_read_from_runtime_config_and_normalized(
    write_yaml, config_data, provider_state_dir
):
    config_data["mihomo"] = {
        "hosts": {
            "Sub.Example.com.": "192.0.2.27",
            "ipv6.example.test": "2001:0db8:0:0::1",
        }
    }
    path = write_yaml("mihomo-hosts.yaml", config_data)

    config = load_config(path, state_dir=provider_state_dir)

    assert dict(config.mihomo.hosts) == {
        "sub.example.com": "192.0.2.27",
        "ipv6.example.test": "2001:db8::1",
    }


def test_mihomo_hosts_are_optional_for_existing_runtime_configs(
    write_yaml, config_data, provider_state_dir
):
    config_data.pop("mihomo")
    path = write_yaml("legacy-config.yaml", config_data)

    config = load_config(path, state_dir=provider_state_dir)

    assert dict(config.mihomo.hosts) == {}


@pytest.mark.parametrize(
    "mihomo",
    [
        [],
        {"hosts": []},
        {"hosts": {"invalid host.example": "192.0.2.27"}},
        {"hosts": {"192.0.2.27": "192.0.2.28"}},
        {"hosts": {"sub.example.test": "not-an-ip"}},
        {
            "hosts": {
                "Sub.Example.test.": "192.0.2.27",
                "sub.example.test": "192.0.2.28",
            }
        },
    ],
)
def test_invalid_mihomo_hosts_are_rejected(
    write_yaml, config_data, provider_state_dir, mihomo
):
    config_data["mihomo"] = mihomo
    path = write_yaml("invalid-mihomo-hosts.yaml", config_data)

    with pytest.raises(ValidationError, match="mihomo"):
        load_config(path, state_dir=provider_state_dir)


def test_mihomo_hosts_are_not_mutable_through_ui_settings():
    with pytest.raises(ValidationError, match="unsupported field"):
        validate_settings_overlay(
            {"mihomo": {"hosts": {"sub.example.test": "192.0.2.27"}}}
        )
