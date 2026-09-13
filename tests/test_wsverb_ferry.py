"""The generic ``ws-*`` ferry: one relay, any registered verb.

A framework verb reaches a real-bash guest by being registered and
tagged — no per-verb shell function, host object or executor edit. The
corpus registers a verb the framework does not ship, so what is being
tested is the relay itself rather than any one verb's behaviour.

Requires the ``dud`` extra; skipped when it isn't installed. Pins
``backend="subprocess"`` explicitly (the only rung without a
hypervisor).
"""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider
from nontainer.wsverb import FerrySpec, tag

pytest.importorskip("dud")

from nontainer.executor_dud import DudExecutor  # noqa: E402

DEMO = FerrySpec(verb="ws-demo", map_bare_paths=True, opaque_flags=("-m",))


def _make_demo():
    """Echo the argv the ferry delivered, plus the cwd it reported. A
    fresh closure per registration: the tag is an attribute of the
    function object, so one shared function could not be registered
    tagged in one workspace and untagged in another."""

    def demo(ctx):
        ctx.stdout.write(f"cwd={ctx.fs.getcwd()}\n")
        ctx.stdout.write(" ".join(ctx.args) + "\n")
        return None

    return demo


def _register(ws):
    ws.runtime.register_command("ws-demo", tag(_make_demo(), DEMO), rebind=_register)


@pytest.fixture
def ws():
    w = Workspace(
        KvgitProvider.open(None, session="wsverb-demo"),
        executor=DudExecutor(backend="subprocess"),
    )
    try:
        yield w
    finally:
        w.close()


def test_a_registered_verb_ferries_with_no_executor_edit(ws):
    _register(ws)
    r = ws.terminal("ws-demo status")
    assert r.exit_code == 0, r.stdout
    assert "cwd=/workspace" in r.stdout
    assert "status" in r.stdout


def test_the_ferry_maps_paths_and_leaves_opaque_values_alone(ws):
    _register(ws)
    guest = ws.runtime.executor._work
    r = ws.terminal(f"ws-demo {guest}/a.txt -m {guest}/msg")
    assert r.exit_code == 0, r.stdout
    # The guest names its own tree; the command reads workspace paths.
    # The -m value is free text and crosses verbatim, path-shaped or not.
    assert f"/workspace/a.txt -m {guest}/msg" in r.stdout


def test_an_untagged_command_under_a_ws_name_is_not_ferried(ws):
    """The relay fronts framework registrations only: an embedder's own
    command under a ws- name stays a local-rung creature, so the guest
    has no such name at all."""
    ws.runtime.register_command("ws-demo", _make_demo(), rebind=_register)
    r = ws.terminal("ws-demo status")
    assert r.exit_code == 127
    assert "ws-demo: command not found" in r.stdout
