"""The apps ↔ workspace extension surface (see scratch proposal).

Apps talks to `Workspace` exclusively through the documented extension
surface — `exec_python(view=...)`, `lock` — plus the ordinary public
API (`fs`, `caps`, `dirty`, `discard`, `cache`, ...). No private
attribute access, no sentinel imports: that contract is what protects
apps from core churn and makes it portable across providers.
"""

import re
from pathlib import Path

from nontainer import Workspace
from nontainer.apps import enable_apps, request
from nontainer.providers import DirProvider, KvgitProvider

APPS_DIR = Path(__file__).resolve().parents[1] / "nontainer" / "apps"

# Workspace internals apps must NOT touch (the acceptance test for the
# extension-surface refactor, made executable). `_ws`/`_dispatch_*`
# style attributes on apps' own classes are fine — this polices the
# seam to core, not intra-package structure.
_FORBIDDEN = (
    r"\._exec_python\b",
    r"\._build_sandbox\b",
    r"\._build_policy\b",
    r"\._provider\b",
    r"\b_UNSET\b",
    r"\._lock\b",
    r"\._policy_memo\b",
)


def test_apps_touches_no_workspace_internals():
    offenders: list[str] = []
    for path in sorted(APPS_DIR.glob("*.py")):
        source = path.read_text()
        for pattern in _FORBIDDEN:
            for m in re.finditer(pattern, source):
                line = source.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.name}:{line}: {m.group()}")
    assert not offenders, "apps reaches past the extension surface:\n" + "\n".join(
        offenders
    )


# -- portability: the same apps code runs on another provider ---------------


HANDLER = """
def get(req):
    return {"n": len(cache.get("names", []))}

def post(req):
    names = cache.get("names", [])
    names.append(req.require("name"))
    cache["names"] = names
    open("/workspace/app/last.txt", "w").write(names[-1])
    return {"n": len(names)}
"""


def _exercise(ws: Workspace) -> None:
    runtime = enable_apps(ws)
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write("/workspace/app/api/names.py", HANDLER.encode())

    # mutating verb via direct dispatch
    r = runtime.dispatch(request("POST", "/api/names", body=b'{"name": "amy"}'))
    assert r.status == 200, r.text
    # GET is read-only (write to cache would 500) and sees the state
    r = runtime.dispatch(request("GET", "/api/names"))
    assert r.status == 200 and b'"n": 1' in r.content
    # the curl builtin drives the same dispatch from inside terminal()
    t = ws.terminal('curl -X POST -d \'{"name": "bo"}\' /api/names')
    assert t, t.stderr
    assert ws.fs.read("/workspace/app/last.txt") == b"bo"


def test_apps_runs_on_kvgit_provider():
    ws = Workspace(KvgitProvider.open(None, session="surface-kvgit"))
    try:
        _exercise(ws)
    finally:
        ws.close()


def test_apps_runs_on_dir_provider(tmp_path):
    """An unversioned, non-staging provider: same apps code, unchanged —
    atomicity degrades honestly (caps-gated), nothing reaches internals."""
    ws = Workspace(DirProvider(tmp_path / "ws", session="surface-dir"))
    try:
        _exercise(ws)
    finally:
        ws.close()


# -- view-sandbox policy memo (finding 3a) -----------------------------------
# The memo moved behind the seam when build_sandbox dissolved into
# exec_python(view=): LocalExecutor mints a fresh sandbox per view but
# reuses one memoized Policy per (timeout, tick_limit, extra_classes).
# These are white-box on the LocalExecutor — the perf property they pin
# (registration is not re-run per handler call) has no public surface.


def test_view_sandbox_memoizes_policy():
    from nontainer.executor import ViewSpec

    ws = Workspace(KvgitProvider.open(None, session="memo"))
    try:
        ex = ws._executor
        view = ViewSpec(timeout=5.0, tick_limit=1000)
        sb1 = ex._build_sandbox(view)
        sb2 = ex._build_sandbox(view)
        assert sb1 is not sb2  # fresh sandbox instances...
        assert sb1.policy is sb2.policy  # ...sharing one memoized policy
        other = ex._build_sandbox(ViewSpec(timeout=9.0, tick_limit=1000))
        assert other.policy is not sb1.policy  # distinct budgets, distinct policy
    finally:
        ws.close()


def test_view_extra_classes_list_shares_memo():
    """``ViewSpec.extra_classes`` may arrive as a list (a frozen
    dataclass stores what it's handed); ``_build_sandbox`` coerces to a
    tuple for the memo key, so a list and the equivalent tuple share one
    policy (PR #7 review)."""
    from nontainer.executor import ViewSpec

    class Marker:
        pass

    ws = Workspace(KvgitProvider.open(None, session="memo-list"))
    try:
        ex = ws._executor
        as_list = ex._build_sandbox(ViewSpec(extra_classes=[Marker]))
        as_tuple = ex._build_sandbox(ViewSpec(extra_classes=(Marker,)))
        assert as_list.policy is as_tuple.policy
    finally:
        ws.close()
