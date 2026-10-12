from __future__ import annotations


MANUAL_RU_DIRECT_RULES = (
    "GEOSITE,VK,DIRECT-NODES",
    "GEOSITE,MAILRU,DIRECT-NODES",
    "GEOSITE,CATEGORY-RU,DIRECT-NODES",
    "DOMAIN-SUFFIX,ru,DIRECT-NODES",
    "DOMAIN-SUFFIX,xn--p1ai,DIRECT-NODES",
    "DOMAIN-SUFFIX,music.yandex.com,DIRECT-NODES",
    "GEOIP,RU,DIRECT-NODES",
)

# The RU geosite data is reduced against the seven manual rules above. With the
# current sources, 2GIS is the only additional selector needed to cover every
# source domain record.
DEFAULT_RU_DIRECT_RULES = (
    "GEOSITE,VK,DIRECT-NODES",
    "GEOSITE,MAILRU,DIRECT-NODES",
    "GEOSITE,CATEGORY-RU,DIRECT-NODES",
    "GEOSITE,2GIS,DIRECT-NODES",
    "DOMAIN-SUFFIX,ru,DIRECT-NODES",
    "DOMAIN-SUFFIX,xn--p1ai,DIRECT-NODES",
    "DOMAIN-SUFFIX,music.yandex.com,DIRECT-NODES",
    "GEOIP,RU,DIRECT-NODES",
)


def merge_direct_rules(configured: tuple[str, ...]) -> tuple[str, ...]:
    """Merge normalized RU defaults with operator rules without duplicates.

    An operator rule that repeats a built-in RU match is normalized to the
    built-in DIRECT-NODES action. Other operator rules retain their action.
    """

    defaults = tuple(_normalize_rule(rule) for rule in DEFAULT_RU_DIRECT_RULES)
    default_matches = {_match_key(rule) for rule in defaults}
    result = list(defaults)
    seen = {rule.casefold() for rule in defaults}

    for rule in configured:
        normalized = _normalize_rule(rule)
        if _match_key(normalized) in default_matches:
            continue
        identity = normalized.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        result.append(normalized)

    return tuple(result)


def _normalize_rule(rule: str) -> str:
    parts = [part.strip() for part in rule.split(",")]
    if len(parts) < 3:
        return rule.strip()

    rule_type = parts[0].upper()
    value = parts[1]
    if rule_type in {"GEOSITE", "GEOIP"}:
        value = value.upper()
    elif rule_type == "DOMAIN-SUFFIX":
        suffix = value.strip(".")
        value = suffix.encode("idna").decode("ascii").lower()

    parts[0] = rule_type
    parts[1] = value
    return ",".join(parts)


def _match_key(rule: str) -> tuple[str, str]:
    parts = rule.split(",")
    if len(parts) < 2:
        return (rule.casefold(), "")
    return (parts[0].upper(), parts[1].casefold())
