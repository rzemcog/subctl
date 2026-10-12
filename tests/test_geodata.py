import ipaddress

from subctl.geodata import (
    ProtoField,
    _encode_field,
    _parse_fields,
    _read_cidr,
    _read_geosite,
    build_ru_geodata,
    derive_ru_direct_rules,
    merge_geoip_dat,
    merge_geosite_dat,
)
from subctl.routing import DEFAULT_RU_DIRECT_RULES


def _message(fields):
    return b"".join(_encode_field(field) for field in fields)


def _geosite_entry(code, domains):
    fields = [ProtoField(1, 2, code.encode())]
    fields.extend(
        ProtoField(
            2,
            2,
            _message(
                [
                    ProtoField(1, 0, domain_type),
                    ProtoField(2, 2, domain.encode()),
                ]
            ),
        )
        for domain_type, domain in domains
    )
    return _encode_field(ProtoField(1, 2, _message(fields)))


def _site_list(entries):
    return b"".join(entries)


def _cidr(network):
    parsed = ipaddress.ip_network(network)
    return _message(
        [
            ProtoField(1, 2, parsed.network_address.packed),
            ProtoField(2, 0, parsed.prefixlen),
        ]
    )


def _geoip_entry(code, networks):
    fields = [ProtoField(1, 2, code.encode())]
    fields.extend(ProtoField(2, 2, _cidr(network)) for network in networks)
    return _encode_field(ProtoField(1, 2, _message(fields)))


def _ip_list(entries):
    return b"".join(entries)


def test_geosite_merge_deduplicates_domains_and_keeps_new_tags():
    base = _site_list(
        [
            _geosite_entry("CATEGORY-RU", [(2, "service.ru"), (3, "example.com")]),
            _geosite_entry("VK", [(2, "vk.com")]),
        ]
    )
    supplement = _site_list(
        [
            _geosite_entry("CATEGORY-RU", [(2, "service.ru"), (2, "new.ru")]),
            _geosite_entry("2GIS", [(2, "2gis.com")]),
        ]
    )

    merged = merge_geosite_dat(base, supplement)
    tags = _read_geosite(merged)

    assert tags["CATEGORY-RU"] == {
        (2, "service.ru"),
        (3, "example.com"),
        (2, "new.ru"),
    }
    assert tags["2GIS"] == {(2, "2gis.com")}


def test_ru_geosite_rules_are_minimized_against_manual_rules():
    site = _site_list(
        [
            _geosite_entry("VK", [(2, "vk.com")]),
            _geosite_entry("MAILRU", [(2, "mail.ru")]),
            _geosite_entry("MAILRU-GROUP", [(2, "mail.ru")]),
            _geosite_entry("CATEGORY-RU", [(2, "service.ru")]),
            _geosite_entry("RU-ONLY-ALIAS", [(2, "service.ru")]),
            _geosite_entry("2GIS", [(2, "map.2gis.ru"), (2, "2gis.com")]),
        ]
    )

    rules = derive_ru_direct_rules(site, site)

    assert rules == DEFAULT_RU_DIRECT_RULES
    assert len(rules) == len(set(rules))


def test_geoip_merge_deduplicates_and_collapses_networks():
    base = _ip_list(
        [
            _geoip_entry("RU", ["192.0.2.0/25", "2001:db8::/64"]),
            _geoip_entry("PRIVATE", ["10.0.0.0/8"]),
        ]
    )
    supplement = _ip_list(
        [
            _geoip_entry("RU", ["192.0.2.0/25", "192.0.2.128/25"]),
            _geoip_entry("PRIVATE", ["10.0.0.0/8"]),
        ]
    )

    merged = merge_geoip_dat(base, supplement)
    records = {}
    for entry in _parse_fields(merged):
        assert entry.number == 1 and entry.wire_type == 2
        fields = _parse_fields(entry.value)
        code = next(item.value.decode() for item in fields if item.number == 1)
        cidrs = [
            _read_cidr(_parse_fields(item.value))
            for item in fields
            if item.number == 2
        ]
        records[code] = cidrs

    assert records["RU"] == [
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("2001:db8::/64"),
    ]
    assert records["PRIVATE"] == [ipaddress.ip_network("10.0.0.0/8")]


def test_build_ru_geodata_writes_one_minimized_rule_list(tmp_path):
    meta_geoip = tmp_path / "meta-geoip.dat"
    ru_geoip = tmp_path / "ru-geoip.dat"
    meta_geosite = tmp_path / "meta-geosite.dat"
    ru_geosite = tmp_path / "ru-geosite.dat"
    output = tmp_path / "out"
    meta_geoip.write_bytes(_ip_list([_geoip_entry("RU", ["192.0.2.0/25"])]))
    ru_geoip.write_bytes(_ip_list([_geoip_entry("RU", ["192.0.2.128/25"])]))
    meta_geosite.write_bytes(
        _site_list(
            [
                _geosite_entry("VK", [(2, "vk.com")]),
                _geosite_entry("MAILRU", [(2, "mail.ru")]),
                _geosite_entry("CATEGORY-RU", [(2, "service.ru")]),
                _geosite_entry("2GIS", [(2, "2gis.com")]),
            ]
        )
    )
    ru_geosite.write_bytes(meta_geosite.read_bytes())

    rules = build_ru_geodata(
        meta_geoip_path=meta_geoip,
        ru_geoip_path=ru_geoip,
        meta_geosite_path=meta_geosite,
        ru_geosite_path=ru_geosite,
        output_dir=output,
    )

    assert rules == DEFAULT_RU_DIRECT_RULES
    assert (output / "geoip.dat").is_file()
    assert (output / "geosite.dat").is_file()
    assert (output / "direct-rules.txt").read_text(encoding="utf-8") == (
        "\n".join(DEFAULT_RU_DIRECT_RULES) + "\n"
    )
