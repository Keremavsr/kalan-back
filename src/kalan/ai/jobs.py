"""Background analysis jobs: one model call per statement (plus one retry on an invalid answer)."""

from __future__ import annotations

import logging
import threading
import uuid

from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError
from kalan.ai.prompt import InvalidModelOutputError, build_payload, build_prompt, parse_answer, retry_prompt
from kalan.store import Job, Store

log = logging.getLogger("kalan.jobs")


class JobRunner:
    def __init__(self, store: Store, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.store = store
        self.timeout = timeout
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def start(self, statement_id: str, adapter: Adapter) -> Job:
        """Queue an analysis; an already pending job for the same statement is returned instead."""
        with self._lock:
            existing = self.store.pending_job_for(statement_id)
            if existing is not None:
                return existing
            job = self.store.create_job(f"job_{uuid.uuid4().hex[:12]}", statement_id, adapter.id)
            thread = threading.Thread(target=self.run, args=(job.id, statement_id, adapter), daemon=True,
                                      name=f"kalan-{job.id}")
            self._threads[job.id] = thread
        log.info("job %s queued: statement %s, provider %s", job.id, statement_id, adapter.id)
        thread.start()
        return job

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)

    def run(self, job_id: str, statement_id: str, adapter: Adapter) -> None:
        try:
            self._run(job_id, statement_id, adapter)
        except Exception as exc:  # never let a job thread die silently
            log.error("job %s failed: %s", job_id, type(exc).__name__)
            self.store.finish_job(job_id, "failed", f"internal error ({type(exc).__name__})")
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def _run(self, job_id: str, statement_id: str, adapter: Adapter) -> None:
        statement = self.store.get_statement(statement_id)
        if statement is None:
            self.store.finish_job(job_id, "failed", "the statement was deleted")
            return
        locale = self.store.get_settings().locale
        known = {tx.id for tx in statement.transactions}
        prompt = build_prompt(build_payload(statement), locale)
        log.info("job %s: sending %d transactions to %s", job_id, len(known), adapter.id)

        answer = None
        for attempt in (1, 2):
            try:
                text = adapter.complete(prompt, self.timeout)
            except AdapterError as exc:
                log.warning("job %s: provider %s failed (attempt %d)", job_id, adapter.id, attempt)
                self.store.finish_job(job_id, "failed", f"{adapter.name}: {exc}")
                return
            try:
                answer = parse_answer(text, known)
                break
            except InvalidModelOutputError as exc:
                log.warning("job %s: invalid answer from %s (attempt %d)", job_id, adapter.id, attempt)
                if attempt == 2:
                    self.store.finish_job(
                        job_id, "failed", f"{adapter.name} returned an invalid answer twice: {exc}"
                    )
                    return
                prompt = retry_prompt(prompt, str(exc))

        assert answer is not None
        commentary = {"locale": locale, **answer.commentary.model_dump()}
        saved = self.store.save_analysis(
            statement_id, adapter.id, dict(answer.categories), set(answer.recurring), commentary
        )
        if not saved:
            self.store.finish_job(job_id, "failed", "the statement was deleted")
            return
        self.store.finish_job(job_id, "done")
        log.info("job %s done: %d categories applied", job_id, len(answer.categories))
