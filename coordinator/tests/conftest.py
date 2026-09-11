"""Test fixtures.

Each test gets a fresh SQLite database file. The schema is created with a
synchronous engine (so the async engine is only ever touched inside the app's
own event loop, via TestClient), and a raw-SQL helper lets tests poke internal
state such as an expired lease.
"""

import os
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "tests" / "_test.db"
os.environ["VIDHIVE_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"

from sqlalchemy import create_engine  # noqa: E402

from app.db.base import Base  # noqa: E402
import app.db.models  # noqa: E402,F401  (registers tables)


@pytest.fixture()
def client():
    if DB_FILE.exists():
        DB_FILE.unlink()
    sync_engine = create_engine(f"sqlite:///{DB_FILE.as_posix()}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    # Rebind the app's cached async engine to this fresh file.
    from app.db import session as sess

    sess.get_engine.cache_clear()
    sess.get_sessionmaker.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def raw_sql():
    def run(sql: str, params: tuple = ()):
        con = sqlite3.connect(DB_FILE)
        try:
            cur = con.execute(sql, params)
            con.commit()
            return cur.fetchall()
        finally:
            con.close()

    return run
