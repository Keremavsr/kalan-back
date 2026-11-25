"""Prompt, payload, answer validation and the provider adapters (no real CLI or server is ever called)."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from kalan.ai import adapters as ad
from kalan.ai.prompt import (
    CATEGORIES,
    InvalidModelOutputError,
    build_payload,
    build_prompt,
    money,
    parse_answer,
)
from kalan.models import Statement, Transaction


def _tx(i: int, description: str, amount: int = 1250, direction: str = "debit", kind: str = "purchase"):
    return Transaction(id=f"tx_{i:012d}", date=date(2026, 9, i), description=description, amount=amount,
                       direction=direction, kind=kind)


def _statement(*txs: Transaction) -> Statement:
    return Statement(id="st_test", bank="testbank", product="Card", card_last4="9876",
                     period_start=date(2026, 8, 26), period_end=date(2026, 9, 25), due_date=date(2026, 10, 5),
                     statement_total=3750, minimum_payment=750, transactions=list(txs))


def test_money():
    assert money(0) == "0.00" and money(5) == "0.05" and money(123456) == "1234.56" and money(-1250) == "-12.50"


def test_payload_fields_and_totals():
    st = _statement(_tx(1, "MARKET"), _tx(2, "IADE", 500, "credit", "refund"), _tx(3, "KART UCRETI", kind="fee"))
    p = build_payload(st)
    assert set(p) == {"totals", "transactions"}
    assert [set(r) for r in p["transactions"]] == [{"id", "date", "description", "amount", "currency", "kind"}] * 3
    assert p["transactions"][1]["amount"] == "-5.00"
    assert p["totals"]["debits"] == "25.00" and p["totals"]["credits"] == "5.00"
    assert p["totals"]["by_kind"] == {"fee": "12.50", "purchase": "12.50", "refund": "-5.00"}
    assert p["totals"]["statement_total"] == "37.50"


def test_no_personal_data_can_reach_the_payload():
    """Even if a description slipped through with personal data, the payload builder redacts it again."""
    st = _statement(
        _tx(1, "ODEME 4111 1111 1111 1111"),
        _tx(2, "HAVALE TR33 0006 1005 1978 6457 8413 26"),
        _tx(3, "IADE ornek.kisi@example.com"),
        _tx(4, "TEL 0532 000 00 00"),
    )
    prompt = build_prompt(build_payload(st), "tr")
    for secret in ("4111 1111", "TR33", "1978 6457", "ornek.kisi@example.com", "0532", "9876", "testbank"):
        assert secret not in prompt
    assert "[IBAN]" in prompt and "[EMAIL]" in prompt


def test_prompt_lists_categories_and_language():
    prompt = build_prompt(build_payload(_statement(_tx(1, "MARKET"))), "en")
    assert all(c in prompt for c in CATEGORIES)
    assert "English (en)" in prompt and "ONE JSON object" in prompt


def _answer(**over) -> str:
    base = {"categories": {"tx_000000000001": "groceries"},
            "commentary": {"summary": "ok", "watch_items": [{"title": "t", "detail": "d",
                                                              "transaction_ids": ["tx_000000000001", "tx_x"]}]},
            "recurring": ["tx_000000000001", "tx_x"]}
    base.update(over)
    return json.dumps(base)


KNOWN = {"tx_000000000001", "tx_000000000002"}


def test_parse_answer_plain_fenced_and_wrapped():
    for text in (_answer(), f"```json\n{_answer()}\n```", f"Here you go:\n{_answer()}\nThanks"):
        a = parse_answer(text, KNOWN)
        assert a.categories == {"tx_000000000001": "groceries"}
        assert a.recurring == ["tx_000000000001"]
        assert a.commentary.watch_items[0].transaction_ids == ["tx_000000000001"]


@pytest.mark.parametrize("text", [
    "no json here",
    "{broken",
    _answer(categories={"tx_000000000001": "yachts"}),
    _answer(categories={"tx_unknown": "groceries"}),
    json.dumps({"categories": {"tx_000000000001": "other"}}),
    _answer(commentary={"summary": ""}),
])
def test_parse_answer_rejects(text):
    with pytest.raises(InvalidModelOutputError):
        parse_answer(text, KNOWN)


def test_unknown_ids_are_dropped():
    a = parse_answer(_answer(categories={"tx_000000000001": "fees", "tx_other": "cash"}), KNOWN)
    assert a.categories == {"tx_000000000001": "fees"}


# -- adapters -------------------------------------------------------------------------------------


class FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "", write: str | None = None):
        self.calls: list[dict] = []
        self.stdout, self.returncode, self.stderr, self.write = stdout, returncode, stderr, write

    def __call__(self, args, stdin, timeout, cwd=None, env=None):
        self.calls.append({"args": args, "stdin": stdin, "timeout": timeout, "cwd": cwd, "env": env})
        if self.write is not None and "-o" in args:
            Path(args[args.index("-o") + 1]).write_text(self.write, encoding="utf-8")
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def on_path(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda name: f"/bin/{name}")


def test_cli_detection(monkeypatch, on_path):
    run = FakeRun("claude 2.1.0 (Claude Code)\n")
    monkeypatch.setattr(ad, "_run", run)
    d = ad.ClaudeCli().detect()
    assert d.available and d.detail == "claude 2.1.0 (Claude Code) found on PATH"
    assert run.calls[0]["args"] == ["/bin/claude", "--version"]
    monkeypatch.setattr(ad, "_run", FakeRun(returncode=1))
    assert not ad.CodexCli().detect().available


def test_cli_missing():
    d = ad.GeminiCli().detect()
    assert not d.available and d.detail == "gemini not found on PATH"
    with pytest.raises(ad.AdapterError):
        ad.GeminiCli().complete("x")


def test_claude_adapter(monkeypatch, on_path):
    run = FakeRun(json.dumps({"type": "result", "is_error": False, "result": '{"a": 1}'}))
    monkeypatch.setattr(ad, "_run", run)
    assert ad.ClaudeCli().complete("PROMPT", timeout=180) == '{"a": 1}'
    call = run.calls[0]
    assert call["args"] == ["/bin/claude", "-p", "--output-format", "json"]
    assert call["stdin"] == "PROMPT" and call["timeout"] == 180 and call["cwd"]
    monkeypatch.setattr(ad, "_run", FakeRun(json.dumps({"is_error": True, "result": "limit reached"})))
    with pytest.raises(ad.AdapterError, match="limit reached"):
        ad.ClaudeCli().complete("PROMPT")


def test_codex_adapter_reads_output_file(monkeypatch, on_path):
    run = FakeRun(stdout="log noise", write='{"from": "file"}')
    monkeypatch.setattr(ad, "_run", run)
    assert ad.CodexCli().complete("PROMPT") == '{"from": "file"}'
    args = run.calls[0]["args"]
    assert args[:3] == ["/bin/codex", "exec", "--skip-git-repo-check"] and "-o" in args and args[-1] == "-"
    assert run.calls[0]["stdin"] == "PROMPT"


def test_agy_and_gemini_adapters(monkeypatch, on_path):
    run = FakeRun("answer")
    monkeypatch.setattr(ad, "_run", run)
    assert ad.AgyCli().complete("PROMPT") == "answer"
    assert run.calls[0]["args"] == ["/bin/agy", "-p", "PROMPT"]
    assert ad.GeminiCli().complete("PROMPT") == "answer"
    assert run.calls[1]["args"][:2] == ["/bin/gemini", "-p"] and run.calls[1]["stdin"] == "PROMPT"
    assert run.calls[1]["env"] == {"GEMINI_CLI_TRUST_WORKSPACE": "true"}


def test_cli_exit_code_is_an_error(monkeypatch, on_path):
    monkeypatch.setattr(ad, "_run", FakeRun(returncode=2, stderr="boom"))
    with pytest.raises(ad.AdapterError, match="exited with code 2: boom"):
        ad.AgyCli().complete("x")


def test_timeout_kills_the_child():
    import sys
    import time

    t0 = time.monotonic()
    with pytest.raises(ad.AdapterError, match="within 1 s"):
        ad._run([sys.executable, "-c", "import time; time.sleep(60)"], None, 1)
    assert time.monotonic() - t0 < 20


def test_child_never_inherits_our_stdin():
    import sys

    proc = ad._run([sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"], None, 30)
    assert proc.stdout.strip() == "0"
    proc = ad._run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], "PROMPT", 30)
    assert proc.stdout.strip() == "PROMPT"


def test_openai_compatible_needs_config_and_never_shows_key(monkeypatch):
    a = ad.OpenAICompatible()
    assert not a.detect().available
    monkeypatch.setenv("KALAN_OPENAI_BASE_URL", "https://api.example.test/v1/")
    monkeypatch.setenv("KALAN_OPENAI_MODEL", "some-model")
    assert "API key" in a.detect().detail
    monkeypatch.setenv("KALAN_OPENAI_API_KEY", "sk-test-not-a-real-key")
    d = a.detect()
    assert d.available and "sk-test" not in d.detail and d.detail == "api.example.test, model some-model"
    seen = {}

    def fake_http(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=headers, timeout=timeout)
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(ad, "_http_json", fake_http)
    assert a.complete("PROMPT", timeout=180) == "{}"
    assert seen["url"] == "https://api.example.test/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test-not-a-real-key"
    assert seen["body"]["messages"] == [{"role": "user", "content": "PROMPT"}] and seen["timeout"] == 180


def test_http_error_message_has_no_secret(monkeypatch):
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(ad.urllib.request, "urlopen", boom)
    with pytest.raises(ad.AdapterError) as exc:
        ad._http_json("https://api.example.test/v1/chat/completions", {}, {"Authorization": "Bearer sk-x"}, 1)
    assert str(exc.value) == "HTTP 401 from api.example.test"


def test_local_model_only_localhost(monkeypatch):
    a = ad.LocalModel()
    assert a.detect().detail == "KALAN_LOCAL_URL must point at localhost"  # conftest points it elsewhere
    monkeypatch.setenv("KALAN_LOCAL_URL", "http://127.0.0.1:18500/v1")
    monkeypatch.setattr(ad, "_http_json", lambda url, body, headers, timeout: (
        {"data": [{"id": "gemma"}]} if url.endswith("/models") else {"choices": [{"message": {"content": "ok"}}]}))
    d = a.detect()
    assert d.available and d.detail == "127.0.0.1:18500, model gemma"
    assert a.complete("PROMPT") == "ok"


def test_registry_caches_and_survives_broken_detection():
    class Broken(ad.FakeAdapter):
        def detect(self):
            raise RuntimeError("x")

    reg = ad.ProviderRegistry([Broken(id="broken"), ad.FakeAdapter()], ttl=60)
    first = reg.detect_all()
    assert not first["broken"].available and first["fake"].available
    assert reg.detect_all() is first
    assert reg.detect_all(refresh=True) is not first
