-- CSV / OFX / QIF imports. Rows live in their own table (source "import"); descriptions are redacted
-- before insert. Deleting an import deletes its rows.

CREATE TABLE imports (
    id         TEXT PRIMARY KEY,
    format     TEXT NOT NULL CHECK (format IN ('csv', 'ofx', 'qif')),
    data       TEXT NOT NULL,                 -- {"currency", "detected", "rows", "imported", "months"}
    created_at TEXT NOT NULL
);

CREATE TABLE import_transactions (
    id        TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
    date      TEXT NOT NULL,
    position  INTEGER NOT NULL,
    data      TEXT NOT NULL,                  -- Transaction JSON without category
    category  TEXT
);

CREATE INDEX import_transactions_import ON import_transactions (import_id);
CREATE INDEX import_transactions_date ON import_transactions (date);
