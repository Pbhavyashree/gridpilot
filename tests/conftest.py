import os
import tempfile

# Point the app at a throwaway SQLite file BEFORE any app module is imported.
_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["HTTP_RETRIES"] = "1"

import pytest  # noqa: E402

from app import config  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    monkeypatch.setattr(config, "HTTP_RETRIES", 1)
    init_db()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s
