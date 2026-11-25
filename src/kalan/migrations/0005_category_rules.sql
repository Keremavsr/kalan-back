-- Category rules the user added (merchant phrase -> category). The built-in rules ship in categories.yaml.
-- category_source records who set a transaction's category, so a new user rule can replace what a built-in
-- rule or the model chose but never what the user entered by hand: rule | model | user, NULL = none.

CREATE TABLE category_rules (
    id         TEXT PRIMARY KEY,
    pattern    TEXT NOT NULL,
    folded     TEXT NOT NULL UNIQUE,         -- pattern after fold(), the duplicate key
    category   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

ALTER TABLE transactions ADD COLUMN category_source TEXT;
ALTER TABLE import_transactions ADD COLUMN category_source TEXT;

UPDATE transactions SET category_source = 'user' WHERE category IS NOT NULL AND source = 'manual';
UPDATE transactions SET category_source = 'model' WHERE category IS NOT NULL AND source <> 'manual';
