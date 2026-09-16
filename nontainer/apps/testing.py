"""``call``: the one piece of test support a handler needs.

A handler is the one unit ordinary Python cannot reach. Importing it
and calling its verb by hand fails — ``db`` and ``cache`` live in the
dispatch namespace, not in the module's globals — and it would skip the
envelope anyway: the liberal return, ``HttpError`` becoming a status,
the 400 a missing field earns.

So a test says::

    def test_limit_is_honoured():
        db = MagicMock()
        db.query.return_value = ["ann", "bob", "cy"]
        resp = call("scores", params={"limit": "2"}, db=db)
        assert resp.status == 200
        assert resp.json["scores"] == ["ANN", "BOB"]

``call`` is a bare name in the test's own namespace rather than
something it imports, for the same reason ``db`` is a bare name in a
handler: the handler has to run in the TEST's namespace. Re-entering
dispatch would give it a fresh view sandbox where the ``MagicMock`` the
test built does not exist — it would work under ``isolation="none"`` by
accident of shared memory and break under ``"process"``. So each
handler a test names is composed into a closure over the test program,
its line count preserved, and ``call`` invokes that.

Dependencies arrive as keyword arguments (``call(..., db=fake)``)
rather than by patching, because there is nothing to patch them on: a
handler's ``db`` and ``cache`` are bound into its namespace by
dispatch, not into the module's attributes. Everything that IS a module
attribute patches normally — ``patch.object`` on a workspace module,
a class or an instance reaches what the module itself reads.

One asymmetry, stated rather than papered over: ``call`` does not
reproduce the read-only filesystem a real GET runs under, because the
handler runs in the test's own sandbox. A GET that writes passes here
and 500s under ``ws-curl``. The wire tier is where structural REST is
enforced; this tier is where the logic is checked.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from .contract import HANDLER_CONTRACT, HttpError, make_request, normalize


class CallError(Exception):
    """A handler that cannot be composed into a test program. The
    message names the shape that works."""


@dataclass(frozen=True)
class TestResponse:
    """What ``call`` returns: the response, not the handler's raw
    return. Liberal returns are normalized and an ``HttpError`` is a
    status, because the envelope is what the helper exists to test."""

    status: int
    content: bytes = b""
    content_type: str = "text/plain"
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        """The body parsed as JSON, or ``None`` when it is not JSON."""
        import json as _json

        try:
            return _json.loads(self.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


class Call:
    """The host half of ``call``, reached from the test program as a
    contract class.

    Everything but the closure over the composed handlers lives here,
    in ordinary Python, so the envelope a test sees is built by the
    same ``make_request`` and ``normalize`` the real dispatch uses
    rather than by a second implementation in generated source.
    """

    @staticmethod
    def dispatch(
        handlers: Any,
        module: str,
        method: str = "GET",
        path: str | None = None,
        params: Any = None,
        body: Any = None,
        json: Any = None,
        headers: Any = None,
        objects: Any = None,
    ) -> TestResponse:
        import json as _json
        from urllib.parse import urlencode

        objects = dict(objects or {})
        entry = handlers.get(module) if hasattr(handlers, "get") else None
        if entry is None:
            raise CallError(
                f"call({module!r}) has no handler to run: name it as a plain "
                "string (the handler is composed into the test program before "
                f"the test runs, so the name cannot be computed), and check "
                f"that app/api/{module}.py exists"
            )
        maker, injectable, required = entry
        absent = [name for name in required if name not in objects]
        if absent:
            named = ", ".join(f"{name}=" for name in absent)
            raise CallError(
                f"call({module!r}) needs {named}: the handler reads "
                f"{', '.join(absent)} free, and nothing in this session binds "
                "that name — a request would raise NameError. Pass a fake, or "
                "fix the name in the handler."
            )
        unknown = [name for name in objects if name not in injectable]
        if unknown:
            named = ", ".join(sorted(unknown))
            reads = ", ".join(injectable) or "nothing outside itself"
            raise CallError(
                f"call({module!r}, {named}=...) substitutes what the handler "
                f"does not use: it reads {reads}. A fake nothing reads proves "
                "nothing, so this is an error rather than a pass."
            )

        verb = method.lower()
        verbs = maker(**objects)
        fn = verbs.get(verb)
        if fn is None:
            return Call._error(405, f"{method.upper()} not supported by {module}")

        url = path if path is not None else f"/api/{module}"
        if json is not None:
            body = _json.dumps(json).encode()
            headers = {**(headers or {}), "content-type": "application/json"}
        elif isinstance(body, str):
            body = body.encode()
        elif isinstance(body, (dict, list)):
            body = _json.dumps(body).encode()
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        request = make_request(
            method.upper(), url, body=body or b"", headers=dict(headers or {})
        )
        try:
            wire = normalize(fn(request))
        except HttpError as e:
            return Call._error(e.status, e.message)
        return TestResponse(wire.status, wire.content, wire.content_type, wire.headers)

    @staticmethod
    def _error(status: int, message: str) -> TestResponse:
        """An error body the way dispatch writes one: JSON, so a
        frontend's ``res.json()`` keeps working."""
        import json as _json

        body = _json.dumps({"error": message}).encode()
        return TestResponse(int(status), body, "application/json")


