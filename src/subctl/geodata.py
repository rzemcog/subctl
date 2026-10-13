"""Merge V2Ray geodata sources and derive the compact RU routing rule list."""

from __future__ import annotations

import argparse
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from .routing import DEFAULT_RU_DIRECT_RULES, MANUAL_RU_DIRECT_RULES


ProtoValue: TypeAlias = int | bytes


@dataclass(frozen=True)
class ProtoField:
    number: int
    wire_type: int
    value: ProtoValue


def merge_geosite_dat(base: bytes, supplement: bytes) -> bytes:
    """Union GeoSite records by code and domain, preserving protobuf fields."""

    return _merge_lists(base, supplement, kind="geosite")


def merge_geoip_dat(base: bytes, supplement: bytes) -> bytes:
    """Union GeoIP records, deduplicate and collapse each code's CIDR ranges."""

    return _merge_lists(base, supplement, kind="geoip")


def derive_ru_direct_rules(ru_geosite: bytes, merged_geosite: bytes) -> tuple[str, ...]:
    """Return a compact deterministic selector set covering RU geosite data.

    Requested GEOSITE selectors and suffix rules seed coverage, but any rule
    wholly covered by another selected matcher is omitted. Additional GEOSITE
    selectors are chosen only when needed for source domains. Every chosen tag
    must exist in the merged output database.
    """

    source = _read_geosite(ru_geosite)
    target = _read_geosite(merged_geosite)
    fixed_codes = tuple(
        rule.split(",")[1].upper()
        for rule in MANUAL_RU_DIRECT_RULES
        if rule.upper().startswith("GEOSITE,")
    )
    fixed_code_set = set(fixed_codes)
    missing = sorted(fixed_code_set - target.keys())
    if missing:
        raise ValueError("merged geosite is missing required tags: " + ", ".join(missing))
    minimized_fixed_codes = _minimize_geosite_codes(fixed_codes, target)
    fixed_geosite_matchers = set().union(
        *(target[code] for code in minimized_fixed_codes)
    )

    all_source_patterns = set().union(*source.values()) if source else set()
    other_rules = [
        rule
        for rule in MANUAL_RU_DIRECT_RULES
        if not rule.upper().startswith("GEOSITE,")
        and not _domain_suffix_rule_is_covered(rule, fixed_geosite_matchers)
    ]
    matchers = set(fixed_geosite_matchers)
    for rule in MANUAL_RU_DIRECT_RULES:
        parts = rule.split(",")
        if parts[0].upper() == "DOMAIN-SUFFIX":
            matchers.add((2, _normalize_domain(parts[1])))

    selected = list(minimized_fixed_codes)
    uncovered = {
        pattern
        for pattern in all_source_patterns
        if not _pattern_is_covered(pattern, matchers)
    }

    while uncovered:
        best_code: str | None = None
        best_gain: set[tuple[int, str]] = set()
        for code, patterns in source.items():
            if code in selected:
                continue
            candidate_patterns = target.get(code)
            if candidate_patterns is None:
                raise ValueError(f"merged geosite is missing source tag: {code}")
            gain = {
                item
                for item in uncovered
                if _pattern_is_covered(item, candidate_patterns)
            }
            if len(gain) > len(best_gain) or (
                len(gain) == len(best_gain) and gain and (best_code is None or code < best_code)
            ):
                best_code, best_gain = code, gain
        if best_code is None or not best_gain:
            raise ValueError(f"could not cover {len(uncovered)} RU geosite records")
        selected.append(best_code)
        matchers.update(target[best_code])
        uncovered = {
            pattern
            for pattern in all_source_patterns
            if not _pattern_is_covered(pattern, matchers)
        }

    # Remove selectors made redundant by suffix rules or the other selected
    # tags. Iterate to a stable result because removing one tag may expose
    # another redundant selector.
    supplemental = [code for code in selected if code not in fixed_code_set]
    changed = True
    while changed:
        changed = False
        for code in tuple(supplemental):
            other_matchers = set().union(
                *(target[other] for other in minimized_fixed_codes),
                *(target[item] for item in supplemental if item != code),
            )
            for rule in other_rules:
                parts = rule.split(",")
                if parts[0].upper() == "DOMAIN-SUFFIX":
                    other_matchers.add((2, _normalize_domain(parts[1])))
            if all(
                _pattern_is_covered(pattern, other_matchers)
                for pattern in source[code]
            ):
                supplemental.remove(code)
                changed = True
                break

    final_geosite_matchers = set().union(
        *(target[code] for code in minimized_fixed_codes),
        *(target[code] for code in supplemental),
    )
    minimized_other_rules = [
        rule
        for rule in other_rules
        if not _domain_suffix_rule_is_covered(rule, final_geosite_matchers)
    ]
    geosite_rules = [
        f"GEOSITE,{code},DIRECT-NODES" for code in minimized_fixed_codes
    ]
    return tuple(
        [
            *geosite_rules,
            *(f"GEOSITE,{code},DIRECT-NODES" for code in supplemental),
            *minimized_other_rules,
        ]
    )


