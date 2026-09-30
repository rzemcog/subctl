import stat

import yaml

from conftest import SEED_URI, VALID_ALICE_TOKEN, VALID_BOB_TOKEN, VALID_PROVIDER_TOKEN
from subctl.config import load_config
from subctl.registry import load_users
from subctl.render import build_mihomo_profile, render_user_yaml, render_users


def test_render_user_yaml_matches_golden_snapshot(profile_config, users_path):
    config = profile_config
    registry = load_users(users_path)

    actual = render_user_yaml(config, registry.users["alice"])
    expected = _fixture("alice_mihomo.yaml")

    lines = actual.splitlines()
    assert lines[0].startswith("# subctl-seed-generated-at: ")
    assert lines[1].startswith("# subctl-seed-source-updated-at: ")
    assert lines[2] == "# subctl-seed-node-count: 1"
    assert "\n".join(lines[3:]) + "\n" == expected


def test_render_command_writes_parseable_yaml_for_multiple_users(
    run_subctl, cli_paths, tmp_path
):
    cache = tmp_path / "state" / "cache" / "provider.decoded"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(SEED_URI + "\n", encoding="utf-8")
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


def test_render_yaml_does_not_include_upstream_provider_url(profile_config, users_path):
    config = profile_config
    registry = load_users(users_path)

    output = render_user_yaml(config, registry.users["alice"])

    assert "https://provider.example/subscription" not in output
    assert VALID_PROVIDER_TOKEN in output


def test_render_rules_and_fallback_order(profile_config, users_path):
    config = profile_config
    registry = load_users(users_path)

    parsed = yaml.safe_load(render_user_yaml(config, registry.users["alice"]))

    groups = {group["name"]: group for group in parsed["proxy-groups"]}
    assert groups["PRIVATE"]["use"] == ["private"]
    assert groups["PRIVATE"]["empty-fallback"] == "REJECT"
    assert groups["PROVIDER-AUTO"]["type"] == "fallback"
    assert groups["PROVIDER-AUTO"]["proxies"] == [
        "PROVIDER-AUTO-LIVE",
        "PROVIDER-AUTO-SEED",
    ]
    assert groups["PROVIDER-AUTO"]["empty-fallback"] == "REJECT"
    assert groups["PROVIDER-AUTO-LIVE"]["type"] == "url-test"
    assert groups["PROVIDER-AUTO-LIVE"]["use"] == ["provider"]
    assert groups["PROVIDER-AUTO-LIVE"].get("hidden", False) is False
    assert groups["PROVIDER-AUTO-LIVE"]["interval"] == 15
    assert groups["PROVIDER-AUTO-LIVE"]["timeout"] == 3000
    assert groups["PROVIDER-AUTO-LIVE"]["max-failed-times"] == 2
    assert groups["PROVIDER-AUTO-LIVE"]["tolerance"] == 50
    assert groups["PROVIDER-AUTO-LIVE"]["empty-fallback"] == "REJECT"
    assert groups["PROVIDER-AUTO-LIVE"]["lazy"] is True
    assert groups["PROVIDER-AUTO-SEED"]["type"] == "url-test"
    assert groups["PROVIDER-AUTO-SEED"]["use"] == ["provider-seed"]
    assert groups["PROVIDER-AUTO-SEED"].get("hidden", False) is False
    assert groups["PROVIDER-AUTO-SEED"]["tolerance"] == 50
    assert groups["PROVIDER-AUTO-SEED"]["empty-fallback"] == "REJECT"
    assert groups["FETCH-PRIVATE"]["proxies"] == [
        "PROVIDER-AUTO-SEED",
        "DIRECT",
    ]
    assert "default-selected" not in groups["FETCH-PRIVATE"]
    assert "empty-fallback" not in groups["FETCH-PRIVATE"]
    assert groups["FETCH-AUTO-PROVIDER"]["proxies"] == [
        "PRIVATE",
        "PROVIDER-AUTO-SEED",
        "DIRECT",
    ]
    assert "default-selected" not in groups["FETCH-AUTO-PROVIDER"]
    assert "empty-fallback" not in groups["FETCH-AUTO-PROVIDER"]
    assert groups["FETCH-PRIVATE"]["hidden"] is True
    assert groups["FETCH-AUTO-PROVIDER"]["hidden"] is True
    assert groups["AUTO"]["type"] == "fallback"
    assert groups["AUTO"]["proxies"] == ["PRIVATE", "PROVIDER-AUTO"]
    assert groups["AUTO"]["empty-fallback"] == "REJECT"
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
    assert groups["GLOBAL"]["proxies"] == [
        name for name in groups if name != "GLOBAL"
    ]
    assert "fixture-seed" not in groups["GLOBAL"]["proxies"]

    assert "exclude-filter" not in parsed["proxy-providers"]["private"]
    assert parsed["proxy-providers"]["provider"]["exclude-filter"] == (
        "(?i)(?:Киев|Москва)"
    )
    assert parsed["proxy-providers"]["provider"]["proxy"] == "FETCH-AUTO-PROVIDER"
    assert "override" not in parsed["proxy-providers"]["provider"]
    assert parsed["proxy-providers"]["private"]["proxy"] == "FETCH-PRIVATE"
    assert parsed["proxy-providers"]["provider"]["header"]["User-Agent"] == [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ]
    seed_node = parsed["proxy-providers"]["provider-seed"]["payload"][0]
    assert seed_node["name"] == "fixture-seed"
    assert seed_node["type"] == "vless"
    assert seed_node["server"] == "seed.example.net"
    assert "proxies" not in parsed
    assert seed_node["reality-opts"] == {
        "public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "short-id": "0123456789abcdef",
    }

    for provider in parsed["proxy-providers"].values():
        assert provider["health-check"]["timeout"] == 3000
        assert provider["health-check"]["lazy"] is True

    rules = parsed["rules"]
    assert rules[-1] == "MATCH,BASE"
    assert all(",DIRECT" in rule for rule in rules[:-1])


