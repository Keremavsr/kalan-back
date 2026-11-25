# Adding a bank

There are two ways to teach kalan a new statement layout. Try the template first: it needs no
Python, and most layouts fit it. Write a Python parser only when the template cannot describe the
layout.

## Before you start: look at the text, not the PDF

Both paths read the output of `kalan.pdf.extract_text()`: one line per printed cell (a label and its value
usually land on two lines), in drawing order, pages joined with a form feed.
Print it for a statement of your own and study the line structure:

```python
from kalan.pdf import extract_text
print(extract_text("my-statement.pdf"))
```

Do this on your own machine only. Never commit or post a real statement, its text, or any merchant,
name or amount copied from it.

## Path 1: a YAML template (no code)

A template is one YAML file that describes one card product. kalan reads templates from two places
when it starts:

- `src/kalan/templates/*.yaml` (shipped with kalan);
- `templates/*.yaml` in your data folder (`KALAN_DATA_DIR`, or the platform user data folder, for
  example `%LOCALAPPDATA%\kalan` on Windows or `~/.local/share/kalan` on Linux).

Name the file after its id: `<country>_<bank>_<product>.yaml`, lower case, with the ISO 3166 country
code, for example `br_examplebank_gold.yaml`. A user template whose id is already taken, or that does
not validate, is skipped and logged; the rest keep working.

The full format is the JSON Schema in `src/kalan/templates/template.schema.json` (`kalan template
schema` prints it; point your editor's YAML plugin at it for completion). The working reference is
`src/kalan/templates/tr_vakifbank_worldcard.yaml`. The parts:

| Key | What it declares |
| --- | --- |
| `id`, `country`, `bank`, `product`, `currency` | Identity. `currency` is any ISO 4217 code. |
| `detect.all` / `detect.none` | Phrases that must (or must not) appear. Be strict: two parsers claiming one statement is an error. |
| `amount` | `preset` (`dot_decimal` 2,345.60, `comma_decimal` 2.345,60, `space_comma` 2 345,60) or `thousands` + `decimal`; `credit_signs` marks credits. |
| `date_format` | Tokens `DD D MM M MMM YYYY YY` in any order: `DD.MM.YYYY`, `MM/DD/YYYY`, `YYYY-MM-DD`, `D MMM YYYY`. |
| `header` | `layout` (`colon_line`: label, `:`, value on three lines; `next_line`; `inline`: `Label: value`) and `fields`: printed labels for `statement_total`, `minimum_payment`, `due_date`, `period_end` (required) and `period_start`, `credit_limit`, `next_period_end`, `next_due_date`, `card` (optional). |
| `table` | `header` labels, `end` label, lines to skip (page titles, footers), and the row `layout`: `cells` (each cell on its own line, `columns` in order) or `line` (one row per line, `row_pattern` with named groups `date`, `description`, `amount`, optional `installment`, `points`). |
| `installment.cells` | Regexes for the installment column: groups `left` + `each` (`(?P<left>\d+)x(?P<each>[\d.,]+)`), `k` + `n` (`(?P<k>\d+)/(?P<n>\d+)`), or `last: true` for a cell like `Son Taksit`. |
| `installment.description` | Regexes giving the installment number from the description (group `k`, optional `n`). |
| `installment.stem_remove` | Regexes removed from the description to get a stable plan name, so the same plan gets the same id every month. |
| `kinds` | Ordered rules: `match` (regex on the folded description), optional `credit` and `installment` conditions, then `kind`, optional `direction` and `component` (roles of an installment cash advance: `principal`, `bsmv`, `kkdf`, `interest`, `early_payoff`). First match wins; then unmatched credits get `credit_kind`, the rest `default_kind`. |
| `summary` | The printed totals block: `title`, `layout` and labels for `previous_balance`, `period_transactions`, `interest_and_fees`, `payments`, `statement_total`. kalan checks the parsed rows against them. |
| `rates` | Optional interest-rate block. |

Labels and `match` regexes are compared after `fold()`: lower case, no diacritics, Turkish dotted and
undotted I unified, spaces collapsed. Write `odemeniz icin tesekkurler`, not `ÖDEMENİZ İÇİN TEŞEKKÜRLER`.

Test the template on a statement of your own:

```sh
kalan template check my_template.yaml my-statement.pdf
kalan template check my_template.yaml my-statement.pdf --quiet
```

It validates the file, runs it on the redacted text and prints every header field (found or missing),
row counts by kind, installment plans, and each reconciliation check. Descriptions are never printed;
with `--quiet` no values are printed at all, so the output is safe to paste into an issue. Exit code 0
means the statement parsed and every total matched.

Done when `check` says `reconciliation: ok` on every statement you have. To contribute the template,
open a pull request with the YAML and a synthetic sample (see "Synthetic fixtures and tests" below); never a real statement.

If kalan read a statement with its AI fallback (no template matched), `POST
/statements/{id}/template-draft` asks the model for a first draft of the template. Treat it as a
starting point and check it with `kalan template check`.

## Path 2: a Python parser

Use this when a layout needs logic a template cannot express. Each parser is one Python module in
`src/kalan/parsers/`, named like a template id. The registry imports every module whose name fits
that pattern and registers each `StatementParser` subclass in it. A Python parser takes precedence
over a built-in template with the same id.

### Write the parser

```python
from kalan.amounts import COMMA_DECIMAL
from kalan.models import Statement
from kalan.parsers.base import StatementParser


class ExampleBankGold(StatementParser):
    id = "tr_examplebank_gold"      # must equal the module name
    bank = "examplebank"
    product = "Gold"
    country = "TR"
    currency = "TRY"
    amount_format = COMMA_DECIMAL     # declared, never guessed
    date_format = "DD.MM.YYYY"

    def detect(self, text: str) -> bool:
        ...  # strict: bank name and product name and a layout marker

    def parse(self, text: str) -> Statement:
        ...
```

Rules:

- `detect` must be strict. Two parsers claiming the same text is an error.
- `parse` receives text that has already been redacted (`kalan.redact`). Read the card's last four
  digits from the redaction token (`[CARD ****1234]`) or the masked number; never store more.
- Money is `int` minor units. Use `kalan.amounts.parse_amount` with the parser's `amount_format`.
- Match labels with `kalan.text.fold`, which ignores case, diacritics and the dotted/undotted I.
- Keep each row's printed description as is; put extra structure (installment index, remaining
  count, foreign amount) in the dedicated fields.
- Give every installment row an `InstallmentInfo` with a deterministic `plan_id`, so the same
  plan gets the same id in every month's statement.
- Fill `printed_summary` if the statement prints totals; `kalan.reconcile` checks against them.
- Raise `ParseError` when the layout is broken. Never return a half-parsed statement silently.

## Synthetic fixtures and tests (both paths)

Write a generator in `tools/` that produces PDFs whose `get_text()` output has the same line
structure as the real statement, with invented merchants, amounts, card number and name, plus a
ground-truth JSON per PDF. See `tools/synth_vakifbank.py`. Then add a test that parses every
fixture and compares every field with the ground truth (see `tests/test_vakifbank.py`).

Check a folder of your own real statements without printing personal data:

```sh
python tools/check_real.py path/to/folder
```
