import base64
import stat
from urllib.parse import unquote, urlsplit

import pytest

from conftest import VALID_ALICE_TOKEN, VALID_BOB_TOKEN, VALID_PROVIDER_TOKEN
from subctl.config import load_config
from subctl.errors import ValidationError
from subctl.registry import load_users
from subctl.render import RenderOptions, render_subscriptions


PERSONAL_FEED = "ss://YWVzLTEyOC1nY206cGFzcw@private.example:443#alice%20node\n"
PROVIDER_FEED = "trojan://pass@provider.example:443#provider-node\n"


def test_raw_render_combines_personal_and_provider_as_base64(
    tmp_path, config_path, users_path
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    registry = load_users(users_path)
    _write_provider_cache(tmp_path)

    summary = render_subscriptions(
        config,
        registry,
        options=RenderOptions(mode="raw-only", user_name="alice"),
        fetch_subscription=lambda url, timeout: PERSONAL_FEED.encode("utf-8"),
    )

    assert summary.rendered == 1
    raw_path = tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw"
    assert stat.S_IMODE(raw_path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(raw_path.stat().st_mode) == 0o644
    decoded = base64.b64decode(raw_path.read_text(encoding="utf-8")).decode("utf-8")
    assert decoded.splitlines() == [
        "ss://YWVzLTEyOC1nY206cGFzcw@private.example:443#PRIVATE%20%7C%20alice%20node",
        "trojan://pass@provider.example:443#PROVIDER%20%7C%20provider-node",
    ]


def test_raw_render_prefixes_and_encodes_unescaped_fragment_spaces(
    tmp_path, config_path, users_path
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    registry = load_users(users_path)
    _write_provider_cache(tmp_path)

    render_subscriptions(
        config,
        registry,
        options=RenderOptions(mode="raw-only", user_name="alice"),
        fetch_subscription=lambda url, timeout: (
            "ss://YWVzLTEyOC1nY206cGFzcw@private.example:443#alice node\n".encode("utf-8")
        ),
    )

    raw_path = tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw"
    decoded = base64.b64decode(raw_path.read_text(encoding="utf-8")).decode("utf-8")

    assert decoded.splitlines()[0].endswith("#PRIVATE%20%7C%20alice%20node")


def test_raw_render_excludes_shared_provider_nodes_by_name(
    tmp_path, config_path, users_path
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    registry = load_users(users_path)
    cache = tmp_path / "state/cache/provider.decoded"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        "\n".join(
            [
                "trojan://pass@one.example:443#Киев-1",
                "trojan://pass@two.example:443#МОСКВА-2",
                "trojan://pass@three.example:443#Warsaw-3",
            ]
        ),
        encoding="utf-8",
    )

    render_subscriptions(
        config,
        registry,
        options=RenderOptions(mode="raw-only", user_name="alice"),
        fetch_subscription=lambda url, timeout: PERSONAL_FEED.encode("utf-8"),
    )

    raw_path = tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw"
    decoded = base64.b64decode(raw_path.read_text(encoding="utf-8")).decode("utf-8")
    names = [unquote(urlsplit(uri).fragment) for uri in decoded.splitlines()]

    assert names == ["PRIVATE | alice node", "PROVIDER | Warsaw-3"]


def test_raw_render_missing_provider_cache_errors_with_refresh_hint(
    tmp_path, config_path, users_path
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    registry = load_users(users_path)

    with pytest.raises(ValidationError, match="refresh-provider"):
        render_subscriptions(
            config,
            registry,
            options=RenderOptions(mode="raw-only", user_name="alice"),
            fetch_subscription=lambda url, timeout: PERSONAL_FEED.encode("utf-8"),
        )


def test_invalid_personal_feed_preserves_existing_raw(
    tmp_path, config_path, users_path
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    registry = load_users(users_path)
    _write_provider_cache(tmp_path)
    raw_path = tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("old raw content", encoding="utf-8")

    summary = render_subscriptions(
        config,
        registry,
        options=RenderOptions(mode="raw-only", user_name="alice"),
        fetch_subscription=lambda url, timeout: b"not a subscription",
    )

    assert summary.failed == 1
    assert raw_path.read_text(encoding="utf-8") == "old raw content"


def test_network_failure_for_one_user_does_not_corrupt_other_users(
    tmp_path, config_path, users_path
):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    registry = load_users(users_path)
    _write_provider_cache(tmp_path)
    alice_raw = tmp_path / "public/s" / f"{VALID_ALICE_TOKEN}.raw"
    bob_raw = tmp_path / "public/s" / f"{VALID_BOB_TOKEN}.raw"
    alice_raw.parent.mkdir(parents=True, exist_ok=True)
    alice_raw.write_text("old alice raw", encoding="utf-8")

    def fetcher(url, timeout):
        if url.endswith("/alice"):
            raise TimeoutError
        return "ss://YWVzLTEyOC1nY206cGFzcw@bob.example:443#bob\n".encode("utf-8")

    summary = render_subscriptions(
        config,
        registry,
        options=RenderOptions(mode="raw-only"),
        fetch_subscription=fetcher,
    )

    assert summary.rendered == 1
    assert summary.failed == 1
    assert alice_raw.read_text(encoding="utf-8") == "old alice raw"
    assert bob_raw.exists()


def test_raw_summary_and_errors_do_not_include_secrets(tmp_path, config_path, users_path):
    config = load_config(
        config_path,
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "public",
    )
    registry = load_users(users_path)
    _write_provider_cache(tmp_path)

    summary = render_subscriptions(
        config,
        registry,
        options=RenderOptions(mode="raw-only", user_name="alice"),
        fetch_subscription=lambda url, timeout: b"not a subscription",
    )
    combined = "\n".join(
        [str(summary.rendered), str(summary.skipped), str(summary.failed)]
        + [user.error or "" for user in summary.users]
    )

    assert VALID_ALICE_TOKEN not in combined
    assert VALID_PROVIDER_TOKEN not in combined
    assert "https://provider.example/subscription" not in combined


def _write_provider_cache(tmp_path):
    cache = tmp_path / "state/cache/provider.decoded"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(PROVIDER_FEED, encoding="utf-8")
