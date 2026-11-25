# Adding a bank

Each supported statement layout is one Python module in `src/kalan/parsers/`, named
`<country>_<bank>_<product>.py` in lower case: ISO 3166 country code, bank, card product. Examples:
`tr_vakifbank_worldcard.py`, `br_nubank_mastercard.py`. The registry imports every module whose
name fits that pattern and registers each `StatementParser` subclass in it. Nothing else needs to be
edited.

## 1. Look at the text, not the PDF

Parsers read the output of PyMuPDF `page.get_text()` for each page, joined with a form feed
(`kalan.parsers.PAGE_BREAK`). Print it for a statement of your own and study the line structure:

```python
from kalan.pdf import extract_text
print(extract_text("my-statement.pdf"))
```

Do this on your own machine only. Never commit a real statement, its text, or any merchant or
amount copied from it.

## 2. Write the parser

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

## 3. Add synthetic fixtures and tests

Write a generator in `tools/` that produces PDFs whose `get_text()` output has the same line
structure as the real statement, with invented merchants, amounts, card number and name, plus a
ground-truth JSON per PDF. See `tools/synth_vakifbank.py`. Then add a test that parses every
fixture and compares every field with the ground truth (see `tests/test_vakifbank.py`).

Check a folder of your own real statements without printing personal data:

```sh
python tools/check_real.py path/to/folder
```
