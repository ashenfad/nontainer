"""An agent builds a guestbook, then it's served FROZEN — the idiomatic shape.

The lesson this example teaches: an app's mutable state does NOT go in the
workspace (cache/files). It goes to an external store injected via
`host_objects`, and the agent is told about that store with a
`python_primer`. The workspace holds only the app itself — so it can be
frozen and served read-only, concurrently, while the store (here a
thread-safe SQLite) owns its own state and its own locking.

Flow:
  1. Author: the agent builds the guestbook using the injected `db`
     (SQL), verifies it with curl + test_app.  (needs an API key)
  2. Publish: commit, then publish app/ — a derived, immutable commit
     that nothing can write to, opened with the `db` the handlers call.
  3. Serve: build_router serves that frozen workspace read-only; POSTs
     still work because the mutation lands in SQLite, not the VFS.
     (runs without a key)

Run:  ANTHROPIC_API_KEY=... uv run python examples/webapp.py
      (without a key, the app is seeded and only the serving demo runs)
Deps: pip install nontainer[agno,apps] anthropic
      playwright install chromium
"""

import os
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from nontainer import PythonConfig, Store
from nontainer.apps import build_router, enable_apps


class Db:
    """A tiny thread-safe SQLite store, injected as ``db``. Frozen serving
    calls handlers CONCURRENTLY, so the store — not nontainer — owns the
    locking; nontainer serves lock-free. This is the external store an
    app graduates to when it needs shared mutable state."""

    def __init__(self, path: str) -> None:
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()

    def execute(self, sql: str, params: tuple = ()) -> None:
        """A write (INSERT/UPDATE/CREATE TABLE); commits."""
        with self._lock:
            self._c.execute(sql, params)
            self._c.commit()

    def query(self, sql: str, params: tuple = ()) -> list:
        """A read (SELECT); returns a list of row tuples."""
        with self._lock:
            return self._c.execute(sql, params).fetchall()


DB_PRIMER = (
    "`db` is a shared SQLite store for PERSISTENT app state (it survives "
    "across requests and is shared across users). Use it — NOT `cache` — "
    "for anything the app must remember. API: `db.execute(sql, params=())` "
    "for writes (INSERT / UPDATE / `CREATE TABLE IF NOT EXISTS`), and "
    "`db.query(sql, params=()) -> list of row tuples` for reads. It is "
    "thread-safe; just call it."
)

TASK = """\
Build a guestbook web app in this workspace, backed by the `db` SQLite store:

1. Backend app/api/entries.py:
   - Ensure the table: db.execute("CREATE TABLE IF NOT EXISTS entries (name TEXT)")
     at the top of each handler (idempotent).
   - get(req)  -> {"entries": [row[0] for row in db.query("SELECT name FROM entries")]}
   - post(req) -> name = req.require("name"); db.execute(
       "INSERT INTO entries (name) VALUES (?)", (name,)); return {"ok": True}
2. Test with curl: GET (empty), POST a couple of names, GET again.
3. Frontend app/index.html — plain JS, RELATIVE urls — list + input + add button.
4. Verify with test_app: add an entry through the UI, assert the list grew,
   take a screenshot.

Report what you verified. Do NOT use `cache` for the guestbook data — it's
shared, so it belongs in `db`."""

# The app the agent would write — used when no API key is present so the
# serving demo always runs.
SEED_HANDLER = """\
def get(req):
    db.execute("CREATE TABLE IF NOT EXISTS entries (name TEXT)")
    return {"entries": [r[0] for r in db.query("SELECT name FROM entries")]}

def post(req):
    db.execute("CREATE TABLE IF NOT EXISTS entries (name TEXT)")
    db.execute("INSERT INTO entries (name) VALUES (?)", (req.require("name"),))
    return {"ok": True}
"""

SEED_HTML = b"""<!doctype html><html><body>
<h1>Guestbook</h1><ul id="list"></ul>
<input id="n"/><button id="add">add</button>
<script>
const load = async () => {
  const r = await fetch('api/entries');           // RELATIVE
  const {entries} = await r.json();
  document.getElementById('list').innerHTML =
    entries.map(e => `<li>${e}</li>`).join('');
};
document.getElementById('add').onclick = async () => {
  await fetch('api/entries', {method:'POST',
    body: JSON.stringify({name: document.getElementById('n').value})});
  await load();
};
load();
</script></body></html>"""


def _has_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "guestbook.db")
    db = Db(db_path)

    store = Store(tempfile.mkdtemp())
    ws = store.open("guestbook", python=PythonConfig(host_objects={"db": db}))
    runtime = enable_apps(ws)

    if _has_key():
        from _model import pick_model  # noqa: I001
        from agno.agent import Agent

        from nontainer.adapters.agno import WorkspaceTools

        agent = Agent(
            model=pick_model(),
            tools=[WorkspaceTools(ws, apps=runtime, python_primer=DB_PRIMER)],
            tool_call_limit=30,
        )
        print("=== agent (authoring) ===")
        print(agent.run(TASK).content)
    else:
        print("(no API key — seeding the app so the serving demo runs)")
        # The app tree is <ws.root>/app — /workspace/app by default,
        # the same path `$APP_ORIGIN` dispatches against.
        ws.files.write(f"{ws.root}/app/api/entries.py", SEED_HANDLER)
        ws.files.write(f"{ws.root}/app/index.html", SEED_HTML)

    # -- the publish point: the app subtree, derived and frozen ---------
    # `publish` names a commit, so land what the session is holding
    # first. What it derives holds `app/` and nothing else — not the
    # transcript, not the cache, not the scratch files — which is what
    # makes the whole served tree safe to hand to the internet.
    #
    # A publication carries the tree; `db` is a live SQLite handle, and
    # a live handle is not a file. So the embedder hands it over at the
    # open, and the frozen workspace the router serves reaches exactly
    # the store this process owns. Serve the same version twice with two
    # databases and you have two deployments of one app.
    ws.commit()
    pub = store.publish(ws, "guestbook")
    snapshot = pub.open(python=PythonConfig(host_objects={"db": db}))

    print("\n=== serving frozen (read-only VFS; state lives in SQLite) ===")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    router = build_router(lambda t: snapshot if t == "demo" else None)
    app = Starlette()
    app.mount("/apps", router)
    client = TestClient(app)

    def post(name: str):
        return client.post("/apps/demo/api/entries", content=f'{{"name": "{name}"}}')

    # concurrent POSTs — nontainer serves lock-free; SQLite serializes them
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(post, [f"user{i}" for i in range(6)]))

    entries = client.get("/apps/demo/api/entries").json()["entries"]
    print(f"entries after 6 concurrent POSTs: {sorted(entries)}")
    print("served index.html ok:", client.get("/apps/demo/").status_code == 200)

    # the VFS never mutated — the app is still a clean, frozen artifact
    print(
        "workspace uncommitted after serving:",
        ws.uncommitted,
        "(state is in SQLite, not the VFS)",
    )
    snapshot.close()
    ws.close()
    store.close()


if __name__ == "__main__":
    main()
