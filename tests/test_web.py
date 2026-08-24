from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from subctl.jobs import JobStore
from subctl.service import SubscriptionService
from subctl.web import create_app


def _service(tmp_path: Path) -> SubscriptionService:
    output = tmp_path / "public"
    config = tmp_path / "config.yaml"
    users = tmp_path / "users.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "provider": {"upstream_url": "https://provider.example/feed", "shared_token": "p" * 32, "refresh_interval_seconds": 900},
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
    return SubscriptionService(
        config_path=config,
        users_path=users,
        state_dir=tmp_path / "state",
        output_dir=output,
        lock_path=tmp_path / "lock",
    )


def test_web_crud_requires_ui_header(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = create_app(service=service, store=JobStore(tmp_path / "jobs.sqlite3"))
    with TestClient(app) as client:
        response = client.post(
            "/api/users",
            json={"name": "alice", "xui_subscription": "https://panel.example/sub/alice"},
        )
        assert response.status_code == 403

        response = client.post(
            "/api/users",
            headers={"X-Subctl-UI": "1"},
            json={"name": "alice", "xui_subscription": "https://panel.example/sub/alice"},
        )
        assert response.status_code == 201
        assert response.json()["user"]["name"] == "alice"
        assert "xui_subscription" in response.json()["user"]

        listed = client.get("/api/users").json()["users"]
        assert listed[0]["name"] == "alice"
        assert "xui_subscription" not in listed[0]


def test_public_fetch_records_user_telemetry_without_ip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = JobStore(tmp_path / "jobs.sqlite3")
    app = create_app(service=service, store=store)
    with TestClient(app) as client:
        created = client.post(
            "/api/users",
            headers={"X-Subctl-UI": "1"},
            json={"name": "alice", "xui_subscription": "https://panel.example/sub/alice"},
        ).json()["user"]
        user = service.get_user("alice")
        output = service._config().public.output_dir / "s"
        output.mkdir(parents=True)
        (output / f"{user.token}.yaml").write_text("profile", encoding="utf-8")

        response = client.get(f"/s/{user.token}.yaml")
        assert response.status_code == 200
        activity = client.get("/api/users/alice/activity").json()["activity"]
        assert activity["fetch"]["yaml"]["http_status"] == 200
        assert "ip" not in str(activity)
        assert created["status"]["artifacts"]["yaml"]["present"] is False
