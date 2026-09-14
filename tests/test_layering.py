"""The layering rule, made executable.

nontainer is one package with five layers:

    CORE          everything under ``nontainer/`` that is not one of the
                  four below -- Store, Workspace, Runtime, the providers,
                  the executors, the ws-git fiction (``agentgit``,
                  ``wsgit``) and the verb ferry (``wsverb``, ``wscurl``)
    SESSIONS      ``nontainer.sessions``
    APPS          ``nontainer.apps.*``
    TESTING VERBS ``nontainer.wspytest``, ``nontainer.wsvitest``
    ADAPTERS      ``nontainer.adapters.*``

The rule: **core imports nothing from the other four; the other four
run on core's public API.** One package, one direction. A later split
into distributions would then be a repackaging rather than a redesign,
and until then the seam stays honest without anybody remembering it.

The reverse invariants below are the ones that hold today and should
keep holding: sessions is built on core alone, the testing verbs are
apps-shaped commands, apps may call a testing verb, and adapters sit on
top of everything because that is what an adapter is.

Every import counts -- module-level and function-local, ``import x``
and ``from x import y``, relative and absolute, and a submodule pulled
in by name (``from . import sessions``) as much as one named in the
``from`` clause. A lazy import inside a function is still a dependency;
it is only a deferred one.
"""

from __future__ import annotations

import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1] / "nontainer"

SESSIONS = "nontainer.sessions"
APPS = "nontainer.apps"
ADAPTERS = "nontainer.adapters"
TESTING_VERBS = ("nontainer.wspytest", "nontainer.wsvitest")

#: The four layers core must not reach into. Core is defined by
#: subtraction: every module under ``nontainer/`` that is not one of
#: these.
NOT_CORE = (SESSIONS, APPS, ADAPTERS, *TESTING_VERBS)


def _module_name(path: pathlib.Path) -> str:
    rel = path.resolve().relative_to(PKG.parent)
    name = str(rel.with_suffix("")).replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def _package_of(module: str, is_init: bool) -> str:
    """The package a module's relative imports resolve against."""
    return module if is_init else module.rsplit(".", 1)[0]


