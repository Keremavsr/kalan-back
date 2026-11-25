"""Runtime configuration read from the environment (never from the database).

* ``KALAN_DATA_DIR``: folder for the SQLite file (default: the per-user data dir from ``platformdirs``).
* ``KALAN_CORS_ORIGINS``: comma-separated allowlist of browser origins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_dir

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DB_FILE = "kalan.db"


def data_dir() -> Path:
    override = os.environ.get("KALAN_DATA_DIR")
    return Path(override) if override else Path(user_data_dir("kalan", appauthor=False))


def cors_origins() -> list[str]:
    raw = os.environ.get("KALAN_CORS_ORIGINS")
    if raw is None:
        return list(DEFAULT_CORS_ORIGINS)
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


@dataclass
class Config:
    data_dir: Path = field(default_factory=data_dir)
    cors_origins: list[str] = field(default_factory=cors_origins)
    max_upload_bytes: int = MAX_UPLOAD_BYTES

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE
