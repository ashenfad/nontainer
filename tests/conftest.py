import hashlib
import os

import pytest

from nontainer import Workspace
from nontainer.providers import DirProvider


@pytest.fixture(scope="module")
def chromium_available():
    """Skip cleanly where the [apps] browser isn't installed. Shared:
    both test_app suites (behavior and page-error frames) need it."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"chromium unavailable: {e}")


@pytest.fixture
def dir_ws(tmp_path):
    """A dir-backed workspace with default (stdlib-only) python config."""
    provider = DirProvider(tmp_path / "ws", session="test-session")
    ws = Workspace(provider)
    yield ws
    ws.close()


# NONTAINER_TEST_KV=postgres reruns the suite with every Store keeping its
# kvgit data in PostgreSQL instead of on disk: one table per store path,
# so two Store objects on one path share a store exactly as they share a
# directory. NONTAINER_TEST_PG_DSN names the database (default
# dbname=nontainer_test); it is dropped and recreated.
if os.environ.get("NONTAINER_TEST_KV") == "postgres":
    import sys

    import psycopg
    from kvgit.kv.postgres import Postgres
    from psycopg_pool import ConnectionPool

    import nontainer.store  # noqa: F401 - loads the module patched below

    _dsn = os.environ.get("NONTAINER_TEST_PG_DSN", "dbname=nontainer_test")
    _dbname = psycopg.conninfo.conninfo_to_dict(_dsn)["dbname"]
    _admin = psycopg.conninfo.make_conninfo(_dsn, dbname="postgres")
    with psycopg.connect(_admin, autocommit=True) as _c:
        _c.execute(f'DROP DATABASE IF EXISTS "{_dbname}" WITH (FORCE)')
        _c.execute(f'CREATE DATABASE "{_dbname}"')
    _pool = ConnectionPool(
        _dsn, min_size=2, max_size=32, kwargs={"autocommit": True}, open=True
    )

    def _postgres_backend(path, create):
        table = "t_" + hashlib.sha1(str(path).encode()).hexdigest()[:24]
        return Postgres(pool=_pool, table=table)

    # ``nontainer.store`` names the store() sugar, which shadows its
    # module, so the module is patched through sys.modules.
    sys.modules["nontainer.store"]._disk_backend = _postgres_backend

    def pytest_unconfigure(config):
        _pool.close()

    def pytest_collection_modifyitems(config, items):
        skip = pytest.mark.skip(reason="exercises the on-disk kvgit layout")
        for item in items:
            if "disk_only" in item.keywords:
                item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "disk_only: exercises the on-disk kvgit layout itself; skipped "
        "when the suite runs with NONTAINER_TEST_KV=postgres",
    )
