from subctl.routing import DEFAULT_RU_DIRECT_RULES, merge_direct_rules


def test_direct_rules_normalize_idn_and_deduplicate_default_matches():
    rules = merge_direct_rules(
        (
            "GEOSITE,vk,DIRECT",
            "GEOSITE,mailru,DIRECT",
            "GEOSITE,category-ru,DIRECT",
            "DOMAIN-SUFFIX,.ru,DIRECT",
            "DOMAIN-SUFFIX,.рф,DIRECT",
            "DOMAIN-SUFFIX,music.yandex.com,DIRECT",
            "GEOIP,ru,DIRECT",
            "DOMAIN-SUFFIX,.Example.invalid,DIRECT-NODES",
            "DOMAIN-SUFFIX,example.invalid,DIRECT-NODES",
        )
    )

    assert DEFAULT_RU_DIRECT_RULES == (
        "GEOSITE,CATEGORY-RU,DIRECT-NODES",
        "GEOSITE,2GIS,DIRECT-NODES",
        "GEOIP,RU,DIRECT-NODES",
    )
    assert rules[: len(DEFAULT_RU_DIRECT_RULES)] == DEFAULT_RU_DIRECT_RULES
    assert rules[len(DEFAULT_RU_DIRECT_RULES) :] == (
        "DOMAIN-SUFFIX,example.invalid,DIRECT-NODES",
    )
    assert len(rules) == len(set(rules))
