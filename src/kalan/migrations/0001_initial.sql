-- Only redacted data is stored: statements come out of the redact -> parse pipeline,
-- manual descriptions are redacted before insert.

CREATE TABLE statements (
    id              TEXT PRIMARY KEY,
    bank            TEXT NOT NULL,
    card_last4      TEXT,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    data            TEXT NOT NULL,            -- Statement JSON without transactions
    analysis_status TEXT NOT NULL DEFAULT 'none',
    created_at      TEXT NOT NULL
);

-- Duplicate rule (docs/api.md): same bank + card_last4 + period.
CREATE UNIQUE INDEX statements_unique_period
    ON statements (bank, ifnull(card_last4, ''), period_start, period_end);

CREATE TABLE transactions (
    id              TEXT PRIMARY KEY,
    statement_id    TEXT REFERENCES statements (id) ON DELETE CASCADE,
    source          TEXT NOT NULL CHECK (source IN ('statement', 'manual')),
    date            TEXT NOT NULL,
    position        INTEGER NOT NULL,
    data            TEXT NOT NULL,            -- Transaction JSON without category
    category        TEXT,
    model_recurring INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX transactions_statement ON transactions (statement_id);
CREATE INDEX transactions_date ON transactions (date);

CREATE TABLE manual_plans (
    id             TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
    data           TEXT NOT NULL              -- ManualInstallment JSON
);

CREATE TABLE analyses (
    statement_id TEXT PRIMARY KEY REFERENCES statements (id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,
    data         TEXT NOT NULL,               -- commentary JSON
    created_at   TEXT NOT NULL
);

CREATE TABLE jobs (
    id           TEXT PRIMARY KEY,
    statement_id TEXT NOT NULL,
    provider     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed')),
    error        TEXT,
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                       -- JSON
);
