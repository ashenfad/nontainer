"""The whole surface, end to end, with no LLM in the loop.

Every verb an embedder reaches for, in the order a real session would
meet them: open a store and a session, run a shell command and some
python, compose a commit through the agent's own index, delegate to a
fork with a narrowed view, merge it back, take one file from a session
you did not fork, read another session's tree in place, publish a
subtree and open the publication frozen.

Nothing here needs a model: the "agent" is this script, calling the
same verbs an agent's tools call.

Run:  uv run python examples/tour.py
"""

import tempfile

from nontainer import Store, WorkspaceError
from nontainer.wsgit import register_wsgit

APP_PAGE = """<!doctype html>
<title>Rates</title>
<h1>Rates</h1>
"""


def step(n: int, what: str) -> None:
    print(f"\n=== {n}. {what} " + "=" * max(0, 58 - len(what)))


def main() -> None:
    store = Store(tempfile.mkdtemp())
    ws = store.open("analyst")
    # ws-git is an opt-in terminal builtin, like apps: the embedder
    # decides whether this session's agent gets the versioning verbs.
    register_wsgit(ws)

    step(1, "a terminal and a python call, each its own commit")
    print(
        ws.terminal(
            "mkdir -p data\n"
            "cat > data/rates.csv <<'EOF'\n"
            "region,rate\nnorth,4\nsouth,7\n"
            "EOF\n"
            "ls data"
        ).stdout.strip()
    )
    r = ws.run_python(
        "import csv\n"
        "rows = list(csv.DictReader(open('data/rates.csv')))\n"
        "cache['best'] = max(rows, key=lambda r: int(r['rate']))['region']\n"
        "total = sum(int(r['rate']) for r in rows)"
    )
    print("total =", r.namespace["total"], "| cache['best'] =", ws.cache["best"])
    print("commits so far:", len(list(ws.log())))

    step(2, "compose a commit through the agent's index")
    ws.files.write("/workspace/report.md", "# Rates\n\nnorth 4, south 7\n")
    ws.files.write("/workspace/scratch.txt", "half-formed thought\n")
    ws.index.stage(["/workspace/report.md"])
    agent_commit = ws.index.commit("first report")
    print("ws-git status:", repr(ws.terminal("ws-git status").stdout))
    print("ws-git log:", ws.terminal("ws-git log").stdout.strip())
    print(
        "the scratch file stayed out of it:",
        "/workspace/scratch.txt" not in ws._provider.files_at(agent_commit),
    )

    step(3, "delegate: a fork that sees one file and starts a fresh chat")
    child = ws.fork("polish", inherit="fresh", paths=["report.md"])
    print("what the delegate can see:", child.files.list("/workspace"))
    child.files.write("/workspace/report.md", "# Rates\n\nNorth 4, South 7.\n")
    child.files.write("/workspace/notes.md", "capitalized the regions\n")
    child.index.commit("polished")
    try:
        child.files.write("/workspace/data/rates.csv", "tampered\n")
    except PermissionError as e:
        print("the write rule:", str(e).split(" — ")[0])

    step(4, "look before you merge, then merge")
    d = ws.diff(ws.head, child.index.head)
    print("in what I sent it to do:", sorted(d.in_seed))
    print("elsewhere:", sorted(d.elsewhere))
    # A merge takes only what has been committed, on BOTH sides — and
    # this session still has its own work in flight.
    try:
        ws.merge("polish")
    except WorkspaceError as e:
        print("refused:", str(e).split(", and a merge")[0])
    ws.terminal("ws-git commit -m 'my own work so far'")

    out = ws.merge("polish")
    print("merged:", out.merged, "| conflicts:", out.conflicts)
    print("report.md now:", ws.files.read("/workspace/report.md").decode().strip())
    child.close()

    step(5, "take one file from a session you did not fork")
    other = store.open("colleague")
    other.files.write("/workspace/method.md", "how the rates were sampled\n")
    other.commit(info={"tool": "seed"})
    other.close()
    ws.checkout("colleague", paths=["method.md"])
    print("taken:", ws.files.read("/workspace/method.md").decode().strip())
    print("provenance:", next(iter(ws.log(limit=1))).info)

    step(6, "attach another session's tree and read it in place")
    at = ws.files.attach("colleague", "reviews")
    print("attached", at, "->", ws.files.attachments())
    print(ws.terminal("cat reviews/method.md").stdout.strip())
    ws.files.detach("reviews")

    step(7, "publish a subtree, and open the publication frozen")
    ws.files.write("/workspace/app/index.html", APP_PAGE)  # autocommit lands it
    pub = store.publish(ws, "rates", paths=("app/",))
    version = pub.current_version
    print("published", pub.name, version.version, "from", version.published_from)
    frozen = store.resolve(version.ref)
    try:
        print(
            "served page:",
            frozen.files.read("/workspace/app/index.html").splitlines()[0].decode(),
        )
        print(
            "and nothing else came with it:",
            frozen.files.list("/workspace", recursive=True),
        )
    finally:
        frozen.close()

    step(8, "two histories over one branch")
    print("the store's, newest first — every durability point:")
    for c in list(ws.log())[:6]:
        print(f"  {c.id[:10]}  {c.info.get('tool', '?')}")
    print("\nthe agent's own, which is all ws-git log shows:")
    for line in ws.terminal("ws-git log").stdout.splitlines():
        print(" ", line)
    print("\nsessions on the store:", store.sessions())

    ws.close()
    store.close()


if __name__ == "__main__":
    main()
