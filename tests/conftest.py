"""Shared pytest fixtures: TestClient + a fresh schema."""
import os
import sys

# point the ORM at an isolated database BEFORE importing app modules
os.environ.setdefault(
    "PETRI_DATABASE_URL",
    "postgresql+psycopg2://petri@localhost:55432/petritest?host=/tmp")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
    # raw SQL because the models<->versions FK cycle prevents drop_all's
    # topological sort
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS petri_publications CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS petri_state_edges CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS petri_state_nodes CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS petri_check_runs CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS petri_versions CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS petri_models CASCADE"))


@pytest.fixture()
def client():
    # isolate tests: clear business tables before each test
    with engine.begin() as conn:
        for t in ("petri_publications", "petri_state_edges",
                  "petri_state_nodes", "petri_check_runs",
                  "petri_versions", "petri_models"):
            conn.execute(text(f"TRUNCATE TABLE {t} RESTART IDENTITY "
                              "CASCADE"))
    with TestClient(app) as c:
        yield c
