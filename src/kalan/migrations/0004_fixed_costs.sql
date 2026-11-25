-- Recurring fixed costs (rent, dues) entered by the user. Shown in the schedule as a separate series.

CREATE TABLE fixed_costs (
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,                 -- FixedCost JSON
    created_at TEXT NOT NULL
);
