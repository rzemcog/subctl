import os
import stat

import yaml

from conftest import (
    VALID_ALICE_TOKEN,
    VALID_BOB_TOKEN,
    VALID_CAROL_TOKEN,
    assert_no_traceback,
)


def load_users(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_list_users_masks_tokens_and_hides_upstream_url(run_subctl, cli_paths):
    result = run_subctl(*cli_paths, "list-users")

    assert result.returncode == 0, result.stderr
    assert "alice" in result.stdout
    assert "bob" in result.stdout
    assert VALID_ALICE_TOKEN not in result.stdout
    assert VALID_BOB_TOKEN not in result.stdout
    assert "provider.example" not in result.stdout
    assert "https://provider.example/subscription" not in result.stderr


def test_add_user_creates_users_file_with_mode_0600_on_linux(
    run_subctl, tmp_path, config_path
):
    users = tmp_path / "new-users.yaml"

    result = run_subctl(
        "--config",
        config_path,
        "--users",
        users,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "add-user",
        "--name",
        "carol",
        "--xui-sub-url",
        "https://panel.example.com/sub/carol",
        "--token",
        VALID_CAROL_TOKEN,
    )

    assert result.returncode == 0, result.stderr
    data = load_users(users)
    assert data["users"]["carol"] == {
        "token": VALID_CAROL_TOKEN,
        "xui_subscription": "https://panel.example.com/sub/carol",
    }

    if os.name == "posix":
        mode = stat.S_IMODE(users.stat().st_mode)
        assert mode == 0o600


def test_add_user_rejects_duplicate_name_without_force(
    run_subctl, tmp_path, config_path, users_path
):
    original = users_path.read_text(encoding="utf-8")

    result = run_subctl(
        "--config",
        config_path,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "add-user",
        "--name",
        "alice",
        "--xui-sub-url",
        "https://panel.example.com/sub/new-alice",
    )

    assert result.returncode != 0
    assert_no_traceback(result)
    assert "alice" in (result.stdout + result.stderr)
    assert users_path.read_text(encoding="utf-8") == original


def test_add_user_force_updates_subscription_without_changing_token(
    run_subctl, tmp_path, config_path, users_path
):
    result = run_subctl(
        "--config",
        config_path,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "add-user",
        "--name",
        "alice",
        "--xui-sub-url",
        "https://panel.example.com/sub/replaced-alice",
        "--force",
    )

    assert result.returncode == 0, result.stderr
    alice = load_users(users_path)["users"]["alice"]
    assert alice["token"] == VALID_ALICE_TOKEN
    assert alice["xui_subscription"] == "https://panel.example.com/sub/replaced-alice"


def test_add_user_force_with_token_overrides_existing_token(
    run_subctl, tmp_path, config_path, users_path
):
    replacement_token = "alice_replacement_token_1234567890ab"

    result = run_subctl(
        "--config",
        config_path,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "add-user",
        "--name",
        "alice",
        "--xui-sub-url",
        "https://panel.example.com/sub/alice-2",
        "--token",
        replacement_token,
        "--force",
    )

    assert result.returncode == 0, result.stderr
    alice = load_users(users_path)["users"]["alice"]
    assert alice["token"] == replacement_token
    assert alice["xui_subscription"] == "https://panel.example.com/sub/alice-2"


def test_add_user_generated_token_is_url_safe_and_not_printed(
    run_subctl, tmp_path, config_path, users_path
):
    result = run_subctl(
        "--config",
        config_path,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "add-user",
        "--name",
        "carol",
        "--xui-sub-url",
        "https://panel.example.com/sub/carol",
    )

    assert result.returncode == 0, result.stderr
    token = load_users(users_path)["users"]["carol"]["token"]
    assert len(token) >= 32
    assert all(char.isalnum() or char in "-_" for char in token)
    assert token not in result.stdout
    assert token not in result.stderr


def test_failed_add_user_validation_does_not_corrupt_existing_file(
    run_subctl, tmp_path, config_path, users_path
):
    original = users_path.read_bytes()

    result = run_subctl(
        "--config",
        config_path,
        "--users",
        users_path,
        "--state-dir",
        tmp_path / "state",
        "--output-dir",
        tmp_path / "public",
        "add-user",
        "--name",
        "carol",
        "--xui-sub-url",
        "https://panel.example.com/sub/carol",
        "--token",
        VALID_BOB_TOKEN,
    )

    assert result.returncode != 0
    assert_no_traceback(result)
    assert users_path.read_bytes() == original
