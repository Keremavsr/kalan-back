"""Background analysis jobs.

* A statement job: one model call per statement (plus one retry on an invalid answer).
* A batch job (``POST /analysis/all``): every statement that still needs the model, one after the other, then
  AI saving suggestions from aggregates (:mod:`kalan.ai.advice`). A failed statement is marked ``failed`` and the
  batch goes on; a failed suggestion step keeps the previous suggestions. Statements stored while a batch runs
  join it.

Every pending job records the process that runs it; a heartbeat thread refreshes its ``heartbeat_at`` every
:data:`HEARTBEAT_SECONDS` while it runs, so a second service on the same database leaves it alone
(:meth:`kalan.store.Store.fail_interrupted_jobs`) and does not start a second batch next to it.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable

from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError
from kalan.ai.prompt import InvalidModelOutputError, build_payload, build_prompt, parse_answer, retry_prompt
from kalan.store import Job, Store

log = logging.getLogger("kalan.jobs")
HEARTBEAT_SECONDS = 30.0


class JobRunner:
    def __init__(self, store: Store, timeout: float = DEFAULT_TIMEOUT,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS) -> None:
        self.store = store
        self.timeout = timeout
        self.heartbeat_seconds = heartbeat_seconds
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._batch_extra: dict[str, set[str]] = {}
        self._beat: threading.Thread | None = None
        self._beat_wake = threading.Event()

    # -- one statement ------------------------------------------------------------------------------

    def start(self, statement_id: str, adapter: Adapter) -> Job:
        """Queue an analysis; an already pending job for the same statement is returned instead."""
        with self._lock:
            existing = self.store.pending_job_for(statement_id)
            if existing is not None:
                return existing
            job = self.store.create_job(f"job_{uuid.uuid4().hex[:12]}", statement_id, adapter.id)
            self._spawn(job.id, self.run, (job.id, statement_id, adapter))
        log.info("job %s queued: statement %s, provider %s", job.id, statement_id, adapter.id)
        return job

    def _spawn(self, job_id: str, target: Callable[..., None], args: tuple[object, ...]) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True, name=f"kalan-{job_id}")
        self._threads[job_id] = thread
        thread.start()
        if self._beat is None or not self._beat.is_alive():
            self._beat = threading.Thread(target=self._heartbeat_loop, daemon=True, name="kalan-heartbeat")
            self._beat.start()

    def _heartbeat_loop(self) -> None:
        """While this runner has jobs, tell the database every few seconds that their owner is alive."""
        while True:
            self._beat_wake.wait(self.heartbeat_seconds)
            self._beat_wake.clear()
            with self._lock:
                running = list(self._threads)
                if not running:
                    self._beat = None
                    return
            try:
                self.store.heartbeat(running)
            except Exception as exc:  # a busy database must not end the heartbeat
                log.warning("heartbeat failed: %s", type(exc).__name__)

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)

    def run(self, job_id: str, statement_id: str, adapter: Adapter) -> None:
        try:
            error = self.analyze(statement_id, adapter, job_id)
            if error is None:
                self.store.finish_job(job_id, "done")
            else:
                self.store.finish_job(job_id, "failed", error)
        except Exception as exc:  # never let a job thread die silently
            log.error("job %s failed: %s", job_id, type(exc).__name__)
            self.store.finish_job(job_id, "failed", f"internal error ({type(exc).__name__})")
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def analyze(self, statement_id: str, adapter: Adapter, job_id: str = "-") -> str | None:
        """Analyse one statement and store the result. Returns None on success, else the error text (the
        statement is then marked ``failed``)."""
        statement = self.store.get_statement(statement_id)
        if statement is None:
            return "the statement was deleted"
        locale = self.store.get_settings().locale
        known = {tx.id for tx in statement.transactions}
        uncategorised = {tx.id for tx in statement.transactions if tx.category is None}
        prompt = build_prompt(build_payload(statement), locale)
        log.info("job %s: sending %d transactions to %s", job_id, len(known), adapter.id)

        answer = None
        for attempt in (1, 2):
            try:
                text = adapter.complete(prompt, self.timeout)
            except AdapterError as exc:
                log.warning("job %s: provider %s failed (attempt %d)", job_id, adapter.id, attempt)
                self.store.set_analysis_status(statement_id, "failed")
                return f"{adapter.name}: {exc}"
            try:
                answer = parse_answer(text, known, uncategorised)
                break
            except InvalidModelOutputError as exc:
                log.warning("job %s: invalid answer from %s (attempt %d)", job_id, adapter.id, attempt)
                if attempt == 2:
                    self.store.set_analysis_status(statement_id, "failed")
                    return f"{adapter.name} returned an invalid answer twice: {exc}"
                prompt = retry_prompt(prompt, str(exc))

        assert answer is not None
        commentary = {"locale": locale, **answer.commentary.model_dump()}
        saved = self.store.save_analysis(
            statement_id, adapter.id, dict(answer.categories), set(answer.recurring), commentary
        )
        if not saved:
            return "the statement was deleted"
        log.info("job %s: statement %s done, %d categories applied (%d covered by rules)", job_id, statement_id,
                 len(answer.categories), len(known) - len(uncategorised))
        return None

    # -- batch --------------------------------------------------------------------------------------

    def needs_model(self, include_uncategorised: bool) -> list[str]:
        """Statements the batch should analyse: not analysed yet (or failed), and with ``include_uncategorised``
        also analysed ones that still have rows without a category. Oldest first."""
        out = []
        for st in self.store.all_statements():
            if st.analysis_status in ("none", "failed") or (
                include_uncategorised and st.analysis_status == "done"
                and any(t.category is None for t in st.transactions)
            ):
                out.append(st.id)
        return out

    def start_batch(self, adapter: Adapter, advise: Callable[[Adapter, float], None],
                    include_uncategorised: bool = True, also: list[str] | None = None) -> Job:
        """Start a batch, or return the pending one (``also`` statement ids then join it)."""
        with self._lock:
            pending = self.store.latest_batch_job(pending_only=True)
            if pending is not None and pending.id in self._threads:
                self._batch_extra.setdefault(pending.id, set()).update(also or [])
                return pending
            if pending is not None and pending.owner_pid != os.getpid() and self.store.job_alive(pending):
                # Another service on the same database runs it; new statements join it there (status "none").
                return pending
            todo = self.needs_model(include_uncategorised)
            for sid in also or []:
                if sid not in todo:
                    todo.append(sid)
            job = self.store.create_batch_job(f"job_{uuid.uuid4().hex[:12]}", adapter.id, len(todo))
            self._batch_extra[job.id] = set()
            self._spawn(job.id, self.run_batch, (job.id, todo, adapter, advise))
        log.info("batch %s queued: %d statements, provider %s", job.id, len(todo), adapter.id)
        return job

    def run_batch(self, job_id: str, todo: list[str], adapter: Adapter,
                  advise: Callable[[Adapter, float], None]) -> None:
        done = failed = 0
        tried: set[str] = set()
        last_error: str | None = None
        try:
            queue = list(todo)
            while queue:
                sid = queue.pop(0)
                tried.add(sid)
                self.store.set_analysis_status(sid, "pending")
                error = self.analyze(sid, adapter, job_id)
                done += 1
                if error is not None:
                    failed += 1
                    last_error = error
                with self._lock:  # statements stored meanwhile join the batch
                    extra = self._batch_extra.get(job_id, set()) - tried - set(queue)
                    self._batch_extra[job_id] = set()
                extra |= {s for s in self.needs_model(False) if s not in tried and s not in queue}
                queue += sorted(extra)
                self.store.update_job(job_id, done=done, failed=failed, total=done + len(queue))
            self.store.update_job(job_id, phase="suggestions")
            try:
                advise(adapter, self.timeout)
            except (AdapterError, InvalidModelOutputError) as exc:
                log.warning("batch %s: suggestions failed (%s)", job_id, type(exc).__name__)
                last_error = f"{adapter.name}: suggestions: {exc}"
            self.store.finish_job(job_id, "done", last_error)
            log.info("batch %s done: %d statements, %d failed", job_id, done, failed)
        except Exception as exc:
            log.error("batch %s failed: %s", job_id, type(exc).__name__)
            self.store.finish_job(job_id, "failed", f"internal error ({type(exc).__name__})")
        finally:
            with self._lock:
                self._threads.pop(job_id, None)
                self._batch_extra.pop(job_id, None)


__all__ = ["JobRunner"]
