from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalan.ai.adapters import FakeAdapter, ProviderRegistry
from kalan.api import create_app
from kalan.config import Config
from kalan.store import Store

VB = Path(__file__).parent / "fixtures" / "vakifbank"
VB_PDFS = sorted(VB.glob("vb-*.pdf"))


@pytest.fixture(scope="session")
def _empty_data_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("kalan-data")


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, _empty_data_dir):
    """No test may read the user's real data folder (or their own bank templates)."""
    monkeypatch.setenv("KALAN_DATA_DIR", str(_empty_data_dir))


@pytest.fixture(autouse=True)
def _no_real_providers(monkeypatch):
    """No test may reach a real CLI, the keyring or a real model server."""
    monkeypatch.setattr("kalan.ai.adapters.shutil.which", lambda name: None)
    monkeypatch.setattr("kalan.ai.adapters._keyring_secret", lambda username: None)
    for var in ("KALAN_OPENAI_BASE_URL", "KALAN_OPENAI_MODEL", "KALAN_OPENAI_API_KEY", "KALAN_LOCAL_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KALAN_LOCAL_URL", "http://example.invalid/v1")


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=tmp_path)


@pytest.fixture
def store(config) -> Store:
    return Store(config.db_path)


@pytest.fixture
def fake() -> FakeAdapter:
    return FakeAdapter([])


@pytest.fixture
def registry(fake) -> ProviderRegistry:
    return ProviderRegistry([fake, FakeAdapter(id="fake-off", available=False)], ttl=0)


@pytest.fixture
def client(config, store, registry) -> TestClient:
    app = create_app(config=config, store=store, registry=registry, job_timeout=5)
    with TestClient(app) as c:
        yield c


def upload(client: TestClient, path: Path):
    return client.post("/statements", files={"file": (path.name, path.read_bytes(), "application/pdf")})


class MemoryKeyring:
    """Stands in for the OS keyring in every test."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.data.get((service, user))

    def set_password(self, service: str, user: str, password: str) -> None:
        self.data[(service, user)] = password

    def delete_password(self, service: str, user: str) -> None:
        del self.data[(service, user)]


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch) -> MemoryKeyring:
    ring = MemoryKeyring()
    monkeypatch.setattr("kalan.pdf_passwords._keyring", lambda: ring)
    return ring


@pytest.fixture
def rules_off(monkeypatch):
    """Statements arrive uncategorised, as before rule-based categories: tests of the AI path alone."""
    monkeypatch.setattr("kalan.categories.apply", lambda transactions, user_rules=(): list(transactions))
