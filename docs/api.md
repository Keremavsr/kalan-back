# kalan API contract v0.2.0

Changes from v0.1.1: AI fallback reader and `needs_review` on statements, `POST /statements/{id}/template-draft`
(see "Statements"). Every v0.1.1 route and field is unchanged.

Back end: `kalan-back` (Python, FastAPI). Listens on `127.0.0.1:8765` only. JSON, UTF-8.
Front end: `kalan-front` (separate repo) talks to it only over HTTP. Base URL configurable (`VITE_KALAN_API`, default `http://127.0.0.1:8765`).
All money values are **integer minor units** (kuruş/centavos) plus `currency` (ISO 4217). Dates are ISO `YYYY-MM-DD`. Months are `YYYY-MM`.
Errors: `{"error": {"code": "string_code", "message": "human text", "details": {}}}` with a fitting HTTP status.

## Health and setup
- `GET /health` -> `{"status":"ok","version":"0.1.0"}`
- `GET /providers` -> `{"providers":[{"id":"claude-cli|codex-cli|agy-cli|gemini-cli|openai-compatible|local","name":"Claude Code","available":true,"detail":"claude 2.x found on PATH"}], "selected":"claude-cli"|null}`
- `PUT /providers/selected` body `{"id":"codex-cli"}` -> same as GET.
- `GET /settings` / `PUT /settings` -> `{"locale":"tr|en","base_currency":"TRY","months_ahead":12}`

## Statements
- `POST /statements?ai_fallback=true|false` multipart `file` (PDF, max 10 MB). Redacts, detects the bank parser or
  template, parses, stores. -> 201 `Statement`. 409 `duplicate` if same bank+card_last4+period already stored.
  When no parser or template matches (v0.2.0):
  - `ai_fallback=true` (default) and a provider is selected and available: the redacted page text goes to that
    provider, which transcribes it into strict JSON (header fields, rows, printed totals). Code turns it into a
    `Statement` with `parser_id: "ai-fallback"`, builds the installment plans and runs the same reconciliation.
    `needs_review: true` when it does not reconcile. The request waits for the model (up to 180 s, one retry on
    invalid JSON). 502 `ai_fallback_failed` if the provider fails or answers invalid JSON twice. The redacted text
    of such a statement is kept (deleted with the statement) for `template-draft`.
  - otherwise 422 `unknown_format` with `details.ai_fallback` = `"off"`, `"no_provider"`, `"provider_unavailable"`
    or `"too_long"` (more than 60 000 characters).
- `POST /statements/{id}/template-draft` (v0.2.0, AI-fallback statements only) -> 200
  `{"provider","yaml":"...","valid":true,"errors":[],"detect":true,"parse_error":null,"reconciliation":Reconciliation|null}`:
  the model drafts a YAML bank template from the kept text; code validates it against the template schema and runs it
  on the same text. `valid:false` with `errors` if the draft still does not validate after one retry. 409
  `no_statement_text` for a statement that was not read by the fallback, 409 `no_provider` / `provider_unavailable`,
  502 `provider_failed`. Synchronous like the fallback.
- `GET /statements` -> `{"items":[StatementSummary]}` newest first.
- `GET /statements/{id}` -> `Statement`
- `DELETE /statements/{id}` -> 204

`StatementSummary`:
```json
{"id":"st_01","bank":"vakifbank","product":"Worldcard","card_last4":"0000","period_start":"2026-08-26","period_end":"2026-09-25",
 "due_date":"2026-10-05","currency":"TRY","statement_total":0,"minimum_payment":0,
 "reconciliation":{"status":"ok|mismatch","expected":1047844,"computed":1047844,"difference":0},
 "analysis_status":"none|pending|done|failed","needs_review":false}
```
`Statement` = `StatementSummary` + `"transactions":[Transaction]` + `"interest_rates":{"purchase_monthly":"3.25", ...}` (strings, as printed)
+ `"parser_id":"tr_vakifbank_worldcard|<template id>|ai-fallback"` and optional parsed extras (`credit_limit`,
`next_period_end`, `next_due_date`, `printed_summary`, `page_count`).
`currency` is any ISO 4217 code; minor units follow ISO 4217 (JPY 0 decimals, KWD 3, most currencies 2).

`Transaction`:
```json
{"id":"tx_01","date":"2026-09-13","description":"Mobil-Taksitli Nakit Avans 1.Taksit Anap","amount":343978,"currency":"TRY",
 "direction":"debit|credit","kind":"purchase|installment|cash_advance|fee|tax|interest|payment|refund|carry_over|other",
 "installment":{"remaining_count":2,"remaining_amount_each":373011,"is_last":false,"plan_id":"pl_01"}|null,
 "category":"groceries|null","merchant":"normalized merchant or null","source":"statement|manual"}
```

## Manual entry
- `POST /transactions` body: `{"date","description","amount","currency","direction","installment":{"total_count":6,"amount_each":50000}|null}` -> 201 `Transaction` (`source:"manual"`).
- `GET /transactions?month=2026-09&source=manual|statement` (both filters optional) -> `{"items":[Transaction]}`, by date.
- `DELETE /transactions/{id}` (manual only) -> 204

## Calculations (computed by code, never by the model)
- `GET /installments/schedule?months=12` ->
```json
{"currency":"TRY","months":[{"month":"2026-11","total":411000,"items":[{"plan_id":"pl_01","description":"...","amount":373011,"index":2,"count":3}]}],
 "plans":[{"plan_id":"pl_01","description":"...","kind":"purchase|cash_advance","remaining_count":2,"amount_each":373011,"ends":"2026-12","total_remaining":746022}]}
```
Every schedule item and every plan also carries `"breakdown":{"principal":int,"interest":int,"bsmv":int,"kkdf":int}` or `null`
(null for plain purchases; for a cash advance the four parts of that month / of the remaining total, summing to `amount` /
`total_remaining`).
- `GET /summary/monthly?month=2026-09` -> always 200 (a month with no data returns zeros, empty `by_category` and empty `statement_ids`, never 404) `{"month","currency","total_spend","total_credit","by_category":[{"category","amount","share":"0.18"}],"previous_month":{"total_spend","change":"0.12"}|null,"statement_ids":[...]}`
- `GET /subscriptions` -> `{"items":[{"merchant","amount_last","amount_previous","currency","months_seen":["2026-08","2026-09"],"price_change":"0.20"|null}]}` (recurring = same normalized merchant in >= 2 consecutive months with amounts within 25% or flagged by the model).

## Model analysis (one call per statement)
- `POST /statements/{id}/analyze` -> 202 `{"job_id":"job_01","status":"pending"}`
- `GET /jobs/{job_id}` -> `{"status":"pending|done|failed","error":null}`
- `GET /statements/{id}/analysis` -> `{"categories_applied":true,"commentary":{"locale":"tr","summary":"text","watch_items":[{"title","detail","transaction_ids":[...]}]},"provider":"claude-cli","created_at":"..."}`

What is sent to the model: only date, description, amount, currency, kind per transaction plus code-computed totals. Never card number, name, address, IBAN, customer number.
The AI fallback and template draft send the statement's page text after redaction (card number, name, address, IBAN,
national id, phone, e-mail and long numbers removed), redacted a second time just before sending.
