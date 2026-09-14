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
and ``from x import y``, relative and absolute. A lazy import inside a
function is still a dependency; it is only a deferred one.
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


def _imports(path: pathlib.Path) -> set[tuple[str, int]]:
    """Every ``nontainer.*`` module this file imports, with line numbers.

    Relative imports are resolved against the importing module's own
    package, so ``from ..executor import x`` in an adapter reads as
    ``nontainer.executor`` like the absolute spelling would.
    """
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[tuple[str, int]] = set()
    for node in ast.walk(ast.parse(path.read_text())):
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
            if target.startswith("nontainer"):
                found.add((target, node.lineno))
    return found


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


#: Private attributes read on objects that are not nontainer's, where
#: an AST walk cannot see the type and the layering rule has nothing to
#: say. ``module:attr`` -> why.
_FOREIGN = {
    "nontainer.adapters.mcp:_resource_manager": "FastMCP server internals",
    "nontainer.adapters.mcp:_templates": "FastMCP server internals",
}


def _layer_private_attrs(paths: list[pathlib.Path]) -> set[str]:
    """The private attribute names a layer defines on its own classes.

    A layer reading its own object's ``_x`` is intra-layer structure,
    not a reach-in, and one apps module handing another an ``AppRuntime``
    is the ordinary case. Collected across the whole layer because that
    is the scope the attribute belongs to.
    """
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
    return names


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


def test_no_layer_reads_a_core_private_attribute():
    """Nothing above core reads a core object's underscore attribute.

    A private name is the package's to change; the moment a layer above
    reads one, core cannot move without breaking it, which is the
    coupling the layering rule exists to prevent. So every ``obj._x``
    outside core is reported unless ``obj`` is ``self``/``cls``, the
    name is one the layer defines on its own classes, or it belongs to
    a third-party object this walk cannot type. ``getattr(obj, "_x")``
    is the same reach-in spelled as a call, so it is read too.
    """
    layers = {
        SESSIONS: [PKG / "sessions.py"],
        APPS: sorted((PKG / "apps").rglob("*.py")),
        ADAPTERS: sorted((PKG / "adapters").rglob("*.py")),
        "testing verbs": [
            PKG / f"{name.rsplit('.', 1)[1]}.py" for name in TESTING_VERBS
        ],
    }
    offenders: list[str] = []
    for paths in layers.values():
        own = _layer_private_attrs(paths)
        for path in paths:
            module = _module_name(path)
            for node in ast.walk(ast.parse(path.read_text())):
                read = _private_read(node)
                if read is None:
                    continue
                receiver, attr = read
                if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
                    continue
                if attr in own or f"{module}:{attr}" in _FOREIGN:
                    continue
                rel = path.resolve().relative_to(PKG.parent)
                offenders.append(f"{rel}:{node.lineno}: .{attr}")
    assert not offenders, (
        "a layer above core reads a private attribute (use the public "
        "name, or add one):\n" + "\n".join(sorted(offenders))
    )
