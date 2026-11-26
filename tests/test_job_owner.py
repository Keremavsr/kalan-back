"""Job ownership: a service fails only pending jobs whose owner process is gone or silent (task 7)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import timedelta

from kalan.ai.adapters import FakeAdapter
from kalan.ai.jobs import JobRunner
from kalan.procs import pid_alive
from kalan.store import JOB_STALE_AFTER, Store, parse_iso
from tests.conftest import VB_PDFS


def dead_pid() -> int:
    """The id of a process that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _job(store: Store, job_id: str, owner: int | None, beat: str | None, kind: str = "analysis_all") -> None:
    with store._tx() as con:
        con.execute(
            "INSERT INTO jobs (id, statement_id, provider, status, created_at, kind, owner_pid, heartbeat_at) "
            "VALUES (?, '', 'fake', 'pending', ?, ?, ?, ?)",
            (job_id, beat or "2099-01-01T00:00:00Z", kind, owner, beat),
        )


def _status(store: Store, job_id: str) -> str:
    job = store.get_job(job_id)
    assert job is not None
    return job.status


def test_pid_alive():
    assert pid_alive(os.getpid()) and pid_alive(os.getppid())
    assert not pid_alive(dead_pid())
    assert not pid_alive(None) and not pid_alive(0) and not pid_alive(-5)


def test_only_dead_or_silent_owners_are_failed(store):
    beat = "2099-01-01T12:00:00Z"
    now = parse_iso(beat) + timedelta(minutes=1)
    _job(store, "job_live", os.getppid(), beat)  # another live process, beat a minute ago
    _job(store, "job_dead", dead_pid(), beat)
    _job(store, "job_legacy", None, None)  # stored before owners were recorded
    assert store.fail_interrupted_jobs(now=now) == 2
    assert [_status(store, j) for j in ("job_live", "job_dead", "job_legacy")] == ["pending", "failed", "failed"]
    assert store.get_job("job_dead").error.startswith("interrupted")

    later = parse_iso(beat) + JOB_STALE_AFTER + timedelta(seconds=1)  # alive but silent too long
    assert store.fail_interrupted_jobs(now=later) == 1
    assert _status(store, "job_live") == "failed"


def test_orphan_pending_statement_is_released_only_when_no_job_runs(store):
    from kalan.pipeline import parse_pdf

    st = store.add_statement(parse_pdf(VB_PDFS[9]))
    store.set_analysis_status(st.id, "pending")  # a batch was analysing it
    beat = "2099-01-01T12:00:00Z"
    _job(store, "job_live", os.getppid(), beat)
    store.fail_interrupted_jobs(now=parse_iso(beat))
    assert store.get_statement(st.id).analysis_status == "pending"  # the live batch may still be on it
    store.fail_interrupted_jobs(now=parse_iso(beat) + JOB_STALE_AFTER * 2)
    assert store.get_statement(st.id).analysis_status == "failed"  # the next batch takes it again


def test_new_jobs_carry_this_process_and_a_heartbeat(store):
    gate = threading.Event()
    runner = JobRunner(store, timeout=5, heartbeat_seconds=0.05)
    job = runner.start_batch(FakeAdapter(), lambda a, t: gate.wait(10))  # waits in the suggestions step
    assert job.owner_pid == os.getpid() and job.heartbeat_at
    with store._tx() as con:
        con.execute("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?", (job.id,))
    deadline = time.monotonic() + 5
    while store.get_job(job.id).heartbeat_at.startswith("2000") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not store.get_job(job.id).heartbeat_at.startswith("2000")
    gate.set()
    runner.wait(job.id, 10)
    assert _status(store, job.id) == "done"


def test_second_runner_does_not_start_a_second_batch(store):
    beat = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _job(store, "job_other", os.getppid(), beat)  # a batch another service is running right now
    runner = JobRunner(store, timeout=5)
    assert runner.start_batch(FakeAdapter(), lambda a, t: None).id == "job_other"

    with store._tx() as con:  # that service died: a new batch starts
        con.execute("UPDATE jobs SET owner_pid = ? WHERE id = 'job_other'", (dead_pid(),))
    job = runner.start_batch(FakeAdapter(), lambda a, t: None)
    assert job.id != "job_other"
    runner.wait(job.id, 10)
