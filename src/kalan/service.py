"""The read and write operations of kalan without HTTP: used by the CLI and the MCP server.

Every method calls the same domain functions as the matching API route (``build_schedule``,
``monthly_summary``, ``subscriptions``, ``compute_alerts``, ``simulate``, ``parse_import`` ...) with the
same arguments, so the terminal, the MCP tools and the web interface always show the same numbers.

Errors are :class:`ServiceError` with a stable ``code`` (the API error codes where one exists) and a
message without statement content.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

from kalan import categories, gate
from kalan.ai import reader
from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError, ProviderRegistry
from kalan.ai.jobs import JobRunner
from kalan.ai.prompt import CATEGORIES
from kalan.alerts import Alert, compute_alerts
from kalan.amounts import month_key
from kalan.config import Config
from kalan.imports import ImportFileError, Mapping, import_plans, parse_import
from kalan.insights.cancellations import list_cancelled
from kalan.insights.card_fee import find_card_fees, refund_letter_prompt
from kalan.insights.context import current_schedule, expected_amounts, plan_closures, regular, rule_overrides
from kalan.insights.early_payoff import payoff, payoff_overview
from kalan.insights.export import ExportFormat, export
from kalan.insights.fixed_match import fixed_cost_report, matched_transaction_ids
from kalan.insights.forecast import backtest, next_statement_forecast
from kalan.insights.minimum import latest_statement, minimum_payment_plan
from kalan.insights.share_card import CardLocale, load_card
from kalan.insights.which_card import card_options
from kalan.models import Statement, StatementSummary, Transaction
from kalan.parsers import ParseError, StatementParser, UnknownFormatError, all_parsers, get_parser
from kalan.reconcile import reconcile
from kalan.redact import redact
from kalan.schedule import Schedule
from kalan.simulate import SimulationIn, simulate
from kalan.store import DuplicateRuleError, DuplicateStatementError, Store
from kalan.summary import attributed, monthly_summary
from kalan.text import fold

PDF_MAGIC = b"%PDF-"
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
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


def check_month(value: str | None) -> str | None:
    """``None`` or a ``YYYY-MM`` month, else :class:`ServiceError` ``invalid_month``."""
    if value is not None and not MONTH_RE.match(value):
        raise ServiceError("invalid_month", f"not a month (YYYY-MM): {value!r}")
    return value


def check_date(value: str | None) -> date | None:
    """``None`` or a ``YYYY-MM-DD`` date, else :class:`ServiceError` ``invalid_date``."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ServiceError("invalid_date", f"not a date (YYYY-MM-DD): {value!r}") from None


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

    def import_file(self, path: str | Path, bank: str | None = None, ai_fallback: bool = True,
                    password: str | None = None) -> dict[str, Any]:
        """Import a statement PDF (or its extracted ``.txt``) or a CSV, OFX or QIF file.

        ``bank`` forces a parser for a PDF and selects the template rules for a CSV. ``password`` opens an
        encrypted PDF; without it the passwords remembered in the OS keyring are tried. The statement goes
        through the same save gate and category rules as ``POST /statements``. Returns
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
            return {"kind": "statement",
                    "statement": self._import_statement(data, kind, bank, ai_fallback, password)}
        return {"kind": "import", **self._import_rows(data, path.name, bank, kind)}

    def password_hints(self) -> list[dict[str, str]]:
        """``[{"bank", "hint"}]``: where each bank's statement password comes from, as the parsers know it."""
        hints: dict[str, str] = {}
        for p in all_parsers():
            if p.password_hint and p.bank not in hints:
                hints[p.bank] = p.password_hint
        return [{"bank": b, "hint": h} for b, h in sorted(hints.items())]

    def _open_pdf[T](self, data: bytes, password: str | None, read: Callable[..., T]) -> T:
        """``read(data, password=...)``; an encrypted PDF opens with ``password`` or a remembered one.

        Same codes as the API: ``pdf_password_wrong``, ``pdf_password_required`` (both with ``hints``),
        ``invalid_pdf``.
        """
        from kalan import pdf_passwords
        from kalan.pdf import PasswordError

        try:
            return read(data, password=password)
        except PasswordError as exc:
            if exc.wrong:
                raise ServiceError("pdf_password_wrong", "The password does not open this PDF.",
                                   {"hints": self.password_hints()}) from None
            found = pdf_passwords.open_with_remembered(data, sorted({p.bank for p in all_parsers()}), read=read)
            if found is None:
                raise ServiceError("pdf_password_required", "The PDF is encrypted. Give its password with "
                                   "--password.", {"hints": self.password_hints()}) from None
            return found[0]
        except Exception:  # broken PDF
            raise ServiceError("invalid_pdf", "The PDF could not be opened.") from None

    def _save_gate(self, st: Statement) -> Statement:
        """:func:`kalan.gate.check` as in ``POST /statements``: hard findings raise, soft ones mark for review."""
        issues = gate.check(st, self.store.all_statements())
        blocking = gate.hard(issues)
        dup = next((i for i in blocking if i.code == "duplicate_statement"), None)
        if dup is not None:
            raise ServiceError("duplicate", "This statement is already stored.",
                               {"statement_id": dup.details["statement_id"]})
        if blocking:
            raise ServiceError("save_blocked", "The statement was read but failed the checks before saving: "
                               + ", ".join(i.code for i in blocking) + ".",
                               {"issues": [i.model_dump(mode="json") for i in issues]})
        review = gate.soft(issues)
        return st.model_copy(update={"needs_review": True, "review": review}) if review else st

    def _import_statement(self, data: bytes, kind: ImportKind, bank: str | None, ai_fallback: bool,
                          password: str | None = None) -> dict[str, Any]:
        if kind == "pdf":
            if not data.startswith(PDF_MAGIC):
                raise ServiceError("not_pdf", "The file is not a PDF.")
            from kalan.pdf import extract_text

            text = self._open_pdf(data, password, extract_text)
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
        st = self._save_gate(st)
        st = st.model_copy(update={"transactions": categories.apply(st.transactions, self._user_rules())})
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
        rows = categories.apply(result.transactions, self._user_rules())
        imported, skipped = self.store.add_import(import_id, result.format, meta, rows)
        stored = self.store.import_transactions(import_id)
        return {
            "id": import_id, "format": result.format, "currency": result.currency, "rows": len(result.transactions),
            "imported": imported, "skipped_duplicates": skipped, "months": result.months,
            "detected": result.detected, "transactions": [t.model_dump(mode="json") for t in stored],
        }

    # -- anonymous diagnosis (``POST /diagnose``) -------------------------------------------------

    def diagnose(self, path: str | Path, password: str | None = None) -> dict[str, Any]:
        """Structure-only report of a statement PDF: ``{"report": {...}, "text": "..."}``. Nothing is stored.

        ``text`` is what a user pastes into the GitHub issue form; it holds no text or amount of the statement.
        """
        from kalan.diagnose import as_text, diagnose

        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ServiceError("file_unreadable", f"cannot read {path.name} ({exc.strerror or type(exc).__name__})",
                               {"path": str(path)}) from None
        if len(data) > self.config.max_upload_bytes:
            raise ServiceError("file_too_large", "The file is larger than 10 MB.",
                               {"max_bytes": self.config.max_upload_bytes})
        if not data.startswith(PDF_MAGIC):
            raise ServiceError("not_pdf", "The file is not a PDF.")
        report = self._open_pdf(data, password, diagnose)
        return {"report": report, "text": as_text(report)}

    # -- category rules (``/category-rules``) -----------------------------------------------------

    def _user_rules(self) -> list[categories.Rule]:
        return [categories.Rule.of(r["pattern"], r["category"]) for r in self.store.category_rules()]

    def category_rules(self) -> dict[str, Any]:
        """``GET /category-rules``: the user's rules, oldest first, and the category names a rule may use."""
        return {"items": self.store.category_rules(), "categories": list(CATEGORIES)}

    def add_category_rule(self, pattern: str, category: str, apply: bool = True) -> dict[str, Any]:
        """``POST /category-rules``: ``{"rule": {...}, "applied": n}``; ``apply`` recategorises stored rows
        not categorised by hand."""
        pattern = pattern.strip()
        if not 1 <= len(pattern) <= 80:
            raise ServiceError("invalid_pattern", "The pattern must be 1 to 80 characters.")
        if category not in CATEGORIES:
            raise ServiceError("invalid_category", f"Unknown category {category!r}.",
                               {"categories": list(CATEGORIES)})
        try:
            rule = categories.Rule.of(pattern, category)
        except ValueError:
            raise ServiceError("invalid_pattern", "The pattern has no letters or digits.") from None
        try:
            saved = self.store.add_category_rule(f"cr_{uuid.uuid4().hex[:12]}", pattern, rule.folded, category)
        except DuplicateRuleError as dup:
            raise ServiceError("duplicate_rule", "A rule with this pattern exists.",
                               {"rule_id": dup.existing_id}) from None
        applied = 0
        if apply:
            rules = self._user_rules()

            def pick(tx: Transaction) -> str | None:
                found = categories.categorize(tx, rules)
                return found[0] if found is not None and found[1] == "user_rule" else None

            applied = self.store.recategorize(pick)
        return {"rule": saved, "applied": applied}

    def delete_category_rule(self, rule_id: str) -> None:
        """``DELETE /category-rules/{id}``. Rows it categorised keep their category."""
        if not self.store.delete_category_rule(rule_id):
            raise ServiceError("category_rule_not_found", "No category rule with this id.", {"id": rule_id})

    # -- calculations -----------------------------------------------------------------------------

    def schedule(self, months: int | None = None) -> Schedule:
        """``GET /installments/schedule``."""
        return current_schedule(self.store, months)

    def month(self, month: str | None = None) -> dict[str, Any]:
        """``GET /summary/monthly``: spend, credits and categories of one month (default: this month)."""
        return monthly_summary(
            self.store.all_statements(),
            self.store.loose_transactions(),
            month or month_key(date.today()),
            self.store.get_settings().base_currency,
        )

    def subscriptions(self) -> list[dict[str, Any]]:
        """``GET /subscriptions`` items: subscriptions and bills with expected amounts."""
        return regular(self.store)

    def alerts(self) -> list[Alert]:
        statements = self.store.all_statements()
        return compute_alerts(statements, self.schedule(), regular(self.store),
                              cancelled=list_cancelled(self.store), locale=self.store.get_settings().locale)

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

    # -- insights (task 5b, ``/insights/...``) ----------------------------------------------------

    def _statement_or_latest(self, statement_id: str | None) -> Statement:
        if statement_id is not None:
            return self.statement(statement_id)
        st = latest_statement(self.store.all_statements())
        if st is None:
            raise ServiceError("no_statements", "Upload a statement first.")
        return st

    def minimum_payment(self, statement_id: str | None = None, include_installments: bool = False,
                        ) -> dict[str, Any]:
        """``GET /insights/minimum-payment``: what paying only the minimum costs, cycle by cycle."""
        st = self._statement_or_latest(statement_id)
        charges = None
        if include_installments:
            charges = {m.month: m.total for m in self.schedule(60).months}
        return minimum_payment_plan(st, rule_overrides(self.store), charges)

    def card_fees(self) -> list[dict[str, Any]]:
        """``GET /insights/card-fees``: annual card fee lines, newest first."""
        return find_card_fees(self.store.all_statements())

    def card_fee_prompt(self, transaction_id: str) -> dict[str, Any]:
        """``GET /insights/card-fees/{id}/prompt``: the refund-letter prompt for the user's own AI."""
        fee = next((f for f in self.card_fees() if f["transaction_id"] == transaction_id), None)
        if fee is None:
            raise ServiceError("card_fee_not_found", "No annual card fee line with this id.", {"id": transaction_id})
        return refund_letter_prompt(fee)

    def which_card(self, on: date | None = None) -> dict[str, Any]:
        """``GET /insights/which-card``."""
        return card_options(self.store.all_statements(), today=on)

    def share_card(self, locale: CardLocale = "tr", amounts: bool = False) -> dict[str, Any]:
        """``GET /insights/share-card`` (sentence, month shares and SVG) over the next 12 months."""
        return load_card(self.schedule(12), locale, amounts)

    def _forecast_inputs(self) -> dict[str, Any]:
        store = self.store
        statements, loose, costs = store.all_statements(), store.loose_transactions(), store.fixed_costs()
        return {
            "statements": statements, "loose": loose, "currency": store.get_settings().base_currency,
            "manual_plans": store.manual_plans(), "imported_plans": import_plans(store.import_transactions()),
            "fixed_costs": costs, "model_recurring": store.model_recurring_ids(),
            "fixed_tx_ids": matched_transaction_ids(costs, statements, loose),
        }

    def forecast(self, include_fixed: bool = True) -> dict[str, Any]:
        """``GET /insights/forecast``: open period, next statement forecast, backtest summary, rows per card."""
        store = self.store
        return next_statement_forecast(**self._forecast_inputs(), include_fixed=include_fixed,
                                       closed_by_user=plan_closures(store), expected=expected_amounts(store),
                                       locale=store.get_settings().locale)

    def forecast_backtest(self, include_fixed: bool = True) -> dict[str, Any]:
        """``GET /insights/forecast/backtest``."""
        return backtest(**self._forecast_inputs(), include_fixed=include_fixed)

    def early_payoff(self, plan_id: str | None = None, month: str | None = None) -> dict[str, Any]:
        """``GET /insights/early-payoff`` (every open plan) or ``/insights/early-payoff/{plan_id}``."""
        sched = self.schedule()
        if plan_id is None:
            return payoff_overview(sched, self.store.all_statements())
        if month is not None and month not in {m.month for m in sched.months}:
            raise ServiceError("month_outside_schedule", "The payoff month must be inside the schedule window.",
                               {"start_month": sched.start_month, "months": len(sched.months)})
        result = payoff(sched, plan_id, month)
        if result is None:
            raise ServiceError("plan_not_found", "No open installment plan with this id.", {"id": plan_id})
        return result

    def fixed_cost_matches(self) -> dict[str, Any]:
        """``GET /insights/fixed-costs/matches``."""
        store = self.store
        return fixed_cost_report(store.fixed_costs(), store.all_statements(), store.loose_transactions())

    def export(self, fmt: ExportFormat, what: Literal["transactions", "plans"] = "transactions",
               currency: str | None = None) -> tuple[str, int]:
        """``GET /insights/export``: the file text and the number of rows left out."""
        if what == "plans" and fmt != "csv":
            raise ServiceError("plans_csv_only",
                               "Plans export on their own as CSV; hledger and beancount include them.")
        sched = self.schedule(60)
        return export(fmt, self.store.all_statements(), self.store.loose_transactions(), sched.plans,
                      currency or self.store.get_settings().base_currency, what)

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
