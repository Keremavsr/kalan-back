-- Redacted statement text, kept only for statements read by the AI fallback, so the user can ask the
-- model for a template draft later. Removed together with its statement.

CREATE TABLE statement_texts (
    statement_id TEXT PRIMARY KEY REFERENCES statements (id) ON DELETE CASCADE,
    text         TEXT NOT NULL
);
