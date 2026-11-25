"""FastAPI application. Every route of ``docs/api.md`` v0.2.0 lives here.

Logs carry ids, counts and error codes only: never descriptions, amounts, file names or model text.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import kalan
from kalan.ai import reader
from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError, ProviderRegistry
from kalan.ai.jobs import JobRunner
from kalan.alerts import compute_alerts
from kalan.amounts import month_key
from kalan.api.errors import ApiError, error_response, install
from kalan.api.insights import router as insights_router
from kalan.config import Config
from kalan.imports import ImportFileError, Mapping, import_plans, parse_import
from kalan.insights.cancellations import list_cancelled
from kalan.models import Direction, FixedCost, InstallmentInfo, Locale, ManualInstallment, Statement, Transaction
from kalan.parsers import ParseError, StatementParser, UnknownFormatError
from kalan.pdf import extract_text
from kalan.pipeline import parse_text
from kalan.redact import redact
from kalan.schedule import Schedule, build_schedule
from kalan.simulate import SimulationIn, simulate
from kalan.store import DuplicateStatementError, Settings, Store
from kalan.summary import attributed, monthly_summary, subscriptions
from kalan.text import normalize_merchant

log = logging.getLogger("kalan.api")

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
PDF_MAGIC = b"%PDF-"
MULTIPART_OVERHEAD = 64 * 1024


# -- request bodies -------------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderSelection(_Body):
    id: str | None


class SettingsUpdate(_Body):
    locale: Locale | None = None
    base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    months_ahead: int | None = Field(default=None, ge=1, le=60)


class FixedCostIn(_Body):
    name: str = Field(min_length=1, max_length=120)
    amount: int = Field(gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    day: int = Field(ge=1, le=31)
    start: str = Field(pattern=MONTH_PATTERN)
    end: str | None = Field(default=None, pattern=MONTH_PATTERN)
    category: str | None = Field(default=None, max_length=40)


class InstallmentIn(_Body):
    total_count: int = Field(ge=1, le=120)
    amount_each: int = Field(ge=1)


class TransactionIn(_Body):
    date: date
    description: str = Field(min_length=1, max_length=200)
    amount: int = Field(ge=1, description="Minor units")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    direction: Direction
    installment: InstallmentIn | None = None


# -- middleware -----------------------------------------------------------------------------------


class PrivateNetworkAccess:
    """Answer Chrome's Private Network Access preflight: ``Access-Control-Allow-Private-Network: true``.

    Only used with Starlette versions whose CORS middleware cannot do it natively.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "OPTIONS":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if b"access-control-request-method" not in headers:
            await self.app(scope, receive, send)
            return

        async def send_with_pna(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 200:
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"access-control-allow-private-network", b"true")]
            await send(message)

        await self.app(scope, receive, send_with_pna)


