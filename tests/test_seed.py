from __future__ import annotations

import pytest

from conftest import SEED_URI
from subctl.errors import ValidationError
from subctl.seed import load_provider_seed


def test_load_provider_seed_converts_reality_vless_and_tracks_snapshot(tmp_path):
    cache = tmp_path / "provider.decoded"
    cache.write_text(SEED_URI + "\n", encoding="utf-8")

    seed = load_provider_seed(cache)

    assert len(seed.proxies) == 1
    assert seed.proxies[0] == {
        "name": "SEED | fixture-seed",
        "type": "vless",
        "server": "seed.example.net",
        "port": 443,
        "uuid": "123e4567-e89b-12d3-a456-426614174000",
        "network": "tcp",
        "encryption": "",
        "flow": "xtls-rprx-vision",
        "tls": True,
        "servername": "seed.example.net",
        "client-fingerprint": "chrome",
        "reality-opts": {
            "public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "short-id": "0123456789abcdef",
        },
    }
    assert seed.generated_at.endswith("+00:00")
    assert seed.source_updated_at is not None
    assert seed.source_updated_at.endswith("+00:00")


def test_load_provider_seed_applies_name_exclusions_and_unique_names(tmp_path):
    cache = tmp_path / "provider.decoded"
    cache.write_text(SEED_URI + "\n" + SEED_URI + "\n", encoding="utf-8")

    seed = load_provider_seed(cache, ("skip-this",))
    assert len(seed.proxies) == 2
    assert [proxy["name"] for proxy in seed.proxies] == [
        "SEED | fixture-seed",
        "SEED | fixture-seed (2)",
    ]

    with pytest.raises(ValidationError, match="no nodes after applying provider exclusions"):
        load_provider_seed(cache, ("fixture-seed",))


def test_load_provider_seed_rejects_unrepresented_scheme_without_uri_leak(tmp_path):
    cache = tmp_path / "provider.decoded"
    secret_uri = "vmess://opaque-secret-payload#secret-node"
    cache.write_text(secret_uri + "\n", encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        load_provider_seed(cache)

    assert secret_uri not in str(exc_info.value)
    assert "opaque-secret-payload" not in str(exc_info.value)


def test_load_provider_seed_converts_basic_trojan_nodes(tmp_path):
    cache = tmp_path / "provider.decoded"
    cache.write_text("trojan://pass@provider.example:443#provider\n", encoding="utf-8")

    seed = load_provider_seed(cache)

    assert seed.proxies == (
        {
            "name": "SEED | provider",
            "type": "trojan",
            "server": "provider.example",
            "port": 443,
            "password": "pass",
            "tls": True,
        },
    )


def test_load_provider_seed_rejects_duplicate_query_keys_without_uri_leak(tmp_path):
    cache = tmp_path / "provider.decoded"
    secret_uri = SEED_URI.replace("&flow=", "&flow=bad&flow=")
    cache.write_text(secret_uri + "\n", encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        load_provider_seed(cache)

    assert secret_uri not in str(exc_info.value)


def test_load_provider_seed_requires_existing_valid_cache(tmp_path):
    with pytest.raises(ValidationError, match="provider cache is missing"):
        load_provider_seed(tmp_path / "missing.decoded")
