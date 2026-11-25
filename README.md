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

- Everything runs on your computer. The service listens on `127.0.0.1` only and keeps its data in one
  SQLite file in your user data folder (`KALAN_DATA_DIR` overrides it).
- Your card number, name, address, IBAN, national ID, phone, e-mail and customer numbers are removed
  from the statement text before it is parsed or stored. Only the last four card digits are kept.
- The PDF itself is never stored.
- If you ask for an analysis, the chosen AI provider receives, per transaction, only an opaque id,
  the date, the description, the amount, the currency and the kind, plus totals computed by kalan.
  Descriptions are passed through the redactor once more before they leave. A cloud provider (Claude,
  Codex, Antigravity, Gemini, an OpenAI-compatible API) sees this data; a local model keeps it on
  your computer.
- Logs contain ids, counts and error codes, never descriptions, amounts or model output.
- API keys are read from environment variables or the OS keyring. They are never written to the
  database or the logs.

## Install

Requires Python 3.12 or newer.

```sh
pipx install .
```

## Run

```sh
kalan serve                 # http://127.0.0.1:8765
kalan serve --port 9000     # another port; the host is always 127.0.0.1
```

The web interface (`kalan-front`) runs on `http://localhost:5173` by default. To allow other browser
origins, set `KALAN_CORS_ORIGINS` to a comma-separated list.

### Desktop sidecar

A desktop shell (Electron) can start the service as a child process:

```sh
kalan serve --port 0 --print-port
```

`--port 0` picks a free port. `--print-port` writes exactly one line to stdout once the server is
listening, `{"event": "ready", "port": 53141}`, and nothing else ever goes to stdout; logs go to
stderr. When stdin is a pipe and the pipe closes (the parent quit or died), the service shuts down
cleanly with exit code 0.

## AI providers

An AI model is optional. It only suggests a category for each transaction and writes a short
commentary in your language; every number comes from code. Each statement costs one model call (and
one retry if the answer is not valid JSON). A call times out after 180 seconds.

| Provider | Setup |
| --- | --- |
| `claude-cli` | Claude Code installed and signed in (`claude` on PATH) |
| `codex-cli` | Codex CLI installed and signed in (`codex` on PATH) |
| `agy-cli` | Antigravity CLI installed and signed in (`agy` on PATH) |
| `gemini-cli` | Gemini CLI installed and signed in (`gemini` on PATH) |
| `openai-compatible` | `KALAN_OPENAI_BASE_URL`, `KALAN_OPENAI_MODEL`, and `KALAN_OPENAI_API_KEY` or a keyring entry (service `kalan`, user `openai-compatible`) |
| `local` | An OpenAI-compatible server on localhost (llama.cpp, Ollama, LM Studio): `KALAN_LOCAL_URL` (default `http://127.0.0.1:11434/v1`), optional `KALAN_LOCAL_MODEL` |

A CLI counts as available when its executable is on PATH and `--version` succeeds. CLIs run in an
empty temporary folder; Codex runs with a read-only sandbox. The usage limits of your own account
apply.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip install -e ".[dev]"
ruff check .
pytest
python tools/synth_vakifbank.py     # regenerate the synthetic VakıfBank fixtures
```

Supported statements are listed in `src/kalan/parsers/`. To add a bank, see
[docs/adding-a-bank.md](docs/adding-a-bank.md).

Test fixtures are synthetic. Never add a real statement to this repository.

## Licence

MIT, see [LICENSE](LICENSE).
