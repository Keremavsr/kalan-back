"""Remembered PDF passwords, one per bank, kept only in the OS keyring.

Many banks send encrypted e-statements. When the user asks kalan to remember the password for a bank, it goes
to the OS keyring (service :data:`KEYRING_SERVICE`, user name = bank id) and nowhere else: not SQLite, not a
file, not a log line. Which banks have a remembered password is read back from the keyring too, so nothing
about passwords lives in kalan's own data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from kalan.pdf import PasswordError, extract_text

KEYRING_SERVICE = "kalan-pdf-password"

log = logging.getLogger("kalan.pdf_passwords")


class KeyringUnavailableError(RuntimeError):
    """No usable OS keyring backend."""


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except Exception as exc:  # package missing or broken
        raise KeyringUnavailableError("keyring is not installed") from exc
    if isinstance(keyring.get_keyring(), FailKeyring):
        raise KeyringUnavailableError("no OS keyring backend")
    return keyring


def keyring_available() -> bool:
    try:
        _keyring()
    except KeyringUnavailableError:
        return False
    return True


def get(bank: str) -> str | None:
    """The remembered password for ``bank``, or None (also when there is no keyring)."""
    try:
        return _keyring().get_password(KEYRING_SERVICE, bank)
    except Exception:
        return None


def remember(bank: str, password: str) -> None:
    """Store ``password`` for ``bank``. Raises :class:`KeyringUnavailableError` without a keyring."""
    try:
        _keyring().set_password(KEYRING_SERVICE, bank, password)
    except KeyringUnavailableError:
        raise
    except Exception as exc:
        raise KeyringUnavailableError(type(exc).__name__) from None
    log.info("pdf password remembered for bank %s", bank)


def forget(bank: str) -> bool:
    """Delete the remembered password for ``bank``. False when there was none."""
    if get(bank) is None:
        return False
    try:
        _keyring().delete_password(KEYRING_SERVICE, bank)
    except Exception:
        return False
    log.info("pdf password forgotten for bank %s", bank)
    return True


def remembered_banks(banks: Iterable[str]) -> list[str]:
    return [b for b in banks if get(b) is not None]


def open_with_remembered(
    source: str | Path | bytes, banks: Iterable[str], read: Callable[..., str] = extract_text
) -> tuple[str, str] | None:
    """Try every remembered password on an encrypted PDF: ``(text, bank)`` of the first that opens it."""
    for bank in banks:
        password = get(bank)
        if password is None:
            continue
        try:
            return read(source, password=password), bank
        except PasswordError:
            continue
    return None
