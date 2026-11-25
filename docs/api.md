# kalan API contract v0.3.0

Changes from v0.2.0: encrypted PDFs (`password` / `remember_password` on `POST /statements`, `/pdf-passwords`);
save gate (422 `save_blocked`, `review` on statements, see "Save gate"); `POST /diagnose`. Every v0.2.0 route and field is unchanged;
`needs_review` is now also set by soft save-gate findings, not only by the AI fallback.

Changes from v0.1.1: AI fallback reader and `needs_review` on statements, `POST /statements/{id}/template-draft`
(see "Statements"); CSV/OFX/QIF imports (`source: "import"`); `POST /simulate/installment`; `GET /alerts`;
fixed costs (`/fixed-costs`, schedule `fixed` series). Every v0.1.1 route and field is unchanged.

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
  Encrypted PDFs (v0.3.0): optional multipart form fields `password` (max 200 characters) and
  `remember_password=true|false`. Without `password` kalan tries the passwords remembered in the OS keyring.
  - 422 `pdf_password_required`: encrypted and no password given (or no remembered one opens it).
  - 422 `pdf_password_wrong`: the given password does not open it.
  Both carry `details.hints` = `[{"bank":"garanti","hint":"last 6 digits of your national ID"}]` from the bank
  templates (the bank is unknown until the PDF opens, so every hint is listed). With `remember_password=true` and a
  `password` that opened a statement read by a bank parser or template, the password is stored in the OS keyring for
  that statement's `bank`; the 201 response then has header `X-Kalan-Password-Remembered: true|false` (`false`: no
  keyring, or the statement was read by the AI fallback). The password never reaches SQLite, a log or a response.
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
- `GET /pdf-passwords` (v0.3.0) -> `{"keyring":true,"items":[{"bank":"garanti","hint":"text"|null,"remembered":false}]}`,
  one item per known bank; never a password.
- `PUT /pdf-passwords/{bank}` (v0.3.0) body `{"password":"..."}` -> 204. 404 `unknown_bank`, 503 `keyring_unavailable`.
- `DELETE /pdf-passwords/{bank}` (v0.3.0) -> 204. 404 `not_remembered`.
- `GET /statements` -> `{"items":[StatementSummary]}` newest first.
- `GET /statements/{id}` -> `Statement`
- `DELETE /statements/{id}` -> 204

`StatementSummary`:
```json
{"id":"st_01","bank":"vakifbank","product":"Worldcard","card_last4":"0000","period_start":"2026-08-26","period_end":"2026-09-25",
 "due_date":"2026-10-05","currency":"TRY","statement_total":0,"minimum_payment":0,
 "reconciliation":{"status":"ok|mismatch","expected":123456,"computed":123456,"difference":0},
 "analysis_status":"none|pending|done|failed","needs_review":false,
 "review":[{"code":"reconciliation_mismatch","severity":"soft","message":"text","details":{"difference":-1}}]}
```
`review` (v0.3.0) lists the soft save-gate findings; empty for a clean statement.

### Save gate (v0.3.0)
Before `POST /statements` stores a parsed statement, code checks it. A **hard** finding blocks the save: 422
`save_blocked` with `details.issues` = `[GateIssue]` (all findings, hard and soft); a duplicate keeps the v0.2.0
answer, 409 `duplicate` with `details.statement_id`. Only **soft** findings: saved with `needs_review: true` and the
findings in `review`. `GateIssue` = `{"code","severity":"hard|soft","message","details":{}}`.

