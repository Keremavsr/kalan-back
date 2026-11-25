# kalan (back end)

kalan is a free, open source credit card tracker that runs on your own computer. Its focus is
installments (Turkish *taksit*, Brazilian *parcelado*, Mexican *meses sin intereses*): it reads your
card statement PDFs, works out every installment plan, and shows what you have already committed to
pay in each of the coming months.

All money math (installment schedules, totals, statement reconciliation) is done by plain code. An
AI model, if you connect one, only suggests categories and writes commentary.

This repository is the back end (Python). The web interface lives in the separate `kalan-front`
repository and talks to this service over HTTP only (see [docs/api.md](docs/api.md)).

## Privacy

Your card number, name, address, IBAN and customer numbers are removed on your own computer before
anything is stored or sent anywhere; only the last four digits of the card are kept. If you choose a
cloud AI provider, it receives only the date, description, amount and category of each transaction.
If you want nothing at all to leave your computer, choose a local model.

## Install

Requires Python 3.12 or newer.

```sh
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
```

## Development

```sh
pip install -e ".[dev]"
ruff check .
pytest
python tools/synth_vakifbank.py     # regenerate the synthetic VakıfBank fixtures
```

Supported statements are listed in `src/kalan/parsers/`. To add a bank, see
[docs/adding-a-bank.md](docs/adding-a-bank.md).

Test fixtures are synthetic. Never add a real statement to this repository.

## Licence

MIT, see [LICENSE](LICENSE).