def _parse_imports(tree: ast.AST, package: str) -> set[tuple[str, int]]:
    """Every ``nontainer.*`` module ``tree`` imports, with line numbers.

    Relative imports are resolved against ``package``, so ``from
    ..executor import x`` in an adapter reads as ``nontainer.executor``
    like the absolute spelling would. A ``from`` clause that names a
    package records each imported name as a candidate submodule too:
    ``from . import sessions`` in a core module IS an import of
    ``nontainer.sessions``, and recording only the package it was
    pulled from would let that dependency through unread. A name that
    is not a submodule (``from nontainer import Workspace``) becomes a
    candidate nothing classifies, which is harmless.
    """
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nontainer"):
                    found.add((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if not target.startswith("nontainer"):
                continue
            found.add((target, node.lineno))
            for alias in node.names:
                if alias.name != "*":
                    found.add((f"{target}.{alias.name}", node.lineno))
    return found


def _imports(path: pathlib.Path) -> set[tuple[str, int]]:
    module = _module_name(path)
    package = _package_of(module, path.name == "__init__.py")
    return _parse_imports(ast.parse(path.read_text()), package)


def _modules() -> list[tuple[str, pathlib.Path]]:
    return sorted((_module_name(p), p) for p in PKG.rglob("*.py"))


def _in_layer(module: str, layer: str) -> bool:
    return module == layer or module.startswith(layer + ".")


def _is_core(module: str) -> bool:
    return not any(_in_layer(module, layer) for layer in NOT_CORE)


def _offenders(pick, allowed: tuple[str, ...]) -> list[str]:
    """``file:line: importer -> imported`` for every import a layer makes
    outside core and ``allowed``.

    ``pick`` chooses the layer's modules; ``allowed`` names the
    non-core layers it may import from. Core is always allowed: every
    layer runs on it.
    """
    out: list[str] = []
    for module, path in _modules():
        if not pick(module):
            continue
        for target, lineno in sorted(_imports(path)):
            if _is_core(target):
                continue
            if any(_in_layer(target, layer) for layer in allowed):
                continue
            rel = path.resolve().relative_to(PKG.parent)
            out.append(f"{rel}:{lineno}: {module} -> {target}")
    return sorted(out)


def test_import_of_a_submodule_by_name_is_an_import_of_it():
    """``from . import sessions`` and ``from nontainer import apps``
    name the submodule in the import list rather than the ``from``
    clause; the walk must read them as the dependency they are."""
    tree = ast.parse("from . import sessions\nfrom nontainer import apps, Workspace\n")
    found = {target for target, _ in _parse_imports(tree, "nontainer")}
    assert "nontainer.sessions" in found
    assert "nontainer.apps" in found


def test_core_imports_nothing_above_it():
    """Core is the bottom layer: it may not import sessions, apps, the
    testing verbs or an adapter, not even lazily inside a function."""
    offenders = _offenders(_is_core, allowed=())
    assert not offenders, (
        "core imports from a layer above it (core runs on its own; the "
        "other layers run on core):\n" + "\n".join(offenders)
    )


def test_sessions_imports_only_core():
    """Delegation is built on core's public API alone -- an embedder can
    take sessions without apps, the testing verbs or any adapter."""
    offenders = _offenders(lambda m: _in_layer(m, SESSIONS), allowed=(SESSIONS,))
    assert not offenders, "sessions reaches outside core:\n" + "\n".join(offenders)


def test_testing_verbs_import_only_core_and_apps():
    """``ws-pytest`` and ``ws-vitest`` are apps-shaped commands: they
    run a suite through the apps dispatch, and ws-vitest is ws-pytest's
    report contract on a JS harness. Nothing else is theirs to reach."""
    offenders = _offenders(
        lambda m: any(_in_layer(m, v) for v in TESTING_VERBS),
        allowed=(APPS, *TESTING_VERBS),
    )
    assert not offenders, "a testing verb reaches outside core and apps:\n" + "\n".join(
        offenders
    )


def test_apps_imports_only_core_and_testing_verbs():
    """Apps may call a testing verb (the dispatch registers them as
    commands), and otherwise runs on core. It knows nothing of sessions
    or of any adapter."""
    offenders = _offenders(lambda m: _in_layer(m, APPS), allowed=(APPS, *TESTING_VERBS))
    assert not offenders, (
        "apps reaches outside core and the testing verbs:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# no reach-ins: the layers above core use its public names
# ---------------------------------------------------------------------------


#: Reads of a core-defined private name that are NOT reach-ins, keyed
#: by ``(module, receiver as spelled, attribute)`` so an exemption names
#: the one object it is about and covers no other receiver of the same
#: attribute. Two kinds: a layer's own object that happens to share a
#: private name with a core class, and a third-party object an AST walk
#: cannot type.
_EXEMPT: dict[tuple[str, str, str], str] = {
    ("nontainer.adapters.mcp", "server", "_resource_manager"): "FastMCP internals",
    ("nontainer.adapters.mcp", "server._resource_manager", "_templates"): (
        "FastMCP internals"
    ),
}


def _class_private_names(paths: list[pathlib.Path]) -> set[str]:
    """The private attribute names the classes in ``paths`` define:
    ``self._x`` / ``cls._x`` writes and reads, methods, and class-level
    assignments. For core this is the set a reach-in could name."""
    names: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id in ("self", "cls")
                ):
                    names.add(sub.attr)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.AnnAssign) and isinstance(
                    sub.target, ast.Name
                ):
                    names.add(sub.target.id)
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
    return {n for n in names if n.startswith("_") and not n.startswith("__")}


def _private_read(node: ast.AST) -> tuple[ast.expr, str] | None:
    """``(receiver, name)`` if this node reads a private attribute, else
    None. Dunders are the language's, not a package's."""
    if isinstance(node, ast.Attribute):
        receiver, attr = node.value, node.attr
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        receiver, attr = node.args[0], node.args[1].value
    else:
        return None
    if not attr.startswith("_") or attr.startswith("__"):
        return None
    return receiver, attr


def _reach_ins(
    sources: dict[str, str],
    core_private: set[str],
    exempt: dict[tuple[str, str, str], str],
) -> list[str]:
    """``module:line: receiver._attr`` for every read, in ``sources``
    (module name -> source text), of a private name core defines on a
    receiver other than ``self``/``cls``.

    The receiver is what decides, not the name. A layer defining
    ``self._store`` on a class of its own does not make ``ws._store``
    in that layer its own business: the names most likely to collide
    are exactly the ones core keeps its state under, so an exemption
    has to say which object it is about. Anything the walk cannot
    type -- a third-party object -- is exempted by receiver spelling in
    ``exempt`` rather than by name.
    """
    out: list[str] = []
    for module, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            read = _private_read(node)
            if read is None:
                continue
            receiver, attr = read
            if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
                continue
            if attr not in core_private:
                continue
            spelled = ast.unparse(receiver)
            if (module, spelled, attr) in exempt:
                continue
            out.append(f"{module}:{node.lineno}: {spelled}.{attr}")
    return sorted(out)


def test_a_reach_in_is_reported_whatever_the_layer_defines():
    """A layer that keeps a ``_store`` of its own still may not read a
    workspace's; the exemption is by receiver, so ``self._store`` is
    the layer's and ``ws._store`` is the reach-in."""
    source = (
        "class Own:\n"
        "    def __init__(self):\n"
        "        self._store = 1\n"
        "\n"
        "def f(ws):\n"
        "    return ws._store, getattr(ws, '_provider', None)\n"
    )
    found = _reach_ins({"nontainer.adapters.x": source}, {"_store", "_provider"}, {})
    assert found == [
        "nontainer.adapters.x:6: ws._provider",
        "nontainer.adapters.x:6: ws._store",
    ]
    exempt = {("nontainer.adapters.x", "ws", "_store"): "for the test"}
    assert _reach_ins({"nontainer.adapters.x": source}, {"_store"}, exempt) == []


def test_no_layer_reads_a_core_private_attribute():
    """Nothing above core reads a core object's underscore attribute.

    A private name is the package's to change; the moment a layer above
    reads one, core cannot move without breaking it, which is the
    coupling the layering rule exists to prevent. So every ``obj._x``
    outside core, where ``_x`` is a name a core class defines and
    ``obj`` is not ``self``/``cls``, is reported unless an entry in
    ``_EXEMPT`` names that receiver. ``getattr(obj, "_x")`` is the same
    reach-in spelled as a call, so it is read too.
    """
    core_paths = [path for module, path in _modules() if _is_core(module)]
    core_private = _class_private_names(core_paths)
    sources = {
        module: path.read_text() for module, path in _modules() if not _is_core(module)
    }
    offenders = _reach_ins(sources, core_private, _EXEMPT)
    assert not offenders, (
        "a layer above core reads a private attribute (use the public "
        "name, or add one):\n" + "\n".join(offenders)
    )