def _minimize_geosite_codes(
    codes: tuple[str, ...], tags: dict[str, set[tuple[int, str]]]
) -> tuple[str, ...]:
    selected = list(codes)
    changed = True
    while changed:
        changed = False
        for code in tuple(selected):
            other_matchers = set().union(
                *(tags[other] for other in selected if other != code)
            )
            if all(
                _pattern_is_covered(pattern, other_matchers)
                for pattern in tags[code]
            ):
                selected.remove(code)
                changed = True
                break
    return tuple(selected)


def _domain_suffix_rule_is_covered(
    rule: str, geosite_matchers: set[tuple[int, str]]
) -> bool:
    parts = rule.split(",")
    if len(parts) < 2 or parts[0].upper() != "DOMAIN-SUFFIX":
        return False
    suffix = _normalize_domain(parts[1].strip("."))
    return any(
        matcher_type == 2
        and (suffix == matcher_domain or suffix.endswith("." + matcher_domain))
        for matcher_type, matcher_domain in geosite_matchers
    )


def build_ru_geodata(
    *,
    meta_geoip_path: Path,
    ru_geoip_path: Path,
    meta_geosite_path: Path,
    ru_geosite_path: Path,
    output_dir: Path,
) -> tuple[str, ...]:
    """Create merged DAT files and the compact direct-rules text artifact."""

    output_dir.mkdir(parents=True, exist_ok=True)
    merged_geoip = merge_geoip_dat(meta_geoip_path.read_bytes(), ru_geoip_path.read_bytes())
    merged_geosite = merge_geosite_dat(
        meta_geosite_path.read_bytes(), ru_geosite_path.read_bytes()
    )
    rules = derive_ru_direct_rules(ru_geosite_path.read_bytes(), merged_geosite)
    if rules != DEFAULT_RU_DIRECT_RULES:
        raise ValueError(
            "RU geodata now requires a routing rule update; generated rules differ "
            "from subctl defaults"
        )

    (output_dir / "geoip.dat").write_bytes(merged_geoip)
    (output_dir / "geosite.dat").write_bytes(merged_geosite)
    (output_dir / "direct-rules.txt").write_text("\n".join(rules) + "\n", encoding="utf-8")
    return rules