class UploadLimit:
    """Reject request bodies that announce more than the upload limit before reading them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT"):
            length = dict(scope["headers"]).get(b"content-length")
            if length and length.isdigit() and int(length) > self.max_bytes + MULTIPART_OVERHEAD:
                response = error_response(413, "file_too_large", "The file is larger than 10 MB.",
                                          {"max_bytes": self.max_bytes})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# -- app ------------------------------------------------------------------------------------------


def create_app(
    config: Config | None = None,
    store: Store | None = None,
    registry: ProviderRegistry | None = None,
    job_timeout: float = DEFAULT_TIMEOUT,
) -> FastAPI:
    config = config or Config()
    store = store or Store(config.db_path)
    registry = registry or ProviderRegistry()
    runner = JobRunner(store, timeout=job_timeout)
    interrupted = store.fail_interrupted_jobs()
    if interrupted:
        log.info("marked %d interrupted jobs as failed", interrupted)

    app = FastAPI(title="kalan", version=kalan.__version__, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.registry = registry
    app.state.runner = runner
    app.state.config = config
    install(app)
    app.include_router(insights_router)
    cors: dict[str, Any] = {
        "allow_origins": config.cors_origins,
        "allow_methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["*"],
        "max_age": 600,
    }
    native_pna = "allow_private_network" in inspect.signature(CORSMiddleware.__init__).parameters
    if native_pna:  # Starlette >= 1.0 answers the Private Network Access preflight itself
        cors["allow_private_network"] = True
    app.add_middleware(CORSMiddleware, **cors)
    app.add_middleware(UploadLimit, max_bytes=config.max_upload_bytes)
    if not native_pna:
        app.add_middleware(PrivateNetworkAccess)

    # -- health and setup -------------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": kalan.__version__}

    def providers_body(refresh: bool = False) -> dict[str, Any]:
        found = registry.detect_all(refresh=refresh)
        return {
            "providers": [
                {"id": a.id, "name": a.name, "available": found[a.id].available, "detail": found[a.id].detail}
                for a in registry.adapters
            ],
            "selected": store.selected_provider(),
        }

    @app.get("/providers")
    def get_providers(refresh: bool = False) -> dict[str, Any]:
        return providers_body(refresh)

    @app.put("/providers/selected")
    def put_provider(body: ProviderSelection) -> dict[str, Any]:
        if body.id is not None:
            if registry.get(body.id) is None:
                raise ApiError(422, "unknown_provider", f"There is no provider called {body.id!r}.",
                               {"known": [a.id for a in registry.adapters]})
            detection = registry.detect_all(refresh=True)[body.id]
            if not detection.available:
                raise ApiError(409, "provider_unavailable", "This provider is not available on this computer.",
                               {"id": body.id, "detail": detection.detail})
        store.set_selected_provider(body.id)
        log.info("provider selected: %s", body.id)
        return providers_body()

    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        return store.get_settings().model_dump()

    @app.put("/settings")
    def put_settings(body: SettingsUpdate) -> dict[str, Any]:
        merged = store.get_settings().model_dump() | body.model_dump(exclude_none=True)
        return store.put_settings(Settings.model_validate(merged)).model_dump()

    # -- statements -------------------------------------------------------------------------------

    def statement_or_404(statement_id: str) -> Statement:
        st = store.get_statement(statement_id)
        if st is None:
            raise ApiError(404, "statement_not_found", "No statement with this id.", {"id": statement_id})
        return st

    def selected_adapter() -> Adapter:
        provider_id = store.selected_provider()
        if provider_id is None:
            raise ApiError(409, "no_provider", "Choose an AI provider first (PUT /providers/selected).")
        adapter = registry.get(provider_id)
        detection = registry.detect_all().get(provider_id)
        if adapter is None or detection is None or not detection.available:
            raise ApiError(409, "provider_unavailable", "The selected provider is not available on this computer.",
                           {"id": provider_id, "detail": detection.detail if detection else "unknown provider"})
        return adapter

    def ai_read(text: str) -> tuple[Statement, str]:
        """AI fallback for a statement no parser recognised. Returns the statement and its redacted text."""
        try:
            adapter = selected_adapter()
        except ApiError as exc:
            raise ApiError(422, "unknown_format", "No supported bank statement format was recognised.",
                           {"ai_fallback": exc.code}) from None
        redacted = redact(text)
        try:
            st = reader.read_statement(adapter, redacted.text, redacted.card_last4, timeout=job_timeout)
        except reader.TextTooLongError:
            raise ApiError(422, "unknown_format", "No supported format was recognised and the statement is too "
                           "long for the AI reader.", {"ai_fallback": "too_long"}) from None
        except AdapterError as exc:
            raise ApiError(502, "ai_fallback_failed", f"{adapter.name}: {exc}", {"provider": adapter.id}) from None
        except reader.InvalidModelOutputError as exc:
            raise ApiError(502, "ai_fallback_failed", f"{adapter.name} returned an invalid answer twice: {exc}",
                           {"provider": adapter.id}) from None
        return st, redacted.text

    @app.post("/statements", status_code=201)
    def post_statement(
        file: Annotated[UploadFile, File()], ai_fallback: bool = True
    ) -> dict[str, Any]:
        data = file.file.read(config.max_upload_bytes + 1)
        if len(data) > config.max_upload_bytes:
            raise ApiError(413, "file_too_large", "The file is larger than 10 MB.",
                           {"max_bytes": config.max_upload_bytes})
        if not data.startswith(PDF_MAGIC):
            raise ApiError(415, "not_pdf", "The file is not a PDF.")
        try:
            text = extract_text(data)
        except Exception as exc:  # broken or encrypted PDF
            log.info("upload rejected: %s", type(exc).__name__)
            raise ApiError(422, "invalid_pdf", "The PDF could not be opened.") from None
        kept_text: str | None = None
        try:
            st = parse_text(text)
        except UnknownFormatError:
            if not ai_fallback:
                raise ApiError(422, "unknown_format", "No supported bank statement format was recognised.",
                               {"ai_fallback": "off"}) from None
            st, kept_text = ai_read(text)
        except ParseError:
            raise ApiError(422, "parse_error", "The statement was recognised but could not be read.") from None
        except Exception as exc:
            log.info("upload rejected: %s", type(exc).__name__)
            raise ApiError(422, "parse_error", "The statement was recognised but could not be read.") from None
        try:
            saved = store.add_statement(st, redacted_text=kept_text)
        except DuplicateStatementError as dup:
            raise ApiError(409, "duplicate", "This statement is already stored.",
                           {"statement_id": dup.existing_id}) from None
        log.info("statement %s stored: %d transactions, reconciliation %s", saved.id, len(saved.transactions),
                 saved.reconciliation.status if saved.reconciliation else "none")
        return saved.model_dump(mode="json")

    @app.get("/statements")
    def list_statements() -> dict[str, Any]:
        return {"items": [s.model_dump(mode="json") for s in store.list_statements()]}

    @app.get("/statements/{statement_id}")
    def get_statement(statement_id: str) -> dict[str, Any]:
        return statement_or_404(statement_id).model_dump(mode="json")

    @app.delete("/statements/{statement_id}", status_code=204)
    def delete_statement(statement_id: str) -> Response:
        if not store.delete_statement(statement_id):
            raise ApiError(404, "statement_not_found", "No statement with this id.", {"id": statement_id})
        log.info("statement %s deleted", statement_id)
        return Response(status_code=204)

    # -- manual entry -----------------------------------------------------------------------------

    @app.post("/transactions", status_code=201)
    def post_transaction(body: TransactionIn) -> dict[str, Any]:
        tx_id = StatementParser.stable_id("tx", "manual", uuid.uuid4().hex)
        plan = None
        info = None
        if body.installment is not None:
            if body.direction != "debit":
                raise ApiError(422, "invalid_installment", "Only a debit can be paid in installments.")
            plan = ManualInstallment(
                id=StatementParser.stable_id("pl", "manual", tx_id),
                description=body.description,
                start_date=body.date,
                total_count=body.installment.total_count,
                amount_each=body.installment.amount_each,
                currency=body.currency,
            )
            info = InstallmentInfo(
                remaining_count=plan.total_count,
                remaining_amount_each=plan.amount_each,
                is_last=False,
                plan_id=plan.id,
                total_count=plan.total_count,
            )
        tx = Transaction(
            id=tx_id,
            date=body.date,
            description=body.description,
            amount=body.amount,
            currency=body.currency,
            direction=body.direction,
            kind="purchase" if body.direction == "debit" else "refund",
            installment=info,
            merchant=normalize_merchant(body.description) or None,
            source="manual",
        )
        saved = store.add_manual(tx, plan)
        log.info("manual transaction %s stored (plan: %s)", saved.id, plan.id if plan else None)
        return saved.model_dump(mode="json")

    @app.get("/transactions")
    def list_transactions(
        month: Annotated[str | None, Query(pattern=MONTH_PATTERN)] = None,
        source: Literal["manual", "statement", "import"] | None = None,
    ) -> dict[str, Any]:
        rows = attributed(store.all_statements(), store.loose_transactions())
        items = [tx for m, tx in rows if (month is None or m == month) and (source is None or tx.source == source)]
        items.sort(key=lambda t: t.date)  # stable: printed order within a day
        return {"items": [tx.model_dump(mode="json") for tx in items]}

    @app.delete("/transactions/{tx_id}", status_code=204)
    def delete_transaction(tx_id: str) -> Response:
        result = store.delete_manual_transaction(tx_id)
        if result == "missing":
            raise ApiError(404, "transaction_not_found", "No transaction with this id.", {"id": tx_id})
        if result == "not_manual":
            raise ApiError(409, "not_manual", "Only manually entered transactions can be deleted; "
                                              "delete the statement instead.", {"id": tx_id})
        log.info("manual transaction %s deleted", tx_id)
        return Response(status_code=204)

    # -- imports ----------------------------------------------------------------------------------

    @app.post("/imports", status_code=201)
    def post_import(
        file: Annotated[UploadFile, File()],
        mapping: Annotated[str | None, Form()] = None,
        format: Annotated[Literal["csv", "ofx", "qif"] | None, Form()] = None,
    ) -> dict[str, Any]:
        data = file.file.read(config.max_upload_bytes + 1)
        if len(data) > config.max_upload_bytes:
            raise ApiError(413, "file_too_large", "The file is larger than 10 MB.",
                           {"max_bytes": config.max_upload_bytes})
        try:
            parsed_mapping = Mapping.model_validate_json(mapping) if mapping else Mapping()
        except ValidationError as exc:
            raise ApiError(422, "invalid_mapping", "The mapping is not valid.",
                           {"errors": [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()][:5]}
                           ) from None
        try:
            result = parse_import(data, file.filename, parsed_mapping, store.get_settings().base_currency, format)
        except ImportFileError as exc:
            raise ApiError(422, "import_unreadable", f"The file could not be imported: {exc}") from None
        import_id = StatementParser.stable_id("im", uuid.uuid4().hex)
        meta = {"currency": result.currency, "detected": result.detected, "months": result.months}
        imported, skipped = store.add_import(import_id, result.format, meta, result.transactions)
        log.info("import %s (%s): %d rows, %d skipped as duplicates", import_id, result.format, imported, skipped)
        stored = {t.id for t in store.import_transactions(import_id)}
        return {
            "id": import_id, "format": result.format, "currency": result.currency, "rows": len(result.transactions),
            "imported": imported, "skipped_duplicates": skipped, "months": result.months,
            "detected": result.detected,
            "transactions": [t.model_dump(mode="json") for t in store.import_transactions(import_id)
                             if t.id in stored],
        }

    @app.get("/imports")
    def list_imports() -> dict[str, Any]:
        return {"items": store.list_imports()}

    @app.delete("/imports/{import_id}", status_code=204)
    def delete_import(import_id: str) -> Response:
        if not store.delete_import(import_id):
            raise ApiError(404, "import_not_found", "No import with this id.", {"id": import_id})
        return Response(status_code=204)

    # -- fixed costs ------------------------------------------------------------------------------

    @app.post("/fixed-costs", status_code=201)
    def post_fixed_cost(body: FixedCostIn) -> dict[str, Any]:
        if body.end is not None and body.end < body.start:
            raise ApiError(422, "invalid_period", "end must not be before start.")
        cost = FixedCost(
            id=StatementParser.stable_id("fc", uuid.uuid4().hex),
            currency=body.currency or store.get_settings().base_currency,
            **body.model_dump(exclude={"currency"}),
        )
        saved = store.add_fixed_cost(cost)
        log.info("fixed cost %s stored", saved.id)
        return saved.model_dump(mode="json")

    @app.get("/fixed-costs")
    def list_fixed_costs() -> dict[str, Any]:
        return {"items": [c.model_dump(mode="json") for c in store.fixed_costs()]}

    @app.delete("/fixed-costs/{cost_id}", status_code=204)
    def delete_fixed_cost(cost_id: str) -> Response:
        if not store.delete_fixed_cost(cost_id):
            raise ApiError(404, "fixed_cost_not_found", "No fixed cost with this id.", {"id": cost_id})
        return Response(status_code=204)

    # -- calculations -----------------------------------------------------------------------------

    @app.get("/installments/schedule")
    def schedule(months: Annotated[int | None, Query(ge=1, le=60)] = None) -> dict[str, Any]:
        return current_schedule(months).model_dump(mode="json")

    @app.post("/simulate/installment")
    def simulate_installment(body: SimulationIn) -> dict[str, Any]:
        return simulate(body, current_schedule(None))

    def current_schedule(months: int | None) -> Schedule:
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

    @app.get("/summary/monthly")
    def summary(month: Annotated[str | None, Query(pattern=MONTH_PATTERN)] = None) -> dict[str, Any]:
        return monthly_summary(
            store.all_statements(),
            store.loose_transactions(),
            month or month_key(date.today()),
            store.get_settings().base_currency,
        )

    @app.get("/subscriptions")
    def get_subscriptions() -> dict[str, Any]:
        items = subscriptions(store.all_statements(), store.loose_transactions(), store.model_recurring_ids())
        return {"items": items}

    @app.get("/alerts")
    def get_alerts() -> dict[str, Any]:
        statements = store.all_statements()
        subs = subscriptions(statements, store.loose_transactions(), store.model_recurring_ids())
        alerts = compute_alerts(statements, current_schedule(None), subs, cancelled=list_cancelled(store))
        return {"items": [a.model_dump(mode="json") for a in alerts]}

    # -- model analysis ---------------------------------------------------------------------------

    @app.post("/statements/{statement_id}/template-draft")
    def template_draft(statement_id: str) -> dict[str, Any]:
        statement_or_404(statement_id)
        text = store.statement_text(statement_id)
        if text is None:
            raise ApiError(409, "no_statement_text", "Only statements read by the AI fallback keep their text.",
                           {"id": statement_id})
        adapter = selected_adapter()
        try:
            result = reader.draft_template(adapter, text, timeout=job_timeout)
        except AdapterError as exc:
            raise ApiError(502, "provider_failed", f"{adapter.name}: {exc}", {"provider": adapter.id}) from None
        log.info("template draft for %s via %s: valid=%s", statement_id, adapter.id, result["valid"])
        return {"provider": adapter.id, **result}

    @app.post("/statements/{statement_id}/analyze", status_code=202)
    def analyze(statement_id: str) -> dict[str, Any]:
        statement_or_404(statement_id)
        adapter = selected_adapter()
        job = runner.start(statement_id, adapter)
        return {"job_id": job.id, "status": job.status}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise ApiError(404, "job_not_found", "No job with this id.", {"id": job_id})
        return {"job_id": job.id, "statement_id": job.statement_id, "status": job.status, "error": job.error}

    @app.get("/statements/{statement_id}/analysis")
    def get_analysis(statement_id: str) -> dict[str, Any]:
        statement_or_404(statement_id)
        analysis = store.get_analysis(statement_id)
        if analysis is None:
            raise ApiError(404, "analysis_not_found", "This statement has not been analysed yet.",
                           {"id": statement_id})
        return analysis

    return app
