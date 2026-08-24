from __future__ import annotations

import os
import tempfile
import json
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .errors import SubctlError, UpstreamError
from .provider import ProviderFeed, validate_provider_subscription
from .public_files import PUBLIC_FILE_MODE, ensure_public_directory

DEFAULT_FETCH_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class RefreshResult:
    cache_raw_path: Path
    cache_decoded_path: Path
    public_feed_path: Path
    node_count: int
    started_at: str
    finished_at: str
    duration_ms: int


FetchProvider = Callable[[str, float], bytes]


def refresh_provider(
    config: AppConfig,
    *,
    timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    fetch_provider: FetchProvider | None = None,
) -> RefreshResult:
    fetcher = fetch_provider or _fetch_url
    started = time.perf_counter()
    started_at = _now()
    before_count = _cached_node_count(config)
    _write_status(
        config,
        {
            "status": "running",
            "started_at": started_at,
            "node_count_before": before_count,
        },
    )
    try:
        try:
            body = fetcher(config.provider.upstream_url, timeout_seconds)
        except UpstreamError:
            raise
        except urllib.error.HTTPError as exc:
            raise UpstreamError(f"provider fetch failed: HTTP status {exc.code}") from exc
        except TimeoutError as exc:
            raise UpstreamError("provider fetch failed: timeout") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise UpstreamError("provider fetch failed: timeout") from exc
            raise UpstreamError("provider fetch failed") from exc
        except OSError as exc:
            raise UpstreamError("provider fetch failed") from exc

        try:
            feed = validate_provider_subscription(body)
        except SubctlError:
            raise
        except Exception as exc:
            raise SubctlError("provider validation failed") from exc

        cache_dir = config.state_dir / "cache"
        public_feed_dir = config.public.output_dir / "feeds" / "provider"
        ensure_public_directory(public_feed_dir.parent)
        ensure_public_directory(public_feed_dir)
        targets = {
            cache_dir / "provider.raw": (feed.base64_text, None),
            cache_dir / "provider.decoded": (feed.plain_text, None),
            public_feed_dir / config.provider.shared_token: (feed.base64_text, PUBLIC_FILE_MODE),
        }
        _write_transaction(targets)
    except Exception as exc:
        finished_at = _now()
        _write_status(
            config,
            {
                "status": "failed",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": _duration_ms(started),
                "node_count_before": before_count,
                "node_count_after": before_count,
                "error": _safe_error(exc),
                "cache_preserved": True,
            },
        )
        raise

    finished_at = _now()
    _write_status(
        config,
        {
            "status": "succeeded",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": _duration_ms(started),
            "node_count_before": before_count,
            "node_count_after": len(feed.uris),
            "cache_preserved": False,
            "cache_updated_at": finished_at,
        },
    )
    return RefreshResult(
        cache_raw_path=cache_dir / "provider.raw",
        cache_decoded_path=cache_dir / "provider.decoded",
        public_feed_path=public_feed_dir / config.provider.shared_token,
        node_count=len(feed.uris),
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=_duration_ms(started),
    )


def provider_status_path(config: AppConfig) -> Path:
    return config.state_dir / "ui" / "provider-status.json"


def load_provider_status(config: AppConfig) -> dict[str, object]:
    path = provider_status_path(config)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _write_status(config: AppConfig, status: dict[str, object]) -> None:
    path = provider_status_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(status, ensure_ascii=False, sort_keys=True) + "\n"
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _cached_node_count(config: AppConfig) -> int:
    path = config.state_dir / "cache" / "provider.decoded"
    try:
        return len(validate_provider_subscription(path.read_text(encoding="utf-8")).uris)
    except (FileNotFoundError, OSError, SubctlError):
        return 0


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SubctlError):
        return str(exc)
    return f"operation failed: {type(exc).__name__}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _fetch_url(url: str, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "subctl/0.1"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise UpstreamError(f"provider fetch failed: HTTP status {status}")
        return response.read()


def _write_transaction(targets: dict[Path, tuple[str, int | None]]) -> None:
    temp_paths: dict[Path, Path] = {}
    backup_paths: dict[Path, Path] = {}
    moved_targets: list[Path] = []
    installed_targets: list[Path] = []

    try:
        for target, (content, mode) in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_paths[target] = _write_temp_text(target, content, mode=mode)

        for target in targets:
            if target.exists():
                backup = _temp_path_for(target)
                os.replace(target, backup)
                backup_paths[target] = backup
                moved_targets.append(target)

            os.replace(temp_paths[target], target)
            installed_targets.append(target)
            _fsync_dir(target.parent)

    except Exception as exc:
        for target in reversed(installed_targets):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        for target in reversed(moved_targets):
            backup = backup_paths.get(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
                _fsync_dir(target.parent)
        for temp_path in temp_paths.values():
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        raise SubctlError("provider refresh failed while writing files") from exc
    else:
        for backup in backup_paths.values():
            try:
                backup.unlink()
            except FileNotFoundError:
                pass


def _write_temp_text(target: Path, content: str, *, mode: int | None = None) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _temp_path_for(target: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".bak",
        dir=str(target.parent),
    )
    os.close(fd)
    backup = Path(tmp_name)
    backup.unlink()
    return backup


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
