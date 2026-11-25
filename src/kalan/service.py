"""The read and write operations of kalan without HTTP: used by the CLI and the MCP server.

Every method calls the same domain functions as the matching API route (``build_schedule``,
``monthly_summary``, ``subscriptions``, ``compute_alerts``, ``simulate``, ``parse_import`` ...) with the
same arguments, so the terminal, the MCP tools and the web interface always show the same numbers.

Errors are :class:`ServiceError` with a stable ``code`` (the API error codes where one exists) and a
message without statement content.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Literal

from kalan.ai import reader
from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError, ProviderRegistry
from kalan.ai.jobs import JobRunner
from kalan.alerts import Alert, compute_alerts
from kalan.amounts import month_key
from kalan.config import Config
from kalan.imports import ImportFileError, Mapping, import_plans, parse_import
from kalan.models import Statement, StatementSummary, Transaction
from kalan.parsers import ParseError, StatementParser, UnknownFormatError, all_parsers, get_parser
from kalan.reconcile import reconcile
from kalan.redact import redact
from kalan.schedule import Schedule, build_schedule
from kalan.simulate import SimulationIn, simulate
from kalan.store import DuplicateStatementError, Store
from kalan.summary import attributed, monthly_summary, subscriptions
from kalan.text import fold

PDF_MAGIC = b"%PDF-"
ImportKind = Literal["pdf", "txt", "csv", "ofx", "qif"]
SUFFIX_KIND: dict[str, ImportKind] = {".pdf": "pdf", ".txt": "txt", ".csv": "csv", ".ofx": "ofx", ".qfx": "ofx",
                                      ".qif": "qif"}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class Kalan:
    """One user's data: a :class:`Store` plus the AI provider registry."""

    def __init__(
        self,
        config: Config | None = None,
        store: Store | None = None,
        registry: ProviderRegistry | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.config = config or Config()
        self.store = store or Store(self.config.db_path)
        self.registry = registry or ProviderRegistry()
        self.timeout = timeout

    # -- statements -------------------------------------------------------------------------------

    def statements(self) -> list[StatementSummary]:
        """Newest first, like ``GET /statements``."""
        return self.store.list_statements()

    def statement(self, statement_id: str) -> Statement:
        st = self.store.get_statement(statement_id)
        if st is None:
            raise ServiceError("statement_not_found", "No statement with this id.", {"id": statement_id})
        return st

    def banks(self) -> list[str]:
        """Parser and template ids accepted by ``import --bank``."""
        return [p.id for p in all_parsers()]

    def import_file(self, path: str | Path, bank: str | None = None, ai_fallback: bool = True) -> dict[str, Any]:
        """Import a statement PDF (or its extracted ``.txt``) or a CSV, OFX or QIF file.

        ``bank`` forces a parser for a PDF and selects the template rules for a CSV. Returns
        ``{"kind": "statement", "statement": {...}}`` or ``{"kind": "import", ...}`` (the
        ``POST /imports`` answer).
        """
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ServiceError("file_unreadable", f"cannot read {path.name} ({exc.strerror or type(exc).__name__})",
                               {"path": str(path)}) from None
        if len(data) > self.config.max_upload_bytes:
            raise ServiceError("file_too_large", "The file is larger than 10 MB.",
                               {"max_bytes": self.config.max_upload_bytes})
        kind = SUFFIX_KIND.get(path.suffix.lower())
        if kind is None and data.startswith(PDF_MAGIC):
            kind = "pdf"
        if bank is not None and bank not in self.banks():
            raise ServiceError("unknown_bank", f"No parser or template with id {bank!r}.", {"banks": self.banks()})
        if kind in ("pdf", "txt"):
            return {"kind": "statement", "statement": self._import_statement(data, kind, bank, ai_fallback)}
        return {"kind": "import", **self._import_rows(data, path.name, bank, kind)}

    def _import_statement(self, data: bytes, kind: ImportKind, bank: str | None, ai_fallback: bool) -> dict[str, Any]:
        if kind == "pdf":
            if not data.startswith(PDF_MAGIC):
                raise ServiceError("not_pdf", "The file is not a PDF.")
            from kalan.pdf import extract_text

            try:
                text = extract_text(data)
            except Exception:  # broken or encrypted PDF
                raise ServiceError("invalid_pdf", "The PDF could not be opened.") from None
        else:
            text = data.decode("utf-8-sig", errors="replace")
        kept_text: str | None = None
        try:
            st = self._parse(text, bank)
        except UnknownFormatError:
            if not ai_fallback:
                raise ServiceError("unknown_format", "No supported bank statement format was recognised.",
                                   {"ai_fallback": "off"}) from None
            st, kept_text = self._ai_read(text)
        except ParseError:
            raise ServiceError("parse_error", "The statement was recognised but could not be read.") from None
        except Exception:
            raise ServiceError("parse_error", "The statement was recognised but could not be read.") from None
        try:
            saved = self.store.add_statement(st, redacted_text=kept_text)
        except DuplicateStatementError as dup:
            raise ServiceError("duplicate", "This statement is already stored.",
                               {"statement_id": dup.existing_id}) from None
        return saved.model_dump(mode="json")

    @staticmethod
    def _parse(text: str, bank: str | None) -> Statement:
        """``pipeline.parse_text``, or the same steps with a forced parser."""
        if bank is None:
            from kalan.pipeline import parse_text

            return parse_text(text)
        result = redact(text)
        parser = get_parser(bank)
        if not parser.detect(result.text):
            raise UnknownFormatError(f"{bank} does not recognise this statement")
        st = parser.parse(result.text)
        if st.card_last4 is None and result.card_last4 is not None:
            st = st.model_copy(update={"card_last4": result.card_last4})
        return st.model_copy(update={"reconciliation": reconcile(st)})

    def _ai_read(self, text: str) -> tuple[Statement, str]:
        try:
            adapter = self.selected_adapter()
        except ServiceError as exc:
            raise ServiceError("unknown_format", "No supported bank statement format was recognised.",
                               {"ai_fallback": exc.code}) from None
        redacted = redact(text)
        try:
            st = reader.read_statement(adapter, redacted.text, redacted.card_last4, timeout=self.timeout)
        except reader.TextTooLongError:
            raise ServiceError("unknown_format", "No supported format was recognised and the statement is too "
                               "long for the AI reader.", {"ai_fallback": "too_long"}) from None
        except AdapterError as exc:
            raise ServiceError("ai_fallback_failed", f"{adapter.name}: {exc}", {"provider": adapter.id}) from None
        except reader.InvalidModelOutputError as exc:
            raise ServiceError("ai_fallback_failed", f"{adapter.name} returned an invalid answer twice: {exc}",
                               {"provider": adapter.id}) from None
        return st, redacted.text

    def _import_rows(self, data: bytes, filename: str, bank: str | None, kind: ImportKind | None) -> dict[str, Any]:
        fmt = kind if kind in ("csv", "ofx", "qif") else None
        try:
            result = parse_import(data, filename, Mapping(template=bank), self.store.get_settings().base_currency,
                                  fmt)  # type: ignore[arg-type]
        except ImportFileError as exc:
            raise ServiceError("import_unreadable", f"The file could not be imported: {exc}") from None
        import_id = StatementParser.stable_id("im", uuid.uuid4().hex)
        meta = {"currency": result.currency, "detected": result.detected, "months": result.months}
        imported, skipped = self.store.add_import(import_id, result.format, meta, result.transactions)
        stored = self.store.import_transactions(import_id)
        return {
            "id": import_id, "format": result.format, "currency": result.currency, "rows": len(result.transactions),
            "imported": imported, "skipped_duplicates": skipped, "months": result.months,
            "detected": result.detected, "transactions": [t.model_dump(mode="json") for t in stored],
        }

    # -- calculations -----------------------------------------------------------------------------

    def schedule(self, months: int | None = None) -> Schedule:
        """``GET /installments/schedule``."""
        store = self.store
        statements = store.all_statements()
        cut_off_day = max(statements, key=lambda s: s.period_end).period_end.day if statements else None
        imported = import_plans(store.import_transactions())
        result = build_schedule(
            statements,
            store.manual_plans(),
            months=months or store.get_settings().months_ahead,
            cut_off_day=cut_off_day,
            imported=imported,
            fixed_costs=store.fixed_costs(),
        )
        if not statements and not store.manual_plans() and not imported:
            result.currency = store.get_settings().base_currency
        return result

    def month(self, month: str | None = None) -> dict[str, Any]:
        """``GET /summary/monthly``: spend, credits and categories of one month (default: this month)."""
        return monthly_summary(
            self.store.all_statements(),
            self.store.loose_transactions(),
            month or month_key(date.today()),
            self.store.get_settings().base_currency,
        )

    def subscriptions(self) -> list[dict[str, Any]]:
        return subscriptions(self.store.all_statements(), self.store.loose_transactions(),
                             self.store.model_recurring_ids())

    def alerts(self) -> list[Alert]:
        statements = self.store.all_statements()
        subs = subscriptions(statements, self.store.loose_transactions(), self.store.model_recurring_ids())
        return compute_alerts(statements, self.schedule(), subs)

    def simulate(self, body: SimulationIn) -> dict[str, Any]:
        """``POST /simulate/installment``."""
        return simulate(body, self.schedule())

    def transactions(self, month: str | None = None, query: str | None = None, limit: int | None = None,
                     ) -> list[dict[str, Any]]:
        """Every transaction with the month it counts in, newest first, optionally filtered.

        ``query`` matches the description, merchant or category, ignoring case and Turkish accents.
        """
        needle = fold(query) if query else None
        rows: list[tuple[str, Transaction]] = attributed(self.store.all_statements(), self.store.loose_transactions())
        out: list[dict[str, Any]] = []
        for mk, tx in sorted(rows, key=lambda r: (r[1].date, r[1].id), reverse=True):
            if month and mk != month:
                continue
            if needle and not any(needle in fold(v) for v in (tx.description, tx.merchant, tx.category) if v):
                continue
            out.append({"month": mk, **tx.model_dump(mode="json")})
            if limit and len(out) >= limit:
                break
        return out

    # -- AI analysis ------------------------------------------------------------------------------

    def selected_adapter(self) -> Adapter:
        provider_id = self.store.selected_provider()
        if provider_id is None:
            raise ServiceError("no_provider", "Choose an AI provider first (in the app or PUT /providers/selected).")
        adapter = self.registry.get(provider_id)
        detection = self.registry.detect_all().get(provider_id)
        if adapter is None or detection is None or not detection.available:
            raise ServiceError("provider_unavailable", "The selected provider is not available on this computer.",
                               {"id": provider_id, "detail": detection.detail if detection else "unknown provider"})
        return adapter

    def analyze(self, statement_id: str) -> dict[str, Any]:
        """Run the AI analysis of one statement and wait for it (``POST /statements/{id}/analyze``)."""
        self.statement(statement_id)
        adapter = self.selected_adapter()
        runner = JobRunner(self.store, timeout=self.timeout)
        job = runner.start(statement_id, adapter)
        runner.wait(job.id)
        done = self.store.get_job(job.id)
        # A job started by a running ``kalan serve`` belongs to that process: poll the database for it.
        deadline = time.monotonic() + 2 * self.timeout + 10
        while done is not None and done.status == "pending" and time.monotonic() < deadline:
            time.sleep(0.5)
            done = self.store.get_job(job.id)
        if done is None or done.status != "done":
            raise ServiceError("analysis_failed", (done.error if done else None) or "The analysis did not finish.",
                               {"job_id": job.id, "provider": adapter.id})
        return self.analysis(statement_id)

    def analysis(self, statement_id: str) -> dict[str, Any]:
        self.statement(statement_id)
        found = self.store.get_analysis(statement_id)
        if found is None:
            raise ServiceError("analysis_not_found", "This statement has not been analysed yet.", {"id": statement_id})
        return found
