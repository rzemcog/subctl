import re
import stat

import yaml

from conftest import SEED_URI, VALID_ALICE_TOKEN, VALID_BOB_TOKEN, VALID_PROVIDER_TOKEN
from subctl.config import load_config
from subctl.registry import load_users
from subctl.render import build_mihomo_profile, render_user_yaml, render_users
from subctl.routing import DEFAULT_RU_DIRECT_RULES


CONFIGURED_DIRECT_RULES = [
    "DOMAIN-SUFFIX,example.invalid,DIRECT-NODES",
]


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
    assert groups["PROVIDER-AUTO-SEED"]["exclude-filter"] == "(?i)(?:Preferred|Sample)"
    assert groups["DIRECT-PROXY"]["use"] == ["provider"]
    assert groups["DIRECT-PROXY"]["filter"] == "(?i)(?:Preferred|Sample)"
    assert groups["EXCLUDED-NODES"]["use"] == ["provider"]
    assert groups["EXCLUDED-NODES"]["filter"] == "(?i)(?:Excluded)"
    assert groups["EXCLUDED-NODES"]["exclude-filter"] == "(?i)(?:Preferred|Sample)"
    assert groups["DIRECT-NODES"]["proxies"] == ["DIRECT", "DIRECT-PROXY"]
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
        "DIRECT-PROXY",
        "EXCLUDED-NODES",
    ]
    assert groups["BASE"]["type"] == "select"
    assert groups["BASE"]["proxies"] == ["PROXY", "DIRECT", "DIRECT-PROXY"]
    assert groups["GLOBAL"]["proxies"] == [
        name for name in groups if name != "GLOBAL"
    ]
    assert "fixture-seed" not in groups["GLOBAL"]["proxies"]
    assert {"DIRECT-PROXY", "EXCLUDED-NODES", "DIRECT-NODES"}.issubset(
        groups["GLOBAL"]["proxies"]
    )

    assert "exclude-filter" not in parsed["proxy-providers"]["private"]
    assert "exclude-filter" not in parsed["proxy-providers"]["provider"]
    assert parsed["proxy-providers"]["provider"]["proxy"] == "FETCH-AUTO-PROVIDER"
    assert "override" not in parsed["proxy-providers"]["provider"]
    assert parsed["proxy-providers"]["private"]["proxy"] == "FETCH-PRIVATE"
    assert parsed["proxy-providers"]["provider"]["header"]["User-Agent"] == [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/192.0.2.1 Safari/537.36"
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
    assert rules[9 : 9 + len(DEFAULT_RU_DIRECT_RULES)] == list(DEFAULT_RU_DIRECT_RULES)
    assert rules[9 + len(DEFAULT_RU_DIRECT_RULES) : -1] == CONFIGURED_DIRECT_RULES
    assert len(rules) == len(set(rules))
    assert all(rule.endswith(",DIRECT,no-resolve") for rule in rules[:9])

    group_names = {group["name"] for group in parsed["proxy-groups"]}
    assert len(group_names) == len(parsed["proxy-groups"])
    dependencies = {
        group["name"]: [
            proxy for proxy in group.get("proxies", []) if proxy in group_names
        ]
        for group in parsed["proxy-groups"]
    }
    visiting = set()
    visited = set()

    def visit(name):
        assert name not in visiting, f"proxy group cycle includes {name}"
        if name in visited:
            return
        visiting.add(name)
        for dependency in dependencies[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in dependencies:
        visit(name)


def test_render_configured_node_groups_are_partitioned(profile_config, users_path):
    user = load_users(users_path).users["alice"]
    parsed = build_mihomo_profile(profile_config, user)
    groups = {group["name"]: group for group in parsed["proxy-groups"]}
    direct_filter = re.compile(groups["DIRECT-PROXY"]["filter"])
    other_excluded_filter = re.compile(groups["EXCLUDED-NODES"]["filter"])
    auto_exclude_filter = re.compile(groups["PROVIDER-AUTO-LIVE"]["exclude-filter"])
    excluded_direct_filter = re.compile(groups["EXCLUDED-NODES"]["exclude-filter"])
    names = ["Preferred Premium", "SAMPLE-01", "ExcludedPreferred 2", "Excluded Only", "Ordinary"]

    direct_proxy = [name for name in names if direct_filter.search(name)]
    excluded = [
        name
        for name in names
        if other_excluded_filter.search(name) and not excluded_direct_filter.search(name)
    ]
    auto_live = [name for name in names if not auto_exclude_filter.search(name)]

    assert direct_proxy == ["Preferred Premium", "SAMPLE-01", "ExcludedPreferred 2"]
    assert excluded == ["Excluded Only"]
    assert auto_live == ["Ordinary"]


def test_provider_seed_auto_group_uses_configured_node_keywords(profile_config, users_path):
    user = load_users(users_path).users["alice"]
    cache = profile_config.state_dir / "cache" / "provider.decoded"
    cache.write_text(
        "\n".join(
            [
                "trojan://pass@direct.example:443#Preferred%20node",
                "trojan://pass@sample.example:443#Sample%20node",
                "trojan://pass@overlap.example:443#ExcludedPreferred%20node",
                "trojan://pass@excluded.example:443#Excluded%20node",
                "trojan://pass@live.example:443#Ordinary%20node",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    profile = build_mihomo_profile(profile_config, user)
    groups = {group["name"]: group for group in profile["proxy-groups"]}
    seed_names = [
        proxy["name"]
        for proxy in profile["proxy-providers"]["provider-seed"]["payload"]
    ]
    seed_auto_names = [
        name
        for name in seed_names
        if not re.search(groups["PROVIDER-AUTO-SEED"]["exclude-filter"], name)
    ]

    assert seed_names == ["Preferred node", "Sample node", "Ordinary node"]
    assert seed_auto_names == ["Ordinary node"]


def test_render_user_defaults_tun_hosts_and_geodata(profile_config, users_path):
    user = load_users(users_path).users["alice"]
    profile = build_mihomo_profile(profile_config, user)

    assert profile["tun"] == {
        "enable": True,
        "stack": "mips",
        "auto-route": True,
        "mtu": 1350,
        "route-exclude-address": [],
    }
    assert profile["hosts"] == {}
    assert profile["geodata-mode"] is False
    assert profile["geo-auto-update"] is True
    assert profile["geo-update-interval"] == 24
    assert profile["geox-url"] == {
        "geoip": "https://geo.example.invalid/geoip.dat",
        "geosite": "https://geo.example.invalid/geosite.dat",
        "mmdb": "https://geo.example.invalid/geoip.mmdb",
        "asn": "https://geo.example.invalid/asn.mmdb",
    }


def test_render_keeps_ru_defaults_without_operator_overrides(
    config_data, write_yaml, users_path, provider_state_dir
):
    config_data["mihomo"] = {
        "hosts": {},
        "geox_urls": {},
        "routing": {"direct_node_name_keywords": [], "direct_rules": []},
    }
    config = load_config(
        write_yaml("generic-mihomo-config.yaml", config_data),
        state_dir=provider_state_dir,
    )
    user = load_users(users_path).users["alice"]

    profile = build_mihomo_profile(config, user)
    groups = {group["name"]: group for group in profile["proxy-groups"]}

    assert profile["geox-url"] == {
        "geoip": "https://github.com/rzemcog/subctl/releases/download/ru-geodata/geoip.dat",
        "geosite": "https://github.com/rzemcog/subctl/releases/download/ru-geodata/geosite.dat",
        "mmdb": "https://github.com/rzemcog/subctl/releases/download/ru-geodata/geoip.metadb",
        "asn": "https://github.com/rzemcog/subctl/releases/download/ru-geodata/GeoLite2-ASN.mmdb",
    }
    assert groups["DIRECT-PROXY"]["filter"] == "a^"
    assert "exclude-filter" not in groups["PROVIDER-AUTO-SEED"]
    assert "exclude-filter" not in groups["EXCLUDED-NODES"]
    assert profile["rules"] == [
        "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
        "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
        "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
        "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
        "IP-CIDR,::1/128,DIRECT,no-resolve",
        "IP-CIDR,fc00::/7,DIRECT,no-resolve",
        "IP-CIDR,fe80::/10,DIRECT,no-resolve",
        *DEFAULT_RU_DIRECT_RULES,
        "MATCH,BASE",
    ]


def test_ru_default_rules_normalize_and_deduplicate_operator_duplicates(
    config_data, write_yaml, users_path, provider_state_dir
):
    config_data["mihomo"]["routing"]["direct_rules"] = [
        "GEOSITE,vk,DIRECT",
        "GEOSITE,mailru,DIRECT",
        "GEOSITE,category-ru,DIRECT",
        "DOMAIN-SUFFIX,.ru,DIRECT",
        "DOMAIN-SUFFIX,.рф,DIRECT",
        "DOMAIN-SUFFIX,music.yandex.com,DIRECT",
        "GEOIP,ru,DIRECT",
        "DOMAIN-SUFFIX,.Example.invalid,DIRECT-NODES",
        "DOMAIN-SUFFIX,example.invalid,DIRECT-NODES",
    ]
    config = load_config(
        write_yaml("duplicate-ru-rules.yaml", config_data),
        state_dir=provider_state_dir,
    )
    user = load_users(users_path).users["alice"]

    profile = build_mihomo_profile(config, user)
    rules = profile["rules"]
    routed = [rule for rule in rules if rule.endswith(",DIRECT-NODES")]

    assert rules.count("GEOSITE,CATEGORY-RU,DIRECT-NODES") == 1
    assert rules.count("GEOSITE,2GIS,DIRECT-NODES") == 1
    assert rules.count("GEOIP,RU,DIRECT-NODES") == 1
    assert "DOMAIN-SUFFIX,ru,DIRECT-NODES" not in rules
    assert "DOMAIN-SUFFIX,xn--p1ai,DIRECT-NODES" not in rules
    assert "DOMAIN-SUFFIX,music.yandex.com,DIRECT-NODES" not in rules
    assert "GEOSITE,VK,DIRECT-NODES" not in rules
    assert "GEOSITE,MAILRU,DIRECT-NODES" not in rules
    assert rules.count("DOMAIN-SUFFIX,example.invalid,DIRECT-NODES") == 1
    assert len(routed) == len(set(routed))


def test_render_includes_protected_mihomo_hosts_mapping(
    config_data, write_yaml, users_path, provider_state_dir
):
    config_data["public"]["base_url"] = "https://feed.fixture.test"
    mapping = {"feed.fixture.test": "192.0.2.10", "other.fixture.test": "198.51.100.20"}
    config_data["mihomo"] = {"hosts": mapping}
    config = load_config(
        write_yaml("mihomo-host-map.yaml", config_data), state_dir=provider_state_dir
    )
    user = load_users(users_path).users["alice"]

    profile = build_mihomo_profile(config, user)

    assert profile["hosts"] == mapping
    assert profile["tun"]["route-exclude-address"] == ["192.0.2.10/32"]


def test_render_excludes_ip_literal_public_endpoint_from_tun(
    config_data, write_yaml, users_path, provider_state_dir
):
    config_data["public"]["base_url"] = "https://[2001:db8::10]"
    config_data["mihomo"] = {"hosts": {"other.fixture.test": "192.0.2.12"}}
    config = load_config(
        write_yaml("mihomo-ip-endpoint.yaml", config_data), state_dir=provider_state_dir
    )
    user = load_users(users_path).users["alice"]

    profile = build_mihomo_profile(config, user)

    assert profile["hosts"] == {"other.fixture.test": "192.0.2.12"}
    assert profile["tun"]["route-exclude-address"] == ["2001:db8::10/128"]


def test_render_legacy_provider_download_proxy_does_not_override_fetch_routes(
    config_data, write_yaml, users_path, provider_state_dir
):
    config_data["render"]["provider_download_proxy"] = "PROXY"
    config_path = write_yaml("provider-proxy-config.yaml", config_data)
    config = load_config(config_path, state_dir=provider_state_dir)
    registry = load_users(users_path)

    parsed = yaml.safe_load(render_user_yaml(config, registry.users["alice"]))

    assert parsed["proxy-providers"]["provider"]["proxy"] == "FETCH-AUTO-PROVIDER"


def test_render_settings_legacy_provider_download_proxy_is_ignored(
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
