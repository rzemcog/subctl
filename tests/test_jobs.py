from __future__ import annotations

import time
from pathlib import Path

from subctl.jobs import JobRunner, JobStore


class FakeSummary:
    rendered = 2
    skipped = 0
    failed = 0


class FakeService:
    def render_all(self):
        return FakeSummary()


def test_job_store_recovers_and_prunes(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3", retention=2)
    first = store.create("render_all")
    assert store.claim_next()["id"] == first["id"]
    store.recover_active()
    assert store.get(first["id"])["status"] == "queued"
    claimed = store.claim_next()
    store.finish(claimed["id"], status="succeeded", message="ok")

    second = store.create("render_all")
    store.finish(second["id"], status="succeeded", message="ok")
    third = store.create("render_all")
    store.finish(third["id"], status="succeeded", message="ok")
    assert len(store.list()) == 2


def test_runner_executes_render_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    runner = JobRunner(FakeService(), store)
    runner.start()
    try:
        job = runner.enqueue("render_all")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = store.get(job["id"])
            if current["status"] in {"succeeded", "failed", "interrupted"}:
                break
            time.sleep(0.01)

        assert current["status"] == "succeeded"
        assert current["message"] == "rendered=2 skipped=0 failed=0"
    finally:
        runner.stop()
