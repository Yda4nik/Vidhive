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
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "tests" / "_test.db"
os.environ["VIDHIVE_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"

from sqlalchemy import create_engine  # noqa: E402

from app.db.base import Base  # noqa: E402
import app.db.models  # noqa: E402,F401  (registers tables)


def _unlink_when_free(path: pathlib.Path, tries: int = 40) -> None:
    """Delete the DB file, waiting out a lingering handle.

    aiosqlite closes its connection on a background thread, so just after the
    previous test disposes the engine the file can stay briefly locked on
    Windows (WinError 32). Unlinking an open file never blocks on Linux.
    """
    for _ in range(tries):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)
    path.unlink()  # give up waiting and surface the real error


@pytest.fixture()
def client():
    _unlink_when_free(DB_FILE)
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

    # Release the SQLite file handle so the next test can recreate the DB.
    # (Unlinking an open file is fine on Linux but fails on Windows — WinError 32.)
    # aiosqlite keeps its connection on a background thread, so it needs a real
    # async dispose, not just disposing the sync pool.
    import asyncio
    import gc

    engine = sess.get_engine()
    try:
        asyncio.run(engine.dispose())
    except Exception:  # noqa: BLE001 - fall back to the sync pool teardown
        engine.sync_engine.dispose()
    gc.collect()


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