def test_render_includes_protected_mihomo_hosts_mapping(
    config_data, write_yaml, users_path, provider_state_dir
):
    mapping = {"feed.fixture.test": "192.0.2.71"}
    config_data["mihomo"] = {"hosts": mapping}
    config = load_config(
        write_yaml("mihomo-host-map.yaml", config_data), state_dir=provider_state_dir
    )
    user = load_users(users_path).users["alice"]

    assert build_mihomo_profile(config, user)["hosts"] == mapping


def test_render_provider_download_proxy_is_configurable(
    config_data, write_yaml, users_path, provider_state_dir
):
    config_data["render"]["provider_download_proxy"] = "PROXY"
    config_path = write_yaml("provider-proxy-config.yaml", config_data)
    config = load_config(config_path, state_dir=provider_state_dir)
    registry = load_users(users_path)

    parsed = yaml.safe_load(render_user_yaml(config, registry.users["alice"]))

    assert parsed["proxy-providers"]["provider"]["proxy"] == "FETCH-AUTO-PROVIDER"


def test_render_provider_download_proxy_can_be_overridden_by_settings(
    config_path, users_path, provider_state_dir
):
    config = load_config(
        config_path,
        state_dir=provider_state_dir,
        settings_override={"render": {"provider_download_proxy": "PRIVATE"}},
    )
    registry = load_users(users_path)

    parsed = yaml.safe_load(render_user_yaml(config, registry.users["alice"]))

    assert parsed["proxy-providers"]["provider"]["proxy"] == "FETCH-AUTO-PROVIDER"


def test_seed_snapshot_updates_on_render_without_changing_live_provider_lifecycle(
    profile_config, users_path
):
    user = load_users(users_path).users["alice"]
    before = build_mihomo_profile(profile_config, user)
    cache = profile_config.state_dir / "cache" / "provider.decoded"
    cache.write_text(
        SEED_URI.replace("seed.example.net", "seed-next.example.net").replace(
            "fixture-seed", "fixture-next"
        )
        + "\n",
        encoding="utf-8",
    )

    after = build_mihomo_profile(profile_config, user)

    assert before["proxy-providers"]["provider-seed"]["payload"] != after[
        "proxy-providers"
    ]["provider-seed"]["payload"]
    assert after["proxy-providers"]["provider"]["path"] == "./providers/provider.yaml"
    assert after["proxy-providers"]["provider"]["interval"] == 900


def test_render_users_writes_to_user_token_filenames(
    config_path, users_path, tmp_path, provider_state_dir
):
    config = load_config(
        config_path, state_dir=provider_state_dir, output_dir=tmp_path / "public"
    )
    registry = load_users(users_path)

    rendered = render_users(config, registry)

    assert [item.name for item in rendered] == ["alice", "bob"]
    assert rendered[0].yaml_path == tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.yaml"
    assert rendered[1].yaml_path == tmp_path / "public/s" / f"{VALID_BOB_TOKEN}.yaml"


def _fixture(name):
    with open(f"tests/fixtures/{name}", encoding="utf-8") as handle:
        return handle.read()
