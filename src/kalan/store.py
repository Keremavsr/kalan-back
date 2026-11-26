"""SQLite storage (stdlib ``sqlite3``).

One file per user (see :mod:`kalan.config`). Every call opens its own short-lived connection, so the
store is safe to use from the API threads and the analysis job threads at the same time.

Only redacted data reaches this module: statements come out of :func:`kalan.pipeline.parse_pdf`
(redaction runs before parsing) and manual descriptions are redacted here before insert.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kalan import cards
from kalan.amounts import month_key
from kalan.models import FixedCost, Locale, ManualInstallment, Statement, StatementSummary, Transaction
from kalan.procs import pid_alive
from kalan.redact import redact_text
from kalan.text import merchant_fields

_MIGRATIONS_PACKAGE = "kalan.migrations"

JobStatus = Literal["pending", "done", "failed"]
JOB_STALE_AFTER = timedelta(minutes=5)
"""A pending job whose owner has not shown it is alive for this long counts as interrupted (heartbeat every 30 s)."""
READ_ONLY_TX = frozenset({"card_id", "card_label", "category_source"})
"""Transaction fields filled when read, never stored with the row."""
READ_ONLY_STATEMENT = frozenset({"card_id", "card_label"})


class DuplicateStatementError(Exception):
    def __init__(self, existing_id: str) -> None:
        super().__init__(existing_id)
        self.existing_id = existing_id


class DuplicateRuleError(Exception):
    def __init__(self, existing_id: str) -> None:
        super().__init__(existing_id)
        self.existing_id = existing_id


class Settings(BaseModel):
    locale: Locale = "tr"
    base_currency: str = Field(default="TRY", pattern=r"^[A-Z]{3}$")
    months_ahead: int = Field(default=12, ge=1, le=60)


class Job(BaseModel):
    id: str
    statement_id: str
    provider: str
    status: JobStatus
    error: str | None = None
    created_at: str
    finished_at: str | None = None
    kind: Literal["statement", "analysis_all"] = "statement"
    done: int = 0
    total: int = 1
    failed: int = 0
    phase: Literal["statements", "suggestions", "done"] = "statements"
    owner_pid: int | None = None
    heartbeat_at: str | None = None

    def body(self) -> dict[str, Any]:
        """``GET /jobs/{id}`` shape."""
        return {"job_id": self.id, "kind": self.kind, "statement_id": self.statement_id or None,
                "status": self.status, "phase": self.phase, "done": self.done, "total": self.total,
                "failed": self.failed, "error": self.error}


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def migrations() -> list[tuple[int, str, str]]:
    """``(number, name, sql)`` of every bundled migration, in order."""
    out = []
    for entry in resources.files(_MIGRATIONS_PACKAGE).iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            out.append((int(name[:4]), name, entry.read_text(encoding="utf-8")))
    return sorted(out)


def statement_month(st: Statement) -> str:
    """Month a statement belongs to: its cut-off month (``period_end``)."""
    return month_key(st.period_end)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    # -- plumbing ---------------------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    def schema_version(self) -> int:
        with self._connect() as con:
            return con.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        """Apply pending migrations; return the schema version."""
        current = self.schema_version()
        for number, _name, sql in migrations():
            if number <= current:
                continue
            with self._connect() as con:
                # executescript commits on its own, so the version bump is part of the same script.
                con.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
            current = number
        return current

    # -- statements -------------------------------------------------------------------------------

    def add_statement(self, st: Statement, redacted_text: str | None = None) -> Statement:
        """Store a parsed statement. Raise :class:`DuplicateStatementError` if the period is already stored.

        ``redacted_text`` (AI fallback only) is kept for a later template draft.
        """
        data = st.model_dump(mode="json", exclude={"transactions", "analysis_status", *READ_ONLY_STATEMENT})
        with self._tx() as con:
            row = con.execute(
                "SELECT id FROM statements WHERE id = ? OR (bank = ? AND card_last4 IS ? AND period_start = ? "
                "AND period_end = ?)",
                (st.id, st.bank, st.card_last4, st.period_start.isoformat(), st.period_end.isoformat()),
            ).fetchone()
            if row is not None:
                raise DuplicateStatementError(row["id"])
            con.execute(
                "INSERT INTO statements (id, bank, card_last4, period_start, period_end, data, analysis_status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, 'none', ?)",
                (st.id, st.bank, st.card_last4, st.period_start.isoformat(), st.period_end.isoformat(),
                 json.dumps(data, ensure_ascii=False), now_iso()),
            )
            for pos, tx in enumerate(st.transactions):
                self._insert_tx(con, tx, st.id, pos)
            if redacted_text is not None:
                con.execute("INSERT INTO statement_texts (statement_id, text) VALUES (?, ?)", (st.id, redacted_text))
        return self.get_statement(st.id)  # type: ignore[return-value]

    def statement_text(self, statement_id: str) -> str | None:
        with self._connect() as con:
            row = con.execute("SELECT text FROM statement_texts WHERE statement_id = ?", (statement_id,)).fetchone()
            return None if row is None else row["text"]

    @staticmethod
    def _insert_tx(con: sqlite3.Connection, tx: Transaction, statement_id: str | None, pos: int) -> None:
        data = tx.model_dump(mode="json", exclude={"category", *READ_ONLY_TX})
        source = None if tx.category is None else ("user" if tx.source == "manual" else "rule")
        con.execute(
            "INSERT INTO transactions (id, statement_id, source, date, position, data, category, category_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tx.id, statement_id, tx.source, tx.date.isoformat(), pos, json.dumps(data, ensure_ascii=False),
             tx.category, source),
        )

    @staticmethod
    def _tx_from_row(row: sqlite3.Row, card: tuple[str, str] | None = None) -> Transaction:
        data = json.loads(row["data"])
        for key in READ_ONLY_TX:
            data.pop(key, None)
        data["category"] = row["category"]
        if row["category"] is not None:
            data["category_source"] = row["category_source"]
        if data.get("merchant") is not None or data.get("merchant_unreadable"):
            data.update(merchant_fields(data["description"]))  # stored rows get today's cleaner
        if card is not None:
            data["card_id"], data["card_label"] = card
        return Transaction.model_validate(data)

    def card_labels(self, con: sqlite3.Connection | None = None) -> dict[str, str]:
        """``{card_id: label}`` of every card with a stored statement (product of its newest statement)."""
        if con is None:
            with self._connect() as c:
                return self.card_labels(c)
        rows = con.execute(
            "SELECT bank, card_last4, json_extract(data, '$.product') AS product FROM statements "
            "ORDER BY period_end, id"
        ).fetchall()
        names = self.card_names(con)
        out: dict[str, str] = {}
        for r in rows:
            cid = cards.card_id(r["bank"], r["card_last4"])
            out[cid] = cards.label(r["bank"], r["product"], r["card_last4"], names.get(cid))
        return out

    def card_names(self, con: sqlite3.Connection | None = None) -> dict[str, str]:
        if con is None:
            return self._get_setting(cards.NAMES_KEY) or {}
        row = con.execute("SELECT value FROM settings WHERE key = ?", (cards.NAMES_KEY,)).fetchone()
        return {} if row is None else (json.loads(row["value"]) or {})

    def set_card_name(self, card_id: str, name: str | None) -> None:
        names = self.card_names()
        if name:
            names[card_id] = redact_text(name)
        else:
            names.pop(card_id, None)
        self._put_setting(cards.NAMES_KEY, names)

    def _statement_from_row(
        self, con: sqlite3.Connection, row: sqlite3.Row, with_tx: bool, labels: dict[str, str] | None = None
    ) -> Statement:
        data = json.loads(row["data"])
        for key in READ_ONLY_STATEMENT:
            data.pop(key, None)
        data["analysis_status"] = row["analysis_status"]
        labels = labels if labels is not None else self.card_labels(con)
        cid = cards.card_id(row["bank"], row["card_last4"])
        card = (cid, labels.get(cid) or cards.label(row["bank"], data.get("product"), row["card_last4"]))
        data["card_id"], data["card_label"] = card
        if with_tx:
            rows = con.execute(
                "SELECT data, category, category_source FROM transactions WHERE statement_id = ? ORDER BY position",
                (row["id"],),
            ).fetchall()
            data["transactions"] = [self._tx_from_row(r, card).model_dump() for r in rows]
        return Statement.model_validate(data)

    def get_statement(self, statement_id: str) -> Statement | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM statements WHERE id = ?", (statement_id,)).fetchone()
            return None if row is None else self._statement_from_row(con, row, with_tx=True)

    def list_statements(self) -> list[StatementSummary]:
        """Newest first (by cut-off date, then upload time)."""
        with self._connect() as con:
            rows = con.execute("SELECT * FROM statements ORDER BY period_end DESC, created_at DESC, id").fetchall()
            labels = self.card_labels(con)
            out = []
            for row in rows:
                st = self._statement_from_row(con, row, with_tx=False, labels=labels)
                out.append(StatementSummary.model_validate(st.model_dump(include=set(StatementSummary.model_fields))))
            return out

    def all_statements(self) -> list[Statement]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM statements ORDER BY period_end, id").fetchall()
            labels = self.card_labels(con)
            return [self._statement_from_row(con, row, with_tx=True, labels=labels) for row in rows]

    def delete_statement(self, statement_id: str) -> bool:
        with self._tx() as con:
            return con.execute("DELETE FROM statements WHERE id = ?", (statement_id,)).rowcount > 0

    def set_analysis_status(self, statement_id: str, status: str) -> None:
        with self._tx() as con:
            con.execute("UPDATE statements SET analysis_status = ? WHERE id = ?", (status, statement_id))

    # -- manual transactions ----------------------------------------------------------------------

    def add_manual(self, tx: Transaction, plan: ManualInstallment | None = None) -> Transaction:
        tx = tx.model_copy(update={"description": redact_text(tx.description), "source": "manual"})
        with self._tx() as con:
            self._insert_tx(con, tx, None, 0)
            if plan is not None:
                plan = plan.model_copy(update={"description": tx.description})
                con.execute(
                    "INSERT INTO manual_plans (id, transaction_id, data) VALUES (?, ?, ?)",
                    (plan.id, tx.id, json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)),
                )
        return tx

    def manual_plans(self) -> list[ManualInstallment]:
        with self._connect() as con:
            rows = con.execute("SELECT data FROM manual_plans ORDER BY id").fetchall()
            return [ManualInstallment.model_validate_json(r["data"]) for r in rows]

    def manual_transactions(self) -> list[Transaction]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT data, category, category_source FROM transactions WHERE source = 'manual' ORDER BY date, id"
            ).fetchall()
            return [self._tx_from_row(r) for r in rows]

    # -- imports ----------------------------------------------------------------------------------

    def add_import(self, import_id: str, fmt: str, meta: dict[str, Any], txs: list[Transaction]) -> tuple[int, int]:
        """Store an import; rows already stored (same stable id) are skipped. Returns (imported, skipped)."""
        imported = skipped = 0
        with self._tx() as con:
            con.execute("INSERT INTO imports (id, format, data, created_at) VALUES (?, ?, '{}', ?)",
                        (import_id, fmt, now_iso()))
            for pos, tx in enumerate(txs):
                tx = tx.model_copy(update={"description": redact_text(tx.description), "source": "import"})
                data = tx.model_dump(mode="json", exclude={"category", *READ_ONLY_TX})
                cur = con.execute(
                    "INSERT OR IGNORE INTO import_transactions (id, import_id, date, position, data, category, "
                    "category_source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tx.id, import_id, tx.date.isoformat(), pos, json.dumps(data, ensure_ascii=False),
                     tx.category, None if tx.category is None else "rule"),
                )
                if cur.rowcount:
                    imported += 1
                else:
                    skipped += 1
            meta = {**meta, "rows": len(txs), "imported": imported, "skipped_duplicates": skipped}
            con.execute("UPDATE imports SET data = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False), import_id))
        return imported, skipped

    def list_imports(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM imports ORDER BY created_at DESC, id").fetchall()
            return [{"id": r["id"], "format": r["format"], "created_at": r["created_at"], **json.loads(r["data"])}
                    for r in rows]

    def import_transactions(self, import_id: str | None = None) -> list[Transaction]:
        sql = "SELECT data, category, category_source FROM import_transactions"
        args: tuple[str, ...] = ()
        if import_id is not None:
            sql, args = sql + " WHERE import_id = ?", (import_id,)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY date, import_id, position", args).fetchall()
            return [self._tx_from_row(r) for r in rows]

    def delete_import(self, import_id: str) -> bool:
        with self._tx() as con:
            return con.execute("DELETE FROM imports WHERE id = ?", (import_id,)).rowcount > 0

    # -- fixed costs ------------------------------------------------------------------------------

    def add_fixed_cost(self, cost: FixedCost) -> FixedCost:
        cost = cost.model_copy(update={"name": redact_text(cost.name)})
        with self._tx() as con:
            con.execute("INSERT INTO fixed_costs (id, data, created_at) VALUES (?, ?, ?)",
                        (cost.id, json.dumps(cost.model_dump(mode="json"), ensure_ascii=False), now_iso()))
        return cost

    def fixed_costs(self) -> list[FixedCost]:
        with self._connect() as con:
            rows = con.execute("SELECT data FROM fixed_costs ORDER BY created_at, id").fetchall()
            return [FixedCost.model_validate_json(r["data"]) for r in rows]

    def delete_fixed_cost(self, cost_id: str) -> bool:
        with self._tx() as con:
            return con.execute("DELETE FROM fixed_costs WHERE id = ?", (cost_id,)).rowcount > 0

    def loose_transactions(self) -> list[Transaction]:
        """Manual and imported rows: everything that is not part of a statement."""
        return sorted(self.manual_transactions() + self.import_transactions(), key=lambda t: (t.date, t.id))

    def get_transaction(self, tx_id: str) -> Transaction | None:
        with self._connect() as con:
            row = con.execute("SELECT data, category, category_source FROM transactions WHERE id = ?",
                              (tx_id,)).fetchone()
            return None if row is None else self._tx_from_row(row)

    def delete_manual_transaction(self, tx_id: str) -> Literal["deleted", "missing", "not_manual"]:
        with self._tx() as con:
            row = con.execute("SELECT source FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if row is None:
                return "missing"
            if row["source"] != "manual":
                return "not_manual"
            con.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            return "deleted"

    def model_recurring_ids(self) -> set[str]:
        with self._connect() as con:
            rows = con.execute("SELECT id FROM transactions WHERE model_recurring = 1").fetchall()
            return {r["id"] for r in rows}

    # -- category rules -------------------------------------------------------------------------

    def category_rules(self) -> list[dict[str, str]]:
        """User rules, oldest first: ``{"id", "pattern", "category", "created_at"}``."""
        with self._connect() as con:
            rows = con.execute("SELECT * FROM category_rules ORDER BY created_at, id").fetchall()
            return [{"id": r["id"], "pattern": r["pattern"], "category": r["category"], "created_at": r["created_at"]}
                    for r in rows]

    def add_category_rule(self, rule_id: str, pattern: str, folded: str, category: str) -> dict[str, str]:
        """Raise :class:`DuplicateRuleError` when a rule with the same folded pattern exists."""
        with self._tx() as con:
            row = con.execute("SELECT id FROM category_rules WHERE folded = ?", (folded,)).fetchone()
            if row is not None:
                raise DuplicateRuleError(row["id"])
            created = now_iso()
            con.execute("INSERT INTO category_rules (id, pattern, folded, category, created_at) VALUES (?, ?, ?, ?, ?)",
                        (rule_id, pattern, folded, category, created))
        return {"id": rule_id, "pattern": pattern, "category": category, "created_at": created}

    def delete_category_rule(self, rule_id: str) -> bool:
        with self._tx() as con:
            return con.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,)).rowcount > 0

    def fill_missing_categories(self, pick: Callable[[Transaction], str | None]) -> int:
        """Set ``pick(tx)`` on stored rows that still have no category (start-up pass for better rules)."""
        changed = 0
        with self._tx() as con:
            for table in ("transactions", "import_transactions"):
                rows = con.execute(f"SELECT id, data, category, category_source FROM {table} "
                                   "WHERE category IS NULL").fetchall()
                for row in rows:
                    category = pick(self._tx_from_row(row))
                    if category is not None:
                        con.execute(f"UPDATE {table} SET category = ?, category_source = 'rule' WHERE id = ?",
                                    (category, row["id"]))
                        changed += 1
        return changed

    def recategorize(self, pick: Callable[[Transaction], str | None]) -> int:
        """Set ``pick(tx)`` as the category of every stored row the user did not categorise by hand.

        ``pick`` returns the new category or None to leave the row alone. Returns the number of rows changed.
        """
        changed = 0
        with self._tx() as con:
            for table in ("transactions", "import_transactions"):
                rows = con.execute(
                    f"SELECT id, data, category, category_source FROM {table} "
                    "WHERE category_source IS NULL OR category_source <> 'user'"
                ).fetchall()
                for row in rows:
                    tx = self._tx_from_row(row)
                    category = pick(tx)
                    if category is not None and category != row["category"]:
                        con.execute(f"UPDATE {table} SET category = ?, category_source = 'rule' WHERE id = ?",
                                    (category, row["id"]))
                        changed += 1
        return changed

    # -- analysis ---------------------------------------------------------------------------------

    def save_analysis(
        self,
        statement_id: str,
        provider: str,
        categories: dict[str, str],
        recurring: set[str],
        commentary: dict[str, Any],
    ) -> bool:
        """Apply categories and store the commentary in one transaction. False if the statement is gone."""
        with self._tx() as con:
            if con.execute("SELECT 1 FROM statements WHERE id = ?", (statement_id,)).fetchone() is None:
                return False
            for tx_id, category in categories.items():  # only what no rule and no user has categorised
                con.execute(
                    "UPDATE transactions SET category = ?, category_source = 'model' "
                    "WHERE id = ? AND statement_id = ? AND category IS NULL",
                    (category, tx_id, statement_id),
                )
            con.execute(
                "UPDATE transactions SET model_recurring = (id IN (SELECT value FROM json_each(?))) "
                "WHERE statement_id = ?",
                (json.dumps(sorted(recurring)), statement_id),
            )
            con.execute(
                "INSERT OR REPLACE INTO analyses (statement_id, provider, data, created_at) VALUES (?, ?, ?, ?)",
                (statement_id, provider, json.dumps(commentary, ensure_ascii=False), now_iso()),
            )
            con.execute("UPDATE statements SET analysis_status = 'done' WHERE id = ?", (statement_id,))
        return True

    def get_analysis(self, statement_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM analyses WHERE statement_id = ?", (statement_id,)).fetchone()
            if row is None:
                return None
            return {
                "categories_applied": True,
                "commentary": json.loads(row["data"]),
                "provider": row["provider"],
                "created_at": row["created_at"],
            }

    # -- jobs -------------------------------------------------------------------------------------

    def create_job(self, job_id: str, statement_id: str, provider: str) -> Job:
        """A pending statement job owned by this process."""
        job = Job(id=job_id, statement_id=statement_id, provider=provider, status="pending", created_at=now_iso(),
                  owner_pid=os.getpid())
        job.heartbeat_at = job.created_at
        with self._tx() as con:
            con.execute(
                "INSERT INTO jobs (id, statement_id, provider, status, created_at, owner_pid, heartbeat_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (job.id, statement_id, provider, job.created_at, job.owner_pid, job.heartbeat_at),
            )
            con.execute("UPDATE statements SET analysis_status = 'pending' WHERE id = ?", (statement_id,))
        return job

    def finish_job(self, job_id: str, status: JobStatus, error: str | None = None) -> None:
        with self._tx() as con:
            con.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ?, phase = 'done', "
                "done = CASE WHEN kind = 'statement' AND ? = 'done' THEN 1 ELSE done END WHERE id = ?",
                (status, error, now_iso(), status, job_id),
            )
            if status == "failed":
                con.execute(
                    "UPDATE statements SET analysis_status = 'failed' WHERE id = "
                    "(SELECT statement_id FROM jobs WHERE id = ?)",
                    (job_id,),
                )

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return None if row is None else Job.model_validate(dict(row))

    def create_batch_job(self, job_id: str, provider: str, total: int) -> Job:
        job = Job(id=job_id, statement_id="", provider=provider, status="pending", created_at=now_iso(),
                  kind="analysis_all", total=total, owner_pid=os.getpid())
        job.heartbeat_at = job.created_at
        with self._tx() as con:
            con.execute(
                "INSERT INTO jobs (id, statement_id, provider, status, created_at, kind, total, owner_pid, "
                "heartbeat_at) VALUES (?, '', ?, 'pending', ?, 'analysis_all', ?, ?, ?)",
                (job.id, provider, job.created_at, total, job.owner_pid, job.heartbeat_at),
            )
        return job

    def update_job(self, job_id: str, **fields: int | str) -> None:
        """Progress of a batch job: any of ``done``, ``total``, ``failed``, ``phase``. Also a heartbeat."""
        allowed = {"done", "total", "failed", "phase"}
        sets = [k for k in fields if k in allowed]
        if not sets:
            return
        with self._tx() as con:
            con.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in sets)}, heartbeat_at = ? WHERE id = ?",
                        (*(fields[k] for k in sets), now_iso(), job_id))

    def heartbeat(self, job_ids: list[str]) -> None:
        """The owner of these pending jobs is alive (see :data:`JOB_STALE_AFTER`)."""
        if not job_ids:
            return
        with self._tx() as con:
            con.executemany("UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status = 'pending'",
                            [(now_iso(), job_id) for job_id in job_ids])

    @staticmethod
    def job_alive(job: Job, now: datetime | None = None, stale_after: timedelta = JOB_STALE_AFTER) -> bool:
        """A pending job someone is still running: its owner process exists and showed it is alive recently.
        Jobs from before ownership was recorded have no owner and count as dead."""
        if job.status != "pending" or job.owner_pid is None or not pid_alive(job.owner_pid):
            return False
        beat = job.heartbeat_at or job.created_at
        return (now or datetime.now(UTC)) - parse_iso(beat) <= stale_after

    def latest_batch_job(self, pending_only: bool = False) -> Job | None:
        sql = "SELECT * FROM jobs WHERE kind = 'analysis_all'"
        if pending_only:
            sql += " AND status = 'pending'"
        with self._connect() as con:
            row = con.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
            return None if row is None else Job.model_validate(dict(row))

    def pending_job_for(self, statement_id: str) -> Job | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE statement_id = ? AND status = 'pending' AND kind = 'statement' "
                "ORDER BY created_at DESC LIMIT 1",
                (statement_id,),
            ).fetchone()
            return None if row is None else Job.model_validate(dict(row))

    def fail_interrupted_jobs(self, now: datetime | None = None, stale_after: timedelta = JOB_STALE_AFTER) -> int:
        """Mark pending jobs failed whose owner process is gone or silent for longer than ``stale_after``.

        Another live process using the same database keeps its jobs. When no pending job is left afterwards, a
        statement still marked ``pending`` (a batch stopped in the middle of it) becomes ``failed`` so a later
        batch picks it up again."""
        with self._tx() as con:
            rows = [Job.model_validate(dict(r)) for r in con.execute(
                "SELECT * FROM jobs WHERE status = 'pending'").fetchall()]
            ids = [job.id for job in rows if not self.job_alive(job, now, stale_after)]
            for job_id in ids:
                con.execute(
                    "UPDATE jobs SET status = 'failed', error = 'interrupted: the service stopped before the job "
                    "finished', finished_at = ? WHERE id = ?",
                    (now_iso(), job_id),
                )
                con.execute(
                    "UPDATE statements SET analysis_status = 'failed' WHERE id = "
                    "(SELECT statement_id FROM jobs WHERE id = ?)",
                    (job_id,),
                )
            if len(ids) == len(rows):
                con.execute("UPDATE statements SET analysis_status = 'failed' WHERE analysis_status = 'pending'")
            return len(ids)

    # -- settings ---------------------------------------------------------------------------------

    def _get_setting(self, key: str) -> Any:
        with self._connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return None if row is None else json.loads(row["value"])

    def _put_setting(self, key: str, value: Any) -> None:
        with self._tx() as con:
            con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def get_value(self, key: str) -> Any:
        """A JSON value stored under ``key`` in the settings table (``None`` if unset)."""
        return self._get_setting(key)

    def put_value(self, key: str, value: Any) -> None:
        self._put_setting(key, value)

    def get_settings(self) -> Settings:
        raw = self._get_setting("app")
        return Settings.model_validate(raw) if raw else Settings()

    def put_settings(self, settings: Settings) -> Settings:
        self._put_setting("app", settings.model_dump())
        return settings

    def selected_provider(self) -> str | None:
        return self._get_setting("provider")

    def set_selected_provider(self, provider_id: str | None) -> None:
        self._put_setting("provider", provider_id)
