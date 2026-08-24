import stat

import yaml

from conftest import VALID_ALICE_TOKEN, VALID_BOB_TOKEN, VALID_PROVIDER_TOKEN
from subctl.config import load_config
from subctl.registry import load_users
from subctl.render import render_user_yaml, render_users


def test_render_user_yaml_matches_golden_snapshot(config_path, users_path):
    config = load_config(config_path)
    registry = load_users(users_path)

    actual = render_user_yaml(config, registry.users["alice"])
    expected = _fixture("alice_mihomo.yaml")

    assert actual == expected


def test_render_command_writes_parseable_yaml_for_multiple_users(
    run_subctl, cli_paths, tmp_path
):
    result = run_subctl(*cli_paths, "render", "--yaml-only")

    assert result.returncode == 0, result.stderr
    assert "render summary: rendered=2 skipped=2 failed=0" in result.stdout

    alice_path = tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.yaml"
    bob_path = tmp_path / "public/s" / f"{VALID_BOB_TOKEN}.yaml"
    assert alice_path.exists()
    assert bob_path.exists()
    assert stat.S_IMODE(alice_path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(alice_path.stat().st_mode) == 0o644

    alice = yaml.safe_load(alice_path.read_text(encoding="utf-8"))
    bob = yaml.safe_load(bob_path.read_text(encoding="utf-8"))
    assert alice["proxy-providers"]["private"]["url"] == "https://panel.example.com/sub/alice"
    assert bob["proxy-providers"]["private"]["url"] == "https://panel.example.com/sub/bob"
    assert alice_path.read_text(encoding="utf-8") != bob_path.read_text(encoding="utf-8")


def test_render_yaml_does_not_include_upstream_provider_url(config_path, users_path):
    config = load_config(config_path)
    registry = load_users(users_path)

    output = render_user_yaml(config, registry.users["alice"])

    assert "https://provider.example/subscription" not in output
    assert VALID_PROVIDER_TOKEN in output


def test_render_rules_and_fallback_order(config_path, users_path):
    config = load_config(config_path)
    registry = load_users(users_path)

    parsed = yaml.safe_load(render_user_yaml(config, registry.users["alice"]))

    groups = {group["name"]: group for group in parsed["proxy-groups"]}
    assert groups["PRIVATE"]["use"] == ["private"]
    assert groups["PROVIDER-AUTO"]["type"] == "url-test"
    assert groups["PROVIDER-AUTO"]["use"] == ["provider"]
    assert groups["PROVIDER-AUTO"]["interval"] == 15
    assert groups["PROVIDER-AUTO"]["timeout"] == 3000
    assert groups["PROVIDER-AUTO"]["max-failed-times"] == 2
    assert groups["PROVIDER-AUTO"]["tolerance"] == 50
    assert groups["PROVIDER-AUTO"]["lazy"] is True
    assert groups["AUTO"]["type"] == "fallback"
    assert groups["AUTO"]["proxies"] == ["PRIVATE", "PROVIDER-AUTO"]
    assert groups["AUTO"]["timeout"] == 3000
    assert groups["AUTO"]["max-failed-times"] == 2
    assert groups["AUTO"]["lazy"] is True
    assert "AUTO-DIRECT" not in groups
    assert groups["PROXY"]["proxies"] == [
        "AUTO",
        "PRIVATE",
        "PROVIDER-AUTO",
        "DIRECT",
    ]
    assert groups["BASE"]["type"] == "select"
    assert groups["BASE"]["proxies"] == ["DIRECT", "PROXY"]

    assert "exclude-filter" not in parsed["proxy-providers"]["private"]
    assert parsed["proxy-providers"]["provider"]["exclude-filter"] == (
        "(?i)(?:Киев|Москва)"
    )

    for provider in parsed["proxy-providers"].values():
        assert provider["health-check"]["timeout"] == 3000
        assert provider["health-check"]["lazy"] is True

    rules = parsed["rules"]
    assert rules[-1] == "MATCH,BASE"
    assert all(",DIRECT" in rule for rule in rules[:-1])


def test_render_users_writes_to_user_token_filenames(config_path, users_path, tmp_path):
    config = load_config(config_path, output_dir=tmp_path / "public")
    registry = load_users(users_path)

    rendered = render_users(config, registry)

    assert [item.name for item in rendered] == ["alice", "bob"]
    assert rendered[0].yaml_path == tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.yaml"
    assert rendered[1].yaml_path == tmp_path / "public/s" / f"{VALID_BOB_TOKEN}.yaml"


def _fixture(name):
    with open(f"tests/fixtures/{name}", encoding="utf-8") as handle:
        return handle.read()