| Code | Severity | Meaning, `details` |
|------|----------|--------------------|
| `duplicate_statement` | hard | Same bank + card + period (or id) already stored (answered as 409 `duplicate`). `statement_id` |
| `period_invalid` | hard | Period starts after it ends. `period_start`, `period_end` |
| `due_before_cut_off` | hard | Due date before the cut-off date. `period_end`, `due_date` |
| `no_transactions` | hard | No rows read but the total is not zero. |
| `amount_scale` | hard | A total and its rows differ by a factor of 10, 100 or 1000 (decimal separator read wrong). `checks`, `factor` |
| `period_unusual` | soft | Period shorter than 20 or longer than 45 days. `days` |
| `not_reconciled` | soft | No reconciliation was run. |
| `reconciliation_mismatch` | soft | Rows do not add up to the printed totals. `difference`, `checks` (failed check names) |
| `no_printed_summary` | soft | No totals block found; only `statement_total` was checked. |
| `rows_after_cut_off` | soft | Rows dated more than 3 days after the cut-off. `count` |
| `minimum_above_total` | soft | Minimum payment larger than the statement total. |
| `installment_counter` | soft | A row's installment number, plan length and remaining count disagree. `transaction_ids` |
| `installment_sequence` | soft | A plan's installment number does not follow the previous statement's (same card, ended at most 45 days before). `plan_ids`, `previous_statement_id` |
| `installment_missing` | soft | A plan still running in the previous statement is absent (early payoff, or a row not read). `plan_ids`, `previous_statement_id` |
`Statement` = `StatementSummary` + `"transactions":[Transaction]` + `"interest_rates":{"purchase_monthly":"3.25", ...}` (strings, as printed)
+ `"parser_id":"tr_vakifbank_worldcard|<template id>|ai-fallback"` and optional parsed extras (`credit_limit`,
`next_period_end`, `next_due_date`, `printed_summary`, `page_count`).
`currency` is any ISO 4217 code; minor units follow ISO 4217 (JPY 0 decimals, KWD 3, most currencies 2).

