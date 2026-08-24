from __future__ import annotations

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
    job = runner.enqueue("render_all")
    next_job = store.claim_next()
    assert next_job["id"] == job["id"]
    message = runner._execute(next_job)
    store.finish(job["id"], status="succeeded", message=message)
    assert store.get(job["id"])["message"] == "rendered=2 skipped=0 failed=0"
