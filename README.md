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
- The PDF itself is never stored. If no bank template recognises a statement and you selected an AI
  provider, kalan sends the statement's redacted page text to that provider so it can transcribe the
  rows (the AI fallback). kalan then checks the result against the printed totals in code and flags it
  for review when they disagree. The redacted text of such a statement is kept, so you can ask for a
  template draft later, and it is deleted with the statement. Upload with `?ai_fallback=false` to turn
  this off.
- If you ask for an analysis, the chosen AI provider receives, per transaction, only an opaque id,
  the date, the description, the amount, the currency and the kind, plus totals computed by kalan.
  Descriptions are passed through the redactor once more before they leave. A cloud provider (Claude,
  Codex, Antigravity, Gemini, an OpenAI-compatible API) sees this data; a local model keeps it on
  your computer.
- Logs contain ids, counts and error codes, never descriptions, amounts or model output.
- API keys are read from environment variables or the OS keyring. They are never written to the
  database or the logs.

## Install

Download the file for your computer from the latest GitHub Release and put it on your PATH:

| System | File |
| --- | --- |
| Windows (x64) | `kalan-back-windows-x64.exe` (rename it to `kalan.exe`) |
| macOS (Apple silicon) | `kalan-back-macos-arm64` (rename it to `kalan`, then `chmod +x kalan`) |
| Linux (x64) | `kalan-back-linux-x64` (rename it to `kalan`, then `chmod +x kalan`) |

`SHA256SUMS.txt` in the same release lists the checksum of each file. The builds are not signed yet:
Windows SmartScreen and macOS Gatekeeper warn on first start (on macOS, `xattr -d com.apple.quarantine kalan`).

With Python 3.12 or newer you can also install from source: `pipx install .`

## Use from the terminal

Every screen of the app is also a command. Tables by default; add `--json` for scripts (money is then in
minor units, `123456` = `1.234,56`). Turkish names, English aliases in brackets:

```sh
kalan import ekstre.pdf            # a statement PDF; also .csv, .ofx, .qif  (--bank ID, --no-ai)
kalan statements                   # stored statements, newest first      (ekstreler)
kalan takvim -n 6                  # installments of the next 6 months    (schedule)
kalan ay 2099-01                   # spending of a month by category      (month)
kalan islemler -s market           # search transactions                  (transactions)
kalan abonelikler                  # recurring charges                    (subscriptions)
kalan uyarilar                     # alerts                               (alerts)
kalan simule 9.999,99 6            # what if I buy this in 6 installments (simulate)
kalan analiz st_0123456789ab       # AI commentary on one statement       (analyze)
kalan banks                        # ids for --bank
```

`--data-dir PATH` (or `KALAN_DATA_DIR`) points every command at another database. The commands read and
write the same database as `kalan serve`, so the app shows what you imported from the terminal.

## Use with your AI

`kalan mcp` is a local MCP server on stdio. Your own AI CLI starts it and can then answer questions like
"where did I spend most this month?" from your data. It only reads: list statements, one statement,
installment schedule, monthly summary, alerts, subscriptions and transaction search. It cannot import,
delete, change settings or call another AI. What it returns goes to the model your CLI uses, so a cloud
model sees those rows (redacted descriptions, amounts, dates; never card numbers or names).

```sh
claude mcp add --scope user kalan -- kalan mcp     # Claude Code
codex mcp add kalan -- kalan mcp                   # Codex CLI
gemini mcp add --scope user kalan kalan mcp        # Gemini CLI
```

Or by hand: Codex `~/.codex/config.toml`

```toml
[mcp_servers.kalan]
command = "kalan"
args = ["mcp"]
```

Gemini CLI `~/.gemini/settings.json`

```json
{ "mcpServers": { "kalan": { "command": "kalan", "args": ["mcp"] } } }
```

Use the full path of the file instead of `kalan` if it is not on your PATH.

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

One-file build (what the release workflow does on each OS):

```sh
pip install -e ".[build]"
pyinstaller packaging/kalan.spec --clean --noconfirm   # dist/kalan.exe or dist/kalan
python packaging/smoke.py dist/kalan.exe
```

The build bundles pypdfium2 and never PyMuPDF. Tagging `vX.Y.Z` (equal to the package version) runs
`.github/workflows/release.yml`, which attaches the three files and `SHA256SUMS.txt` to a GitHub Release.

Supported statements are the templates in `src/kalan/templates/` and the parsers in `src/kalan/parsers/`.
To add a bank (a YAML template, no Python needed), see
[docs/adding-a-bank.md](docs/adding-a-bank.md).

Banks we would like to support, and how to help, are in [docs/banks-wanted.md](docs/banks-wanted.md).
Statements from any other bank can still be read with the AI fallback or imported as CSV, OFX or QIF.
Amounts accept any ISO 4217 currency and dates any day/month/year order.

Test fixtures are synthetic. Never add a real statement to this repository.

## Licence

MIT, see [LICENSE](LICENSE).