`printed_summary` (the bank's own totals block, signed as debt: positive = owed) or `null`:
```json
{"previous_balance":123456,"period_transactions":999999,"interest_and_fees":1234|null,"payments":123456,
 "statement_total":999999,"remaining_installments":456789|null}
```
`interest_and_fees` is `null` when the bank prints no separate fee line and counts interest and fees inside
`period_transactions` (Yapı Kredi). `remaining_installments` is the printed total of installments still to come
after this statement, `null` when the bank does not print it.

`Transaction`:
```json
{"id":"tx_01","date":"2099-01-15","description":"ORNEK MARKET 1.Taksit","amount":123456,"currency":"TRY",
 "direction":"debit|credit","kind":"purchase|installment|cash_advance|fee|tax|interest|payment|refund|carry_over|other",
 "installment":{"remaining_count":2,"remaining_amount_each":123456,"is_last":false,"plan_id":"pl_01",
                "index":1|null,"total_count":3|null,"component":"principal|bsmv|kkdf|interest|early_payoff|null"}|null,
 "category":"groceries|null","merchant":"normalized merchant or null","source":"statement|manual|import",
 "original_amount":1234|null,"original_currency":"USD|null","points":"12,34|null","detail":"text|null"}
```
`original_amount` / `original_currency`: the foreign amount of a charge made in another currency. `points`: the
loyalty points cell as printed. `detail`: sub-lines the bank prints under a row (bill reference, installment note,
exchange note), joined with ` | `, redacted like the rest of the text; never rows of their own.

## Diagnosis (v0.3.0)
- `POST /diagnose` multipart `file` (PDF, max 10 MB), optional form field `password` -> 200
  `{"report":DiagnosisReport,"text":"plain-text report for an issue form"}`. Nothing is stored. Same errors as the
  upload: 413, 415 `not_pdf`, 422 `pdf_password_required` / `pdf_password_wrong` / `invalid_pdf`.

`DiagnosisReport` holds the statement's structure only. Each line is a shape: digits -> `9`, lower-case letters ->
`a`, upper-case -> `A`, punctuation and single spaces kept, a run of 3+ spaces -> `<N sp>`. No text or amount survives.
```json
{"kalan_version":"0.1.0","page_count":3,"has_text":true,"line_count":132,"lines_per_page":[100,26,6],
 "number_styles":{"1.234,56":39,"1,234.56":0,"1 234,56":0},
 "date_styles":{"DD.MM.YYYY":0,"DD/MM/YYYY":0,"YYYY-MM-DD":0,"DD.MM.YY":0,"D Month YYYY":13},
 "templates":[{"id":"tr_garanti_bonus","detected":true,"anchors":{
    "detect_all":{"found":5,"total":5,"missing":[]},"detect_none_present":[],
    "header_fields":{"statement_total":true,"due_date":true},"table_header":{"found":5,"total":5,"missing":[]},
    "table_end":true,"summary_title":true}}],
 "shapes":[{"page":1,"shape":"Aaaaa Aaaaaa","repeat":1},{"page":1,"shape":"9.999,99 AA","repeat":2}],
 "shapes_truncated":false}
```
`anchors` (template parsers only) names kalan's own template phrases that were found or missing; `has_text:false`
means a scanned image without a text layer. At most 600 shapes; consecutive equal shapes are merged with `repeat`.
The Python entry point is `kalan.diagnose.diagnose(path_or_bytes, password=None) -> dict` (`as_text(report)` for
the plain-text form). The issue form `.github/ISSUE_TEMPLATE/bank-not-supported.yml` asks for the `text`.

## Imports (v0.2.0)
- `POST /imports` multipart `file` (CSV, OFX/QFX or QIF, max 10 MB), optional form fields `format` (`csv|ofx|qif`,
  default: sniffed from name and content) and `mapping` (JSON string, every key optional):
  `{"date","description","amount","debit","credit","installment","currency_column"` (column names, matched ignoring
  case and accents), `"currency":"BRL","delimiter":";","decimal":",","date_format":"DD/MM/YYYY","sign":"negative_debit|positive_debit","template":"<template id>"}`.
  Everything not given is detected: delimiter, header row and columns (names in en/pt/es/tr/he), number format, date
  order (day-first wins when every day is <= 12; `date_ambiguous: true` then), and which sign is a charge (the
  majority sign of the amount column). OFX/QIF: a negative amount is a charge. Installment hints come from the
  installment column or the description (`3/10`, `parcela 3 de 10`, `cuota 3 de 6`); `template` applies that
  template's kind and installment rules instead of the generic ones. Descriptions are redacted before storing.
  -> 201 `{"id":"im_..","format","currency","rows":5,"imported":5,"skipped_duplicates":0,"months":["2026-02"],
  "detected":{"delimiter",";","decimal":",","amount_format","date_format","date_ambiguous","sign","columns":{...}},
  "transactions":[Transaction]}` (only the newly stored rows). A row already stored by an earlier import (same date,
  description, amount and currency, or same OFX FITID) is skipped. 422 `import_unreadable` (message says why),
  422 `invalid_mapping`, 413 `file_too_large`.
- `GET /imports` -> `{"items":[{"id","format","created_at","currency","detected","months","rows","imported","skipped_duplicates"}]}` newest first.
- `DELETE /imports/{id}` -> 204, removes its rows.

Imported rows appear in `GET /transactions` (`source=import` filters), the monthly summary and subscriptions.
Imported installment rows with `k/n` become plans in the schedule with `"source":"import"` (one plan per item,
however many months were imported). Do not import a period you also uploaded as a PDF: both would count.

## Manual entry
- `POST /transactions` body: `{"date","description","amount","currency","direction","installment":{"total_count":6,"amount_each":50000}|null}` -> 201 `Transaction` (`source:"manual"`).
- `GET /transactions?month=2026-09&source=manual|statement` (both filters optional) -> `{"items":[Transaction]}`, by date.
- `DELETE /transactions/{id}` (manual only) -> 204

## Fixed costs (v0.2.0)
- `POST /fixed-costs` body `{"name":"Rent","amount":1500000,"currency":"TRY"|null,"day":1-31,"start":"2026-01","end":"2026-12"|null,"category":null}`
  -> 201 `FixedCost` (same fields plus `id`; `currency` defaults to the base currency; `day` is clipped to short
  months; `end` is inclusive). 422 `invalid_period` if `end` is before `start`. The name is redacted.
- `GET /fixed-costs` -> `{"items":[FixedCost]}`. `DELETE /fixed-costs/{id}` -> 204, 404 `fixed_cost_not_found`.

## Calculations (computed by code, never by the model)
- `GET /installments/schedule?months=12` ->
```json
{"currency":"TRY","months":[{"month":"2026-11","total":411000,"items":[{"plan_id":"pl_01","description":"...","amount":373011,"index":2,"count":3}]}],
 "plans":[{"plan_id":"pl_01","description":"...","kind":"purchase|cash_advance","remaining_count":2,"amount_each":373011,"ends":"2026-12","total_remaining":746022}]}
```
Each plan also has `"source":"statement|manual|import"`. Every schedule item and every plan also carries `"breakdown":{"principal":int,"interest":int,"bsmv":int,"kkdf":int}` or `null`
(null for plain purchases; for a cash advance the four parts of that month / of the remaining total, summing to `amount` /
`total_remaining`).
- The schedule also carries `"fixed":{"kind":"fixed","months":[{"month","total","items":[{"fixed_cost_id","name","amount","currency","date"}]}]}`
  (v0.2.0) over the same months: recurring fixed costs, never added to `months[].total`. `fixed.months[].total`
  sums only items in the schedule currency.
- `GET /summary/monthly?month=2026-09` -> always 200 (a month with no data returns zeros, empty `by_category` and empty `statement_ids`, never 404) `{"month","currency","total_spend","total_credit","by_category":[{"category","amount","share":"0.18"}],"previous_month":{"total_spend","change":"0.12"}|null,"statement_ids":[...]}`
- `GET /subscriptions` -> `{"items":[{"merchant","amount_last","amount_previous","currency","months_seen":["2026-08","2026-09"],"price_change":"0.20"|null}]}` (recurring = same normalized merchant in >= 2 consecutive months with amounts within 25% or flagged by the model).

- `POST /simulate/installment` (v0.2.0) body `{"amount":120000,"count":6,"start_month":"2026-12","interest_monthly":"3.11"|null,"months":null}`
  -> `{"currency","purchase":{"amount","count","start_month","interest_monthly","installments":[{"month","amount","principal","interest"}],
  "total_cost","interest_total","ends"},"months":[{"month","without","purchase","with"}],"peak":{"without":{"month","total"},"with":{"month","total"}}}`.
  `without` is the current schedule (`GET /installments/schedule`), `with` adds the purchase. No interest: equal
  installments, the division remainder in the first. With `interest_monthly` (percent per month): equal annuity
  payments, interest on the open balance, the last installment closes it exactly. A peak tie goes to the earlier month.
  Nothing is stored.

## Alerts (v0.2.0, computed by code)
- `GET /alerts` -> `{"items":[{"id":"al_..","kind","severity":"info|warning|critical","month":"YYYY-MM",
  "message_key":"alert.<kind>","params":{...},"refs":{"statement_ids":[],"transaction_ids":[],"plan_ids":[]}}]}`,
  sorted critical first, then by month. Ids are stable while the data does not change. "This period" is the latest
  statement of each card (bank + last four digits). The front end translates `message_key` with `params`.

| kind | severity | params | when |
| --- | --- | --- | --- |
| `installment_ending` | info | description, amount_each, currency, kind | a plan's last installment is in the next statement month (schedule `start_month`) |
| `limit_high` | warning, critical at >= 100% | used_share ("0.86"), statement_total, credit_limit, currency | statement total above 80% of the printed limit |
| `interest_charged` | warning | count, total, currency | interest rows this period |
| `fee_charged` / `tax_charged` | info | count, total, currency | fee / tax rows this period |
| `foreign_currency` | info | count, currencies, total, currency | rows with an original foreign amount this period |
| `missing_statement` | warning | bank, card_last4 | a month missing between two stored statements of a card |
| `subscription_price_rise` | info | merchant, amount_previous, amount_last, price_change, currency | a subscription's last charge is higher than the one before |
| `due_soon` | warning, critical at <= 1 day | due_date, days_left, statement_total, minimum_payment, currency | due date within 5 days and a balance is owed |

## Model analysis (one call per statement)
- `POST /statements/{id}/analyze` -> 202 `{"job_id":"job_01","status":"pending"}`
- `GET /jobs/{job_id}` -> `{"status":"pending|done|failed","error":null}`
- `GET /statements/{id}/analysis` -> `{"categories_applied":true,"commentary":{"locale":"tr","summary":"text","watch_items":[{"title","detail","transaction_ids":[...]}]},"provider":"claude-cli","created_at":"..."}`

What is sent to the model: only date, description, amount, currency, kind per transaction plus code-computed totals. Never card number, name, address, IBAN, customer number.
The AI fallback and template draft send the statement's page text after redaction (card number, name, address, IBAN,
national id, phone, e-mail and long numbers removed), redacted a second time just before sending.