#: The contract classes a test program needs in scope for ``call``.
CONTRACT = (Call, CallError, TestResponse)

#: Names a composed handler resolves from the test program's own
#: scope: the contract every handler may name, plus the helper itself.
#: They are never parameters — nothing about them is a test's to
#: substitute, and a handler that names ``Response`` is naming the
#: contract rather than an injection.
LEXICAL = frozenset(
    {"Request", "Response", "HttpError", "Call", "CallError", "TestResponse", "call"}
)

#: Bound in every composed test program; the generated ``call`` reads
#: it. Prefixed so a test's own names never collide with it.
REGISTRY = "nt__handlers"


def uses_call(source: str) -> bool:
    """Whether a test file names ``call`` at all.

    What decides the preamble, rather than the handlers found in it: a
    file that calls a handler by a name nobody can resolve still needs
    ``call`` in scope, or the refusal it earns is a bare NameError.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == "call" for node in ast.walk(tree)
    )


def called_modules(source: str) -> tuple[str, ...]:
    """The handler names a test file passes to ``call``, in order.

    A literal is the only spelling that can work: the handler is
    composed before the test runs, so a name the test computes is not
    knowable in time. A computed name reaches ``call`` and is refused
    there, by name.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not isinstance(target, ast.Name) or target.id != "call":
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value not in found:
                found.append(first.value)
    return tuple(found)


def free_names(source: str) -> tuple[str, ...]:
    """The names a handler reads without binding them — what dispatch
    supplies from its namespace, and therefore what a test may
    substitute.

    Scope is the whole question, so Python's own scope analysis answers
    it: a name is free when some scope in the module reads it and
    resolves it to the module's globals, and the module itself never
    binds it. A parameter of one function says nothing about a global
    another function reads; a closure variable belongs to its enclosing
    function; a comprehension target belongs to its comprehension.

    Python's builtins are not part of it: a handler naming ``int`` is
    naming the language, not an injection. Everything left comes from
    outside the file, which is exactly what ``call`` has to stand in
    for.
    """
    import builtins
    import symtable

    table = symtable.symtable(source, "<handler>", "exec")
    bound = {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported()
    }
    reads: set[str] = set()

    def visit(scope: Any) -> None:
        for symbol in scope.get_symbols():
            if symbol.is_global() and symbol.is_referenced():
                reads.add(symbol.get_name())
        for child in scope.get_children():
            visit(child)

    visit(table)
    free = reads - bound - set(dir(builtins))
    # Source order, so a wrapper's parameters read the way the handler
    # does rather than in whatever order a set iterates.
    ordered: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in free and node.id not in ordered:
                ordered.append(node.id)
    return tuple(ordered)