def _merge_lists(base: bytes, supplement: bytes, *, kind: str) -> bytes:
    if kind not in {"geoip", "geosite"}:
        raise ValueError(f"unsupported geodata kind: {kind}")
    code_field = 1
    content_field = 2
    hash_field = 4 if kind == "geoip" else 3
    merged: dict[str, list[ProtoField]] = {}
    order: list[str] = []
    outer_fields: list[ProtoField] = []

    for raw, is_base in ((base, True), (supplement, False)):
        for field in _parse_fields(raw):
            if field.number != 1 or field.wire_type != 2:
                if is_base:
                    outer_fields.append(field)
                continue
            assert isinstance(field.value, bytes)
            record_fields = _parse_fields(field.value)
            code = _record_code(record_fields, code_field).upper()
            if code not in merged:
                merged[code] = record_fields
                order.append(code)
            else:
                merged[code] = _merge_record_fields(
                    merged[code],
                    record_fields,
                    content_field=content_field,
                    hash_field=hash_field,
                    kind=kind,
                )

    entries = []
    for code in order:
        fields = merged[code]
        # A tag repeated twice within the same input also passes through the
        # merge path above. Normalize base-only records here as well.
        fields = _normalize_content_fields(
            fields,
            content_field=content_field,
            hash_field=hash_field,
            kind=kind,
        )
        body = b"".join(_encode_field(item) for item in fields)
        entries.append(_encode_field(ProtoField(1, 2, body)))

    return b"".join(_encode_field(item) for item in outer_fields) + b"".join(entries)


def _merge_record_fields(
    base: list[ProtoField],
    incoming: list[ProtoField],
    *,
    content_field: int,
    hash_field: int,
    kind: str,
) -> list[ProtoField]:
    base_meta = _metadata_key(base, content_field, hash_field)
    incoming_meta = _metadata_key(incoming, content_field, hash_field)
    if base_meta != incoming_meta:
        raise ValueError("geodata entries with the same code have conflicting metadata")
    combined = [*base, *(field for field in incoming if field.number == content_field)]
    return _normalize_content_fields(
        combined,
        content_field=content_field,
        hash_field=hash_field,
        kind=kind,
    )


def _normalize_content_fields(
    fields: list[ProtoField],
    *,
    content_field: int,
    hash_field: int,
    kind: str,
) -> list[ProtoField]:
    metadata = [
        field
        for field in fields
        if field.number not in {content_field, hash_field}
    ]
    content = [
        field
        for field in fields
        if field.number == content_field and field.wire_type == 2
    ]
    if kind == "geosite":
        deduplicated: dict[tuple, ProtoField] = {}
        for field in content:
            assert isinstance(field.value, bytes)
            nested = _parse_fields(field.value)
            deduplicated.setdefault(_semantic_key(nested), field)
        normalized_content = [deduplicated[key] for key in sorted(deduplicated)]
    else:
        networks: dict[ipaddress._BaseNetwork, ProtoField] = {}
        for field in content:
            assert isinstance(field.value, bytes)
            cidr_fields = _parse_fields(field.value)
            network = _read_cidr(cidr_fields)
            if any(item.number not in {1, 2} for item in cidr_fields):
                raise ValueError("cannot normalize a CIDR record with unknown fields")
            networks.setdefault(network, field)
        collapsed = [
            network
            for version in (4, 6)
            for network in ipaddress.collapse_addresses(
                item for item in networks if item.version == version
            )
        ]
        collapsed.sort(key=lambda network: (network.version, int(network.network_address), network.prefixlen))
        normalized_content = [
            ProtoField(content_field, 2, _encode_cidr(network)) for network in collapsed
        ]

    insertion = next(
        (index for index, field in enumerate(fields) if field.number == content_field),
        len(metadata),
    )
    # resource_hash describes the original content and must not survive a merge.
    result = [field for field in metadata if field.number != hash_field]
    insertion = min(insertion, len(result))
    return [*result[:insertion], *normalized_content, *result[insertion:]]


def _metadata_key(fields: list[ProtoField], content_field: int, hash_field: int) -> tuple:
    return _semantic_key(
        [
            field
            for field in fields
            if field.number not in {1, content_field, hash_field}
        ]
    )


def _semantic_key(fields: list[ProtoField]) -> tuple:
    return tuple(
        sorted(
            (
                field.number,
                field.wire_type,
                field.value,
            )
            for field in fields
        )
    )


