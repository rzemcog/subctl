import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


VALID_PROVIDER_TOKEN = "provider_shared_token_1234567890abcd"
VALID_ALICE_TOKEN = "alice_token_1234567890abcdefghijkl"
VALID_BOB_TOKEN = "bob_token_1234567890abcdefghijklmn"
VALID_CAROL_TOKEN = "carol_token_1234567890abcdefghijkl"


@pytest.fixture
def run_subctl():
    def _run_subctl(*args, cwd=None, env=None):
        executable = shutil.which("subctl")
        if executable is None:
            pytest.fail("subctl executable is not on PATH; run `pip install -e .[dev]` first")
        command = [executable, *map(str, args)]
        completed_env = os.environ.copy()
        completed_env.update(env or {})
        return subprocess.run(
            command,
            cwd=cwd,
            env=completed_env,
            text=True,
            capture_output=True,
            check=False,
        )

    return _run_subctl


@pytest.fixture
def config_data(tmp_path):
    return {
        "provider": {
            "upstream_url": "https://provider.example/subscription",
            "shared_token": VALID_PROVIDER_TOKEN,
            "refresh_interval_seconds": 900,
        },
        "public": {
            "base_url": "https://sub.example.com",
            "output_dir": str(tmp_path / "public"),
        },
        "render": {
            "profile_update_interval_seconds": 3600,
            "provider_update_interval_seconds": 900,
            "healthcheck_url": "https://www.gstatic.com/generate_204",
            "healthcheck_interval_seconds": 15,
            "healthcheck_timeout_milliseconds": 3000,
            "healthcheck_max_failed_times": 2,
            "healthcheck_tolerance_milliseconds": 50,
            "healthcheck_lazy": True,
            "provider_exclude_keywords": ["Киев", "Москва"],
        },
    }


@pytest.fixture
def users_data():
    return {
        "users": {
            "alice": {
                "token": VALID_ALICE_TOKEN,
                "xui_subscription": "https://panel.example.com/sub/alice",
            },
            "bob": {
                "token": VALID_BOB_TOKEN,
                "xui_subscription": "https://panel.example.com/sub/bob",
            },
        }
    }


@pytest.fixture
def write_yaml(tmp_path):
    def _write_yaml(name, data):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write_yaml


@pytest.fixture
def config_path(write_yaml, config_data):
    return write_yaml("config.yaml", config_data)


@pytest.fixture
def users_path(write_yaml, users_data):
    return write_yaml("users.yaml", users_data)


@pytest.fixture
def cli_paths(tmp_path, config_path, users_path):
    return [
        "--config",
        config_path,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
    ]


def assert_no_traceback(result):
    output = result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in output


def assert_secret_not_printed(result, *secrets):
    output = result.stdout + result.stderr
    for secret in secrets:
        assert secret not in output
