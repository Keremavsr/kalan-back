"""Encrypted PDFs: password on upload, clear error codes, optional remembering in the OS keyring only."""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf
import pytest

from kalan import pdf_passwords
from kalan.parsers import all_parsers
from kalan.pdf import PasswordError, extract_text
from kalan.pipeline import parse_pdf
from tests.conftest import VB_PDFS

PASSWORD = "ornek-9999"  # invented


def encrypted(path: Path, password: str = PASSWORD) -> bytes:
    with pymupdf.open(str(path)) as doc:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password + "-owner")


def post(client, data: bytes, **form):
    return client.post("/statements", files={"file": ("e.pdf", data, "application/pdf")},
                       data={k: str(v).lower() if isinstance(v, bool) else v for k, v in form.items()})


@pytest.fixture
def locked() -> bytes:
    return encrypted(VB_PDFS[0])


def test_extract_needs_the_password(locked):
    with pytest.raises(PasswordError) as missing:
        extract_text(locked)
    assert missing.value.wrong is False
    with pytest.raises(PasswordError) as wrong:
        extract_text(locked, password="yanlis")
    assert wrong.value.wrong is True
    assert extract_text(locked, password=PASSWORD) == extract_text(VB_PDFS[0])
    assert parse_pdf(locked, password=PASSWORD).parser_id == "tr_vakifbank_worldcard"


def test_password_is_ignored_for_a_plain_pdf():
    assert extract_text(VB_PDFS[0], password="whatever") == extract_text(VB_PDFS[0])


def test_upload_codes(client, locked):
    r = post(client, locked)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "pdf_password_required"
    assert r.json()["error"]["details"]["hints"] == []
    r = post(client, locked, password="yanlis")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "pdf_password_wrong"
    r = post(client, locked, password=PASSWORD)
    assert r.status_code == 201
    assert "X-Kalan-Password-Remembered" not in r.headers


def test_hints_come_from_the_parsers(client, locked, monkeypatch):
    parser = next(p for p in all_parsers() if p.bank == "garanti")
    monkeypatch.setattr(parser, "password_hint", "ORNEK IPUCU")
    r = post(client, locked)
    assert r.json()["error"]["details"]["hints"] == [{"bank": "garanti", "hint": "ORNEK IPUCU"}]
    items = {i["bank"]: i for i in client.get("/pdf-passwords").json()["items"]}
    assert items["garanti"]["hint"] == "ORNEK IPUCU"


def test_remember_then_upload_without_password(client, locked, memory_keyring, store, config, caplog):
    caplog.set_level(logging.DEBUG)
    r = post(client, locked, password=PASSWORD, remember_password=True)
    assert r.status_code == 201
    assert r.headers["X-Kalan-Password-Remembered"] == "true"
    assert memory_keyring.data == {(pdf_passwords.KEYRING_SERVICE, "vakifbank"): PASSWORD}
    listing = client.get("/pdf-passwords").json()
    assert listing["keyring"] is True
    assert {i["bank"] for i in listing["items"] if i["remembered"]} == {"vakifbank"}

    assert client.delete(f"/statements/{r.json()['id']}").status_code == 204
    again = post(client, locked)
    assert again.status_code == 201
    assert again.json()["parser_id"] == "tr_vakifbank_worldcard"

    # the password is in the keyring only: not in the database, not in any log line, not in a response
    for f in config.data_dir.rglob("*"):
        if f.is_file():
            assert PASSWORD.encode() not in f.read_bytes(), f.name
    assert PASSWORD not in caplog.text
    assert PASSWORD not in r.text + again.text + client.get("/pdf-passwords").text


def test_put_and_delete(client, memory_keyring):
    assert client.put("/pdf-passwords/garanti", json={"password": PASSWORD}).status_code == 204
    assert memory_keyring.data[(pdf_passwords.KEYRING_SERVICE, "garanti")] == PASSWORD
    r = client.put("/pdf-passwords/nobank", json={"password": PASSWORD})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_bank"
    assert client.delete("/pdf-passwords/garanti").status_code == 204
    r = client.delete("/pdf-passwords/garanti")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_remembered"
    r = client.put("/pdf-passwords/garanti", json={"password": ""})
    assert r.status_code == 422
    assert PASSWORD not in r.text


def test_no_keyring(client, locked, monkeypatch):
    def unavailable():
        raise pdf_passwords.KeyringUnavailableError("no OS keyring backend")

    monkeypatch.setattr("kalan.pdf_passwords._keyring", unavailable)
    r = client.put("/pdf-passwords/garanti", json={"password": PASSWORD})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "keyring_unavailable"
    assert client.get("/pdf-passwords").json()["keyring"] is False
    r = post(client, locked, password=PASSWORD, remember_password=True)
    assert r.status_code == 201  # the statement is saved even when the password cannot be kept
    assert r.headers["X-Kalan-Password-Remembered"] == "false"
    assert post(client, locked).json()["error"]["code"] == "pdf_password_required"


def test_wrong_remembered_password_is_skipped(client, locked, memory_keyring):
    memory_keyring.set_password(pdf_passwords.KEYRING_SERVICE, "garanti", "eski-sifre")
    assert post(client, locked).json()["error"]["code"] == "pdf_password_required"
    memory_keyring.set_password(pdf_passwords.KEYRING_SERVICE, "vakifbank", PASSWORD)
    assert post(client, locked).status_code == 201
