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
