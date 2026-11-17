"""Importable trusted worker factories for isolated verifier regressions."""

import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from nontainer import Store
from nontainer.apps import AppRuntime, AppsConfig


@contextmanager
def trusted_store(store_path):
    pool = None
    kv = None
    if os.environ.get("NONTAINER_TEST_KV") == "postgres":
        import psycopg
        from kvgit.kv.postgres import Postgres
        from psycopg_pool import ConnectionPool

        base = os.environ.get("NONTAINER_TEST_PG_DSN", "dbname=nontainer_test")
        database = psycopg.conninfo.conninfo_to_dict(base)["dbname"]
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if worker:
            database += "_" + worker
        pool = ConnectionPool(
            psycopg.conninfo.make_conninfo(base, dbname=database),
            min_size=1,
            max_size=2,
            kwargs={"autocommit": True},
        )
        table = (
            "t_"
            + hashlib.sha1(str(Path(store_path) / "kvgit").encode()).hexdigest()[:24]
        )
        kv = Postgres(pool=pool, table=table)
    store = Store(store_path, kv=kv)
    try:
        yield store
    finally:
        store.close()
        if pool is not None:
            pool.close()


@contextmanager
def factory(context, *, log_sink, screenshot_sink):
    with trusted_store(context.config["store"]) as store:
        with store.resolve(context.ref) as snapshot:
            yield AppRuntime(
                snapshot,
                AppsConfig(readonly_handlers=True),
                log_sink=log_sink,
                screenshot_sink=screenshot_sink,
            )


@contextmanager
def blocked_handler(context, *, log_sink, screenshot_sink):
    from nontainer.apps.contract import WireResponse

    with factory(
        context, log_sink=log_sink, screenshot_sink=screenshot_sink
    ) as runtime:
        original = runtime.dispatch

        def dispatch(request, **kwargs):
            if "api/blocked" in request.path:
                Path(context.config["entered"]).write_text("entered")
                threading.Event().wait(60)
                Path(context.config["late_write"]).write_text("unsafe late callback")
                return WireResponse(200, b"{}", "application/json")
            return original(request, **kwargs)

        runtime.dispatch = dispatch
        yield runtime


@contextmanager
def failing_factory(context, **kwargs):
    raise RuntimeError("synthetic worker construction failure")
    yield


def owner(store_path, phases_path, entered_path, late_path):
    import json
    import tempfile

    tempfile.tempdir = str(Path(phases_path).parent)

    from nontainer.apps import VerificationWorker, enable_apps

    with trusted_store(store_path) as store:
        ws = store.open("draft")
        runtime = enable_apps(ws, AppsConfig(readonly_handlers=True))

        def observe(event):
            if event["phase"] == "running":
                Path(phases_path).write_text(json.dumps(event))

        VerificationWorker(
            "verification_worker_support:blocked_handler",
            {"store": store_path, "entered": entered_path, "late_write": late_path},
            timeout_seconds=60,
            observer=observe,
        ).verify(runtime, [{"eval": "fetch('api/blocked').then(r=>r.json())"}])