def verb_names(source: str) -> tuple[str, ...]:
    """The handler's top-level functions — what the wrapper hands back
    for ``call`` to route to."""
    tree = ast.parse(source)
    return tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _refuse_unindentable(source: str, module: str) -> None:
    """A handler that cannot become a function body, refused by name
    rather than silently meaning something else once indented."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names = ", ".join(node.names)
            raise CallError(
                f"app/api/{module}.py declares `global {names}`, which a test "
                "cannot run: the handler is composed into a function, where "
                "`global` names the program's own scope. Pass the value in and "
                "return it out instead."
            )
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "*" for alias in node.names
        ):
            raise CallError(
                f"app/api/{module}.py uses `from ... import *`, which a test "
                "cannot run: the handler is composed into a function, and a "
                "star import is only legal at module level. Import the names "
                "it needs."
            )


def compose(
    ws: Any, modules: Any, app_root: str
) -> tuple[str, list[tuple[str, int, int]]]:
    """The preamble a test program carries: one closure per handler the
    tests name, plus ``call``.

    Returns the source and, per handler, ``(path, first composed line,
    line count)`` — so a frame inside a handler names the handler's own
    file and line.
    """
    available = set(ws.runtime.python_config.host_objects)
    if ws.runtime.cache_enabled:
        available.add("cache")
    fs = ws.files.fs
    lines: list[str] = []
    spans: list[tuple[str, int, int]] = []
    registry: list[str] = []
    for module in modules:
        path = f"{app_root}/api/{module}.py"
        rel = path[len(ws.root) + 1 :] if ws.root != "/" else path.lstrip("/")
        if not fs.exists(path):
            continue
        source = fs.read(path).decode("utf-8")
        try:
            _refuse_unindentable(source, module)
            injectable = tuple(n for n in free_names(source) if n not in LEXICAL)
            verbs = verb_names(source)
        except SyntaxError as e:
            raise CallError(f"{rel} does not parse: {e.msg} (line {e.lineno})") from e
        # A name the session injects defaults to the session's own
        # object, so a test that substitutes nothing talks to the real
        # one. A name it does not inject has no default at all: in a
        # request that handler raises NameError, and here the test is
        # told which fake it owes.
        required = tuple(name for name in injectable if name not in available)
        wrapper = f"nt__handler_{module}"
        signature = ", ".join(
            name if name in required else f"{name}={name}" for name in injectable
        )
        lines.append(f"def {wrapper}({signature}):")
        body = source.splitlines()
        spans.append((rel, len(lines) + 1, len(body)))
        for line in body:
            lines.append(f"    {line}" if line.strip() else "")
        verb_map = ", ".join(f'"{v}": {v}' for v in verbs)
        lines.append(f"    return {{{verb_map}}}")
        registry.append(f'"{module}": ({wrapper}, {injectable!r}, {required!r})')
    lines.append(f"{REGISTRY} = {{{', '.join(registry)}}}")
    lines.append(
        "def call(module, method='GET', path=None, *, params=None, body=None,"
        " json=None, headers=None, **objects):"
    )
    lines.append(
        f"    return Call.dispatch({REGISTRY}, module, method, path, params,"
        " body, json, headers, objects)"
    )
    return "\n".join(lines) + "\n", spans


#: What dispatch binds into a handler's globals, by name — the same
#: three a directly imported handler module has to be given.
_CONTRACT_NAMES = tuple(klass.__name__ for klass in HANDLER_CONTRACT)


def _is_handler(name: str) -> bool:
    """Whether a module name under the api directory is one dispatch
    would route to: a single segment, never an underscore-prefixed one.
    A library under api/ is not a handler and gets no contract from
    dispatch either."""
    return bool(name) and "." not in name and not name.startswith("_")


def imported_handlers(source: str, package: str) -> tuple[str, ...]:
    """The handler modules a test file imports directly, in order.

    Every spelling names the same module object — ``import
    app.api.summary``, ``... as summary``, ``from app.api.summary
    import get``, ``from app.api import summary`` — and a function
    imported out of a module still reads that module's globals, so the
    module is the only thing worth finding.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    found: list[str] = []

    def add(name: str) -> None:
        if _is_handler(name) and name not in found:
            found.append(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{package}."):
                    add(alias.name[len(package) + 1 :])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == package:
                for alias in node.names:
                    add(alias.name)
            elif node.module.startswith(f"{package}."):
                add(node.module[len(package) + 1 :])
    return tuple(found)


def seed_contract(ws: Any, source: str, app_root: str) -> str:
    """The preamble that hands a directly imported handler the globals
    a request hands it.

    ``import app.api.summary`` reaches the file through the workspace's
    import loader, which runs it on its own source and nothing else —
    so ``HttpError``, which dispatch binds into the handler's globals
    for the length of a request, is an undefined name there and every
    sad path dies of ``NameError`` instead of meaning a status. The
    preamble imports the module and binds the three names on it, which
    is the state a request would have found it in; the test's own
    import resolves to that same module object.

    Guarded, because a module that raises while it executes has to
    report at the test's own import line, not here: the loader drops a
    module whose body raised, so the test's import runs it again and
    raises where the agent can see it.
    """
    base = app_root if ws.root == "/" else app_root[len(ws.root) :]
    package = f"{base.strip('/').replace('/', '.')}.api"
    fs = ws.files.fs
    names = [
        name
        for name in imported_handlers(source, package)
        if fs.exists(f"{app_root}/api/{name}.py")
    ]
    lines: list[str] = []
    for index, name in enumerate(names):
        held = f"nt__seeded_{index}"
        lines.append("try:")
        lines.append(f"    import {package}.{name} as {held}")
        lines.extend(f"    {held}.{attr} = {attr}" for attr in _CONTRACT_NAMES)
        lines.append("except Exception:")
        lines.append("    pass")
    return "\n".join(lines) + "\n" if lines else ""