def _read_geosite(data: bytes) -> dict[str, set[tuple[int, str]]]:
    result: dict[str, set[tuple[int, str]]] = {}
    for field in _parse_fields(data):
        if field.number != 1 or field.wire_type != 2:
            continue
        assert isinstance(field.value, bytes)
        fields = _parse_fields(field.value)
        code = _record_code(fields, 1).upper()
        records = result.setdefault(code, set())
        for domain in fields:
            if domain.number != 2 or domain.wire_type != 2:
                continue
            assert isinstance(domain.value, bytes)
            domain_fields = _parse_fields(domain.value)
            domain_type = _first_varint(domain_fields, 1, default=0)
            domain_value = _first_bytes(domain_fields, 2).decode("utf-8")
            records.add((domain_type, _normalize_domain(domain_value)))
    return result


def _record_code(fields: list[ProtoField], number: int) -> str:
    value = _first_bytes(fields, number)
    try:
        code = value.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("geodata record code must be ASCII") from exc
    if not code:
        raise ValueError("geodata record is missing its code")
    return code


def _first_bytes(fields: list[ProtoField], number: int) -> bytes:
    for field in fields:
        if field.number == number and field.wire_type == 2 and isinstance(field.value, bytes):
            return field.value
    return b""


def _first_varint(fields: list[ProtoField], number: int, *, default: int) -> int:
    for field in fields:
        if field.number == number and field.wire_type == 0 and isinstance(field.value, int):
            return field.value
    return default


def _read_cidr(fields: list[ProtoField]) -> ipaddress._BaseNetwork:
    packed = _first_bytes(fields, 1)
    if len(packed) not in {4, 16}:
        raise ValueError("GeoIP CIDR must contain a 4-byte or 16-byte address")
    prefix = _first_varint(fields, 2, default=-1)
    try:
        return ipaddress.ip_network((packed, prefix), strict=False)
    except ValueError as exc:
        raise ValueError("GeoIP CIDR contains an invalid address or prefix") from exc


def _encode_cidr(network: ipaddress._BaseNetwork) -> bytes:
    return b"".join(
        (
            _encode_field(ProtoField(1, 2, network.network_address.packed)),
            _encode_field(ProtoField(2, 0, network.prefixlen)),
        )
    )


def _normalize_domain(value: str) -> str:
    value = value.rstrip(".").lower()
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def _pattern_is_covered(pattern: tuple[int, str], matchers: set[tuple[int, str]]) -> bool:
    pattern_type, value = pattern
    if pattern_type in {2, 3}:
        for matcher_type, matcher_value in matchers:
            if matcher_type == 2 and (
                value == matcher_value or value.endswith("." + matcher_value)
            ):
                return True
            if pattern_type == 3 and matcher_type == 3 and value == matcher_value:
                return True
    return pattern in matchers


def _parse_fields(data: bytes) -> list[ProtoField]:
    fields: list[ProtoField] = []
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("invalid protobuf field number 0")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated protobuf fixed64 field")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf length-delimited field")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated protobuf fixed32 field")
            value, offset = data[offset:end], end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.append(ProtoField(number, wire_type, value))
    return fields


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, offset
        shift += 7
    raise ValueError("invalid or truncated protobuf varint")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varints must be non-negative")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _encode_field(field: ProtoField) -> bytes:
    tag = (field.number << 3) | field.wire_type
    prefix = _encode_varint(tag)
    if field.wire_type == 0:
        assert isinstance(field.value, int)
        return prefix + _encode_varint(field.value)
    assert isinstance(field.value, bytes)
    if field.wire_type == 2:
        return prefix + _encode_varint(len(field.value)) + field.value
    return prefix + field.value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-geoip", type=Path, required=True)
    parser.add_argument("--ru-geoip", type=Path, required=True)
    parser.add_argument("--meta-geosite", type=Path, required=True)
    parser.add_argument("--ru-geosite", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rules = build_ru_geodata(
        meta_geoip_path=args.meta_geoip,
        ru_geoip_path=args.ru_geoip,
        meta_geosite_path=args.meta_geosite,
        ru_geosite_path=args.ru_geosite,
        output_dir=args.output_dir,
    )
    print(f"merged geodata written to {args.output_dir}")
    print(f"RU routing rules: {len(rules)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
