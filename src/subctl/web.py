from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import DEFAULT_CONFIG_PATH, DEFAULT_STATE_DIR, DEFAULT_USERS_PATH
from .errors import SubctlError, ValidationError
from .jobs import JobRunner, JobStore
from .service import SubscriptionService


class UserCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    xui_subscription: str = Field(min_length=1, max_length=4096)


class UserUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    xui_subscription: str | None = Field(default=None, min_length=1, max_length=4096)


class DeleteRequest(BaseModel):
    confirm_name: str


class SettingsRequest(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    user_name: str | None = None


class ProviderSettingsRequest(BaseModel):
    upstream_url: str = Field(min_length=1, max_length=4096)
    refresh_interval_seconds: int = Field(gt=0)
    exclude_keywords: list[str] = Field(default_factory=list)


def create_app(
    *,
    service: SubscriptionService | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    service = service or _service_from_environment()
    store = store or JobStore(_path_from_env("SUBCTL_WEB_JOBS_DB", "/var/lib/subctl/ui/jobs.sqlite3"))
    runner = JobRunner(service, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runner.start()
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(title="subctl admin", version="0.2.0", lifespan=lifespan)
    app.state.service = service
    app.state.store = store
    app.state.runner = runner

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/users")
    def list_users() -> dict[str, Any]:
        return {"users": [service.user_view(user, store=store) for user in service.list_users()]}

    @app.get("/api/users/{name}")
    def get_user(name: str) -> dict[str, Any]:
        try:
            user = service.get_user(name)
            return {"user": service.user_view(user, reveal_upstream=True, store=store)}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.get("/api/users/{name}/activity")
    def user_activity(name: str) -> dict[str, Any]:
        try:
            service.get_user(name)
            return {"activity": store.user_activity(name)}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.get("/api/users/{name}/preview")
    def user_preview(name: str) -> dict[str, Any]:
        try:
            service.get_user(name)
            return service.preview_settings({}, user_name=name)
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/users", status_code=201)
    async def create_user(request: Request, payload: UserCreateRequest) -> dict[str, Any]:
        _require_mutation_header(request)
        try:
            user = service.create_user(name=payload.name, xui_subscription=payload.xui_subscription)
            job = runner.enqueue("render_user", user.name)
            return {"user": service.user_view(user, reveal_upstream=True, store=store), "job": job}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.patch("/api/users/{name}")
    async def update_user(request: Request, name: str, payload: UserUpdateRequest) -> dict[str, Any]:
        _require_mutation_header(request)
        if payload.name is None and payload.xui_subscription is None:
            raise HTTPException(status_code=422, detail="at least one field is required")
        try:
            user = service.update_user(
                current_name=name,
                new_name=payload.name,
                xui_subscription=payload.xui_subscription,
            )
            job = runner.enqueue("render_user", user.name)
            return {"user": service.user_view(user, reveal_upstream=True, store=store), "job": job}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/users/{name}/rotate-token")
    async def rotate_token(request: Request, name: str) -> dict[str, Any]:
        _require_mutation_header(request)
        try:
            user = service.rotate_user(name=name)
            job = runner.enqueue("render_user", user.name)
            return {"user": service.user_view(user, reveal_upstream=True, store=store), "job": job}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/users/{name}")
    async def delete_user(request: Request, name: str, payload: DeleteRequest) -> dict[str, Any]:
        _require_mutation_header(request)
        if payload.confirm_name != name:
            raise HTTPException(status_code=400, detail="confirmation name does not match")
        try:
            service.delete_user(name=name)
            return {"deleted": name}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/users/{name}/render")
    async def render_user(request: Request, name: str) -> dict[str, Any]:
        _require_mutation_header(request)
        try:
            service.get_user(name)
            return {"job": runner.enqueue("render_user", name)}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.get("/api/provider/status")
    def provider_status() -> dict[str, Any]:
        try:
            status = service.provider_status()
            status["last_refresh_job"] = store.latest("refresh_provider")
            return status
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/provider/refresh")
    async def provider_refresh(request: Request) -> dict[str, Any]:
        _require_mutation_header(request)
        return {"job": runner.enqueue("refresh_provider")}

    @app.post("/api/provider/settings")
    async def provider_settings(request: Request, payload: ProviderSettingsRequest) -> dict[str, Any]:
        _require_mutation_header(request)
        try:
            current = service.settings()["settings"]
            settings = {
                **current,
                "provider": {
                    **current["provider"],
                    "upstream_url": payload.upstream_url,
                    "refresh_interval_seconds": payload.refresh_interval_seconds,
                },
                "render": {
                    **current["render"],
                    "provider_exclude_keywords": payload.exclude_keywords,
                },
            }
            published = service.publish_settings(settings)
            job = runner.enqueue("refresh_provider")
            return {"settings": published, "job": job}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        try:
            return service.settings()
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/settings/draft")
    async def save_settings_draft(request: Request, payload: SettingsRequest) -> dict[str, Any]:
        _require_mutation_header(request)
        try:
            return {"draft": service.save_settings_draft(payload.settings)}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/settings/preview")
    async def preview_settings(payload: SettingsRequest) -> dict[str, Any]:
        try:
            return service.preview_settings(payload.settings, user_name=payload.user_name)
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/settings/publish")
    async def publish_settings(request: Request, payload: SettingsRequest) -> dict[str, Any]:
        _require_mutation_header(request)
        try:
            published = service.publish_settings(payload.settings)
            job = runner.enqueue("render_all")
            return {"settings": published, "job": job}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/settings/rollback/{version}")
    async def rollback_settings(request: Request, version: int) -> dict[str, Any]:
        _require_mutation_header(request)
        try:
            published = service.rollback_settings(version)
            job = runner.enqueue("render_all")
            return {"settings": published, "job": job}
        except SubctlError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/render")
    async def render_all(request: Request) -> dict[str, Any]:
        _require_mutation_header(request)
        return {"job": runner.enqueue("render_all")}

    @app.get("/api/jobs")
    def list_jobs(limit: int = 100) -> dict[str, Any]:
        return {"jobs": store.list(limit=limit)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return {"job": store.get(job_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.api_route("/s/{token}.{format_name}", methods=["GET", "HEAD"])
    def user_subscription(token: str, format_name: str) -> FileResponse:
        if format_name not in {"yaml", "raw"}:
            raise HTTPException(status_code=404, detail="not found")
        user = service.get_user_by_token(token)
        if user is None:
            raise HTTPException(status_code=404, detail="not found")
        config = service._config()
        path = config.public.output_dir / "s" / f"{token}.{format_name}"
        if not path.exists():
            store.record_fetch(user.name, format_name, 404, None)
            raise HTTPException(status_code=404, detail="subscription not generated")
        store.record_fetch(user.name, format_name, 200, path.stat().st_size)
        media_type = "text/yaml; charset=utf-8" if format_name == "yaml" else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.api_route("/feeds/provider/{token}", methods=["GET", "HEAD"])
    def provider_subscription(token: str) -> FileResponse:
        config = service._config()
        if token != config.provider.shared_token:
            raise HTTPException(status_code=404, detail="not found")
        path = config.public.output_dir / "feeds" / "provider" / token
        if not path.exists():
            raise HTTPException(status_code=404, detail="provider feed not generated")
        return FileResponse(path, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-store"})

    static_dir = Path(__file__).resolve().parent / "web_dist"
    if static_dir.exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/{path:path}")
        def frontend(path: str) -> FileResponse:
            if path.startswith("api/") or path.startswith("s/") or path.startswith("feeds/"):
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(static_dir / "index.html")

    return app


def _service_from_environment() -> SubscriptionService:
    return SubscriptionService(
        config_path=_path_from_env("SUBCTL_WEB_CONFIG", str(DEFAULT_CONFIG_PATH)),
        users_path=_path_from_env("SUBCTL_WEB_USERS", str(DEFAULT_USERS_PATH)),
        state_dir=_optional_path_from_env("SUBCTL_WEB_STATE_DIR", str(DEFAULT_STATE_DIR)),
        output_dir=_optional_path_from_env("SUBCTL_WEB_OUTPUT_DIR", None),
        lock_path=_optional_path_from_env("SUBCTL_WEB_LOCK_FILE", "/run/subctl-refresh/refresh.lock"),
    )


def _path_from_env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def _optional_path_from_env(name: str, default: str | None) -> Path | None:
    value = os.environ.get(name, default)
    return Path(value) if value else None


def _require_mutation_header(request: Request) -> None:
    if request.headers.get("x-subctl-ui") != "1":
        raise HTTPException(status_code=403, detail="UI mutation header required")


def _http_error(exc: SubctlError) -> HTTPException:
    status = 422 if isinstance(exc, ValidationError) else 500
    return HTTPException(status_code=status, detail=str(exc))


app = create_app()
