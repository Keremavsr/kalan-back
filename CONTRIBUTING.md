# Contributing

Two kinds of help move kalan forward the most: a template for your bank and a translation.

Whatever you send, never include a real statement, a screenshot of one, or text copied from it. Test fixtures in this
repository are synthetic.

## Add your bank

Most statement layouts fit a short YAML template, no Python. [docs/adding-a-bank.md](docs/adding-a-bank.md) walks
through it, and [docs/banks-wanted.md](docs/banks-wanted.md) lists the banks we would like next. Send the template as a
pull request with a synthetic test statement.

Cannot write one? Open a [Bank not supported](https://github.com/Keremavsr/kalan-back/issues/new?template=bank-not-supported.yml)
issue with the anonymous layout report (`kalan teshis <file.pdf>`).

## Translate

- The app's screens are translated in [kalan-front](https://github.com/Keremavsr/kalan-front/blob/main/CONTRIBUTING.md).
- The terminal output is English and Turkish, both in `src/kalan/i18n.py`; the sentences the API writes itself are in
  `src/kalan/texts.py`. Fixes to either are welcome. Keep the keys and the `{placeholders}` as they are.

## Code changes

Open an issue first for anything bigger than a fix, so we agree on it before you spend time. Run `ruff check .` and
`pytest` before the pull request; setup is under Development in the [README](README.md#development).
