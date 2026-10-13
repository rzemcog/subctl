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

# CATEGORY-RU currently subsumes the requested VK, MAILRU, .ru, .рф, and
# music.yandex.com matches. Keep those inputs above for coverage analysis, but
# emit only the minimal effective selector set for the current merged data.
DEFAULT_RU_DIRECT_RULES = (
    "GEOSITE,CATEGORY-RU,DIRECT-NODES",
    "GEOSITE,2GIS,DIRECT-NODES",
    "GEOIP,RU,DIRECT-NODES",
)


def merge_direct_rules(configured: tuple[str, ...]) -> tuple[str, ...]:
    """Merge normalized RU defaults with operator rules without duplicates.

    An operator rule that repeats a built-in RU match is normalized to the
    built-in DIRECT-NODES action. Requested RU matches already covered by the
    minimized built-ins are omitted; other operator rules retain their action.
    """

    defaults = tuple(_normalize_rule(rule) for rule in DEFAULT_RU_DIRECT_RULES)
    default_matches = {_match_key(rule) for rule in defaults}
    subsumed_manual_matches = {
        _match_key(rule)
        for rule in MANUAL_RU_DIRECT_RULES
        if _match_key(rule) not in default_matches
    }
    result = list(defaults)
    seen = {rule.casefold() for rule in defaults}

    for rule in configured:
        normalized = _normalize_rule(rule)
        match = _match_key(normalized)
        if match in default_matches or match in subsumed_manual_matches:
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
