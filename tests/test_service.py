from __future__ import annotations

from pathlib import Path

import yaml

from subctl.service import SubscriptionService


def _config(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    state = tmp_path / "state"
    output = tmp_path / "public"
    users = tmp_path / "users.yaml"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "provider": {
                    "upstream_url": "https://provider.example/feed",
                    "shared_token": "p" * 32,
                    "refresh_interval_seconds": 900,
                },
                "public": {"base_url": "https://sub.example", "output_dir": str(output)},
                "render": {
                    "profile_update_interval_seconds": 3600,
                    "provider_update_interval_seconds": 900,
                    "healthcheck_url": "https://www.gstatic.com/generate_204",
                    "healthcheck_interval_seconds": 15,
                    "healthcheck_timeout_milliseconds": 3000,
                    "healthcheck_max_failed_times": 2,
                    "healthcheck_tolerance_milliseconds": 50,
                    "healthcheck_lazy": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return config, users, state, output


def test_user_lifecycle_preserves_and_rotates_tokens(tmp_path: Path) -> None:
    config, users, state, output = _config(tmp_path)
    service = SubscriptionService(
        config_path=config,
        users_path=users,
        state_dir=state,
        output_dir=output,
        lock_path=tmp_path / "lock",
    )

    created = service.create_user(name="alice", xui_subscription="https://panel.example/sub/alice")
    old_token = created.token
    (output / "s").mkdir(parents=True)
    (output / "s" / f"{old_token}.yaml").write_text("old", encoding="utf-8")
    (output / "s" / f"{old_token}.raw").write_text("old", encoding="utf-8")

    renamed = service.update_user(current_name="alice", new_name="alice-renamed")
    assert renamed.token == old_token
    assert service.get_user("alice-renamed").xui_subscription.endswith("/alice")

    rotated = service.rotate_user(name="alice-renamed")
    assert rotated.token != old_token
    assert not (output / "s" / f"{old_token}.yaml").exists()
    assert not (output / "s" / f"{old_token}.raw").exists()

    service.delete_user(name="alice-renamed")
    assert service.list_users() == []


def test_user_view_contains_public_links_but_not_raw_token(tmp_path: Path) -> None:
    config, users, state, output = _config(tmp_path)
    service = SubscriptionService(
        config_path=config,
        users_path=users,
        state_dir=state,
        output_dir=output,
        lock_path=tmp_path / "lock",
    )
    user = service.create_user(name="bob", xui_subscription="https://panel.example/sub/bob")
    view = service.user_view(user)
    detail = service.user_view(user, reveal_upstream=True)

    assert view["yaml_url"] == f"https://sub.example/s/{user.token}.yaml"
    assert "token" not in view
    assert detail["xui_subscription"] == user.xui_subscription


def test_settings_overlay_is_versioned_and_applies_to_effective_config(tmp_path: Path) -> None:
    config, users, state, output = _config(tmp_path)
    service = SubscriptionService(
        config_path=config,
        users_path=users,
        state_dir=state,
        output_dir=output,
        lock_path=tmp_path / "lock",
    )

    user = service.create_user(name="alice", xui_subscription="https://panel.example/sub/alice")
    draft = service.save_settings_draft(
        {
            "render": {
                "healthcheck_interval_seconds": 30,
                "composition": {"provider_first": True},
            }
        }
    )
    assert draft["render"]["healthcheck_interval_seconds"] == 30
    assert draft["render"]["composition"]["provider_first"] is True
    assert not service.settings_path.exists()

    published = service.publish_settings(draft)
    assert published["version"] == 1
    assert service.settings()["version"] == 1
    effective = service._config()
    assert effective.render.healthcheck_interval_seconds == 30
    assert effective.render.provider_first is True
    preview = service.preview_settings({}, user_name=user.name)
    assert preview["secrets_hidden"] is True
    assert "panel.example/sub/alice" not in preview["yaml"]

    rolled_back = service.rollback_settings(1)
    assert rolled_back["version"] == 2
    assert rolled_back["rolled_back_from"] == 1
