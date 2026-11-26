-- AI batch jobs (POST /analysis/all): one job analyses many statements, then writes AI suggestions.
-- kind: statement (one statement, statement_id set) | analysis_all (statement_id = '').
ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'statement';
ALTER TABLE jobs ADD COLUMN done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN total INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN failed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN phase TEXT NOT NULL DEFAULT 'statements';
