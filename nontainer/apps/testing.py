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

Substituting one is optional: ``call("scores")`` runs the handler
against what the session binds, which is the point of running it at
all. A test that must not write into the session's database, or must
not depend on its state, passes a fake instead — as a keyword rather
than by patching, because there is nothing to patch it on: a handler's
``db`` and ``cache`` are bound into its namespace by dispatch, and
``from host import db`` binds them into a module the composition
rewrites rather than executes. Everything that IS a module attribute
patches normally — ``patch.object`` on a workspace module, a class or
an instance reaches what the module itself reads.

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

from ..executor import HOST_MODULE
from .contract import HttpError, make_request, normalize


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
    same ``make_request`` and liberal-return rules (``normalize``) the
    real dispatch applies rather than by a second implementation in
    generated source.
    """

    @staticmethod
    def dispatch(
        handlers: Any,
        session: Any,
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
                f"{', '.join(absent)}, and nothing in this session binds that "
                "name — a request would fail on it too. Pass a fake, or fix "
                "the name in the handler."
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
        # What the handler's own `host` module shows: the session's
        # names with the test's substitutions over them, handed in
        # under a name no module can bind.
        resolved = {**(session or {}), **objects}
        verbs = maker(**objects, **{OBJECTS: resolved})
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
            wire = normalize(fn(request), request.headers.get("accept"))
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


class Host:
    """The ``host`` module a composed handler imports from.

    A handler reaches its dependencies by importing them, and a test's
    fake has to arrive under that spelling too — so the composition
    rewrites ``import host`` to build one of these from the names the
    test program holds, which are the substitutions where a test made
    one and the session's own objects everywhere else.

    Read-only after construction, in the same words the real module
    refuses in: the module a request hands a handler is rebuilt per
    execution and rejects writes, so a handler that assigns to
    ``host.db`` has to fail here too, or a test would pass on code a
    request refuses.
    """

    def __init__(self, **names: Any) -> None:
        self.__dict__.update(names)

    def _refuse(self, attr: str) -> AttributeError:
        return AttributeError(
            f"Cannot set attribute '{attr}' on module '{HOST_MODULE}': "
            "modules provided for this execution are read-only"
        )

    def __setattr__(self, attr: str, value: Any) -> None:
        raise self._refuse(attr)

    def __delattr__(self, attr: str) -> None:
        raise self._refuse(attr)


class Module:
    """A handler module as the test program holds it.

    The module's body runs as a composed function, the way a handler
    ``call`` reaches does — so the contract and the session's objects
    are in scope before its first line, which is what a request gives
    it. Its names are that function's locals by then: readable here,
    and not writable, because an attribute set on this object would
    change nothing the module's own functions read.
    """

    def __init__(self, name: str, names: Any) -> None:
        # By reference: the package namespace holds the memo itself, so
        # a module imported later shows up under a package already
        # bound by an earlier import.
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_names", names)

    def __getattr__(self, attr: str) -> Any:
        names = object.__getattribute__(self, "_names")
        try:
            return names[attr]
        except KeyError:
            name = object.__getattribute__(self, "_name")
            raise AttributeError(f"module {name!r} has no attribute {attr!r}") from None

    def _refuse(self, attr: str) -> AttributeError:
        name = object.__getattribute__(self, "_name")
        return AttributeError(
            f"Cannot set attribute {attr!r} on module {name!r}: a handler "
            "module runs as a composed function in the test program, whose "
            "own names are what its functions read — an attribute set here "
            "would reach none of them. Substitute through call(), which "
            "binds what the handler reads, or patch a module the test "
            "imports the ordinary way."
        )

    def __setattr__(self, attr: str, value: Any) -> None:
        raise self._refuse(attr)

    def __delattr__(self, attr: str) -> None:
        raise self._refuse(attr)


#: The contract classes a test program needs in scope for ``call``.
CONTRACT = (Call, CallError, TestResponse, Host, Module)

#: Names a composed handler resolves from the test program's own
#: scope: the contract every handler may name, plus the helper itself.
#: They are never parameters — nothing about them is a test's to
#: substitute, and a handler that names ``Response`` is naming the
#: contract rather than an injection.
LEXICAL = frozenset(
    {
        "Request",
        "Response",
        "HttpError",
        "Call",
        "CallError",
        "TestResponse",
        "Host",
        "Module",
        "call",
    }
)

#: Bound in every composed test program; the generated helper reads
#: it. Prefixed so a test's own names never collide with it.
REGISTRY = "nt__handlers"

#: The helper as the preamble defines it, and what ``from host import
#: call`` is rewritten to. Prefixed for the same reason: a test may
#: bind ``call`` to whatever it likes without unbinding the helper the
#: import asked for.
CALL = "nt__call"

#: What a test imports it under.
CALL_NAME = "call"

#: Where a composed test program keeps the handler modules its test
#: file imported, and the package namespace that reads from it — so
#: `import app.api.summary` binds `app` and a second import of the same
#: module shares one execution, as imports do.
MODULES = "nt__modules"
PACKAGE = "nt__package"

#: The session's own objects by name, built once in the preamble, and
#: the keyword every composed handler takes them under. Prefixed
#: because a handler is free to bind ``db`` itself: the module a
#: request gives it still reads the session's, so the name the
#: composition builds that module from cannot be one the module owns.
SESSION = "nt__session"
OBJECTS = "nt__objects"


def uses_call(source: str) -> bool:
    """Whether a test file asks for ``call`` at all — by importing it
    from ``host``, or by naming it.

    What decides the preamble, rather than the handlers found in it: a
    file that calls a handler by a name nobody can resolve still needs
    ``call`` in scope, or the refusal it earns is a bare NameError.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == CALL_NAME:
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == HOST_MODULE
            and any(alias.name == CALL_NAME for alias in node.names)
        ):
            return True
    return False


def _call_bindings(tree: ast.AST) -> set[str]:
    """The names this file means the helper by: ``call``, plus whatever
    ``from host import call as ...`` renamed it to."""
    names = {CALL_NAME}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == HOST_MODULE
        ):
            names.update(
                alias.asname
                for alias in node.names
                if alias.name == CALL_NAME and alias.asname
            )
    return names


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
    helpers = _call_bindings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not isinstance(target, ast.Name) or target.id not in helpers:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value not in found:
                found.append(first.value)
    return tuple(found)


def bound_names(source: str) -> frozenset[str]:
    """The names a handler binds at module level — assigned, defined or
    imported. What a request would find in the module's own globals, so
    nothing in it is a test's to substitute."""
    import symtable

    table = symtable.symtable(source, "<handler>", "exec")
    return frozenset(
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported()
    )


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
    bound = bound_names(source)
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


def host_names(source: str) -> tuple[str, ...]:
    """The dependencies a handler reaches through the ``host`` module,
    in source order.

    ``from host import db`` and ``import host`` then ``host.db`` name
    the same object the bare ``db`` names, so a test substitutes them
    the same way. Python's scope analysis cannot see it — the import
    BINDS the name, and the read is an attribute rather than a name —
    which is why this asks the question separately and why the
    composition rewrites the import instead of leaving it to run.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    held = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == HOST_MODULE
    }
    hits: list[tuple[tuple[int, int], str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == HOST_MODULE
        ):
            hits.extend(
                ((node.lineno, node.col_offset), alias.name)
                for alias in node.names
                if alias.name != "*"
            )
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in held
        ):
            hits.append(((node.lineno, node.col_offset), node.attr))
    ordered: list[str] = []
    for _, name in sorted(hits):
        if name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def substitute_host(source: str) -> str:
    """The handler's source with its ``host`` imports rewritten to read
    the composed program's own names, line for line.

    A composed handler runs inside the test program, where a
    substitution is an ordinary name. Left alone, the import would
    reach past it to the session's real object and a test that faked
    the database would be testing the database. So ``from host import
    db`` becomes nothing — ``db`` is the wrapper's parameter by then —
    and ``import host`` becomes a ``Host`` built from the objects the
    call resolved, which arrive under a name of the composition's own:
    a module that binds ``db`` for itself still reads the session's
    through ``host.db``, as it does in a request.

    Only the import statement's own span is replaced, and a statement
    spanning several lines leaves the rest of them blank, so every line
    of the handler keeps the number a traceback names it by.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    built = f"Host(**{OBJECTS})"
    edits: list[tuple[ast.stmt, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == HOST_MODULE for alias in node.names
        ):
            others = [alias for alias in node.names if alias.name != HOST_MODULE]
            parts = []
            if others:
                parts.append("import " + _spelled(others))
            parts.extend(
                f"{alias.asname or alias.name} = {built}"
                for alias in node.names
                if alias.name == HOST_MODULE
            )
            edits.append((node, "; ".join(parts)))
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == HOST_MODULE
        ):
            # A name the contract owns is left as a real import, so it
            # fails here the way it fails a request: `call` is the test
            # program's, and a handler has no business importing it.
            kept = [alias for alias in node.names if alias.name in LEXICAL]
            parts = []
            if kept:
                parts.append(f"from {HOST_MODULE} import " + _spelled(kept))
            # An alias needs a line to land on; a plain name is already
            # the wrapper's parameter, so the import becomes a no-op.
            parts.extend(
                f"{alias.asname} = {alias.name}"
                for alias in node.names
                if alias not in kept and alias.asname and alias.asname != alias.name
            )
            edits.append((node, "; ".join(parts) or "pass"))
    return _splice(source, edits)


def substitute_call(source: str) -> str:
    """The test file's source with ``from host import call`` rewritten
    to the helper the preamble defines, line for line.

    ``call`` cannot be a real import: it closes over handlers composed
    into this one program, so there is nothing for a module to hand
    out. The import is how a test SAYS it wants the helper — the same
    sentence a handler writes for its own dependencies — and the
    composition is what makes the name mean something. Anything else
    the statement imports stays a real import of the host module.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    edits: list[tuple[ast.stmt, str]] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.ImportFrom)
            or node.level != 0
            or node.module != HOST_MODULE
            or not any(alias.name == CALL_NAME for alias in node.names)
        ):
            continue
        others = [alias for alias in node.names if alias.name != CALL_NAME]
        parts = []
        if others:
            parts.append(f"from {HOST_MODULE} import " + _spelled(others))
        parts.extend(
            f"{alias.asname or alias.name} = {CALL}"
            for alias in node.names
            if alias.name == CALL_NAME
        )
        edits.append((node, "; ".join(parts)))
    return _splice(source, edits)


def _spelled(aliases: Any) -> str:
    """Import targets as written: ``name`` or ``name as other``."""
    return ", ".join(
        alias.name + (f" as {alias.asname}" if alias.asname else "")
        for alias in aliases
    )


def _splice(source: str, edits: Any) -> str:
    """``source`` with each statement's own span replaced by its text.

    Only the span, and a statement spanning several lines leaves the
    rest of them blank: every line of the file keeps the number a
    traceback names it by, which is the whole reason the composition
    may rewrite anything at all.
    """
    if not edits:
        return source
    lines = source.splitlines()
    for node, text in sorted(
        edits, key=lambda edit: (edit[0].lineno, edit[0].col_offset), reverse=True
    ):
        first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        start, end = node.col_offset, node.end_col_offset or 0
        if first == last:
            lines[first] = lines[first][:start] + text + lines[first][end:]
            continue
        lines[first] = lines[first][:start] + text
        for middle in range(first + 1, last):
            lines[middle] = ""
        lines[last] = lines[last][end:]
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


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
            reads = (*free_names(source), *host_names(source))
            injectable = tuple(
                name for name in dict.fromkeys(reads) if name not in LEXICAL
            )
            verbs = verb_names(source)
            source = substitute_host(source)
        except SyntaxError as e:
            raise CallError(f"{rel} does not parse: {e.msg} (line {e.lineno})") from e
        # A name the session injects defaults to the session's own
        # object, so a test that substitutes nothing talks to the real
        # one. A name it does not inject has no default at all: such a
        # handler fails on that name in a request, and here the test is
        # told which fake it owes.
        required = tuple(name for name in injectable if name not in available)
        wrapper = f"nt__handler_{module}"
        signature = ", ".join(
            [name if name in required else f"{name}={name}" for name in injectable]
            + [f"{OBJECTS}=None"]
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
        f"{SESSION} = {{{', '.join(f'{name!r}: {name}' for name in sorted(available))}}}"
    )
    lines.append(
        f"def {CALL}(module, method='GET', path=None, *, params=None, body=None,"
        " json=None, headers=None, **objects):"
    )
    lines.append(
        f"    return Call.dispatch({REGISTRY}, {SESSION}, module, method, path,"
        " params, body, json, headers, objects)"
    )
    # The bare name too: `from host import call` is what a test should
    # write, and a test written before that spelling existed still runs.
    lines.append(f"{CALL_NAME} = {CALL}")
    return "\n".join(lines) + "\n", spans


def _is_handler(name: str) -> bool:
    """Whether a module name under the api directory is one dispatch
    would route to: a single segment, never an underscore-prefixed one.
    A library under api/ is not a handler — dispatch never runs one, so
    a test must not see one run differently either."""
    return bool(name) and "." not in name and not name.startswith("_")


def api_package(ws: Any, app_root: str) -> str:
    """The dotted name the api directory is imported under. Imports
    resolve from the workspace root, so it is the api tree's path
    relative to that root."""
    base = app_root if ws.root == "/" else app_root[len(ws.root) :]
    return f"{base.strip('/').replace('/', '.')}.api"


def imported_handlers(source: str, package: str) -> tuple[str, ...]:
    """The handler modules a test file imports, in source order.

    Every spelling names the same module — ``import app.api.summary``,
    ``... as summary``, ``from app.api.summary import get``, ``from
    app.api import summary`` — and one execution is what they share.
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


def compose_modules(
    ws: Any, source: str, app_root: str
) -> tuple[str, list[tuple[str, int, int]], str]:
    """The preamble for the handler modules a test file imports, the
    regions their lines occupy in it, and the test file rewritten to
    import them from it.

    A handler module read through the workspace's import loader runs on
    its own source and nothing else: ``HttpError`` is undefined there,
    the session's objects are absent, and a sad path dies of NameError
    instead of meaning a status. So the module's body is composed into
    the test program, where the contract and the session's names are
    already in scope — the same reason a handler ``call`` runs is
    composed rather than dispatched.

    The import statement is what runs it, and each one is rewritten in
    place: the module executes where the agent wrote the import, in
    the order the file reads, inside the function or the branch that
    holds it. The first executed import fills the memo and every later
    one shares that execution, as imports do. Nothing runs from the
    preamble itself, which only defines.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "", [], source
    package = api_package(ws, app_root)
    fs = ws.files.fs
    wanted = [
        name
        for name in imported_handlers(source, package)
        if fs.exists(f"{app_root}/api/{name}.py")
    ]
    if not wanted:
        return "", [], source

    segments = package.split(".")
    namespace = f"Module({package!r}, {MODULES})"
    for segment in reversed(segments[1:]):
        parent = ".".join(segments[: segments.index(segment)])
        namespace = f"Module({parent!r}, {{{segment!r}: {namespace}}})"
    lines = [f"{MODULES} = {{}}", f"{PACKAGE} = {namespace}"]
    spans: list[tuple[str, int, int]] = []
    for name in wanted:
        path = f"{app_root}/api/{name}.py"
        rel = path[len(ws.root) + 1 :] if ws.root != "/" else path.lstrip("/")
        handler = fs.read(path).decode("utf-8")
        try:
            _refuse_unindentable(handler, name)
        except SyntaxError as e:
            raise CallError(f"{rel} does not parse: {e.msg} (line {e.lineno})") from e
        lines.append(f"def nt__body_{name}():")
        body = handler.splitlines()
        spans.append((rel, len(lines) + 1, len(body)))
        for line in body:
            lines.append(f"    {line}" if line.strip() else "")
        # locals() is the module's namespace at the end of its body —
        # exactly what a module object exposes, including whatever a
        # conditional bound and whatever it did not.
        lines.append("    return locals()")
        lines.append(f"def nt__module_{name}():")
        lines.append(f"    nt__mod = {MODULES}.get({name!r})")
        lines.append("    if nt__mod is None:")
        lines.append(
            f"        nt__mod = Module({f'{package}.{name}'!r}, nt__body_{name}())"
        )
        lines.append(f"        {MODULES}[{name!r}] = nt__mod")
        lines.append("    return nt__mod")
    return (
        "\n".join(lines) + "\n",
        spans,
        _splice(source, _module_edits(tree, package, wanted, segments[0])),
    )


def _module_edits(
    tree: ast.AST, package: str, wanted: Any, top: str
) -> list[tuple[ast.stmt, str]]:
    """Each import of a composed handler module, rewritten to the call
    that runs it. Anything else the statement imports stays a real
    import of its own."""
    edits: list[tuple[ast.stmt, str]] = []
    for node in ast.walk(tree):
        parts: list[str] = []
        kept: list[Any] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = (
                    alias.name[len(package) + 1 :]
                    if alias.name.startswith(f"{package}.")
                    else ""
                )
                if name not in wanted:
                    kept.append(alias)
                elif alias.asname:
                    parts.append(f"{alias.asname} = nt__module_{name}()")
                else:
                    # `import app.api.summary` binds the top package and
                    # reaches the module through it, as Python does.
                    parts.append(f"nt__module_{name}(); {top} = {PACKAGE}")
            if parts and kept:
                parts.insert(0, "import " + _spelled(kept))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == package:
                for alias in node.names:
                    if alias.name in wanted:
                        held = alias.asname or alias.name
                        parts.append(f"{held} = nt__module_{alias.name}()")
                    else:
                        kept.append(alias)
                if parts and kept:
                    parts.insert(0, f"from {package} import " + _spelled(kept))
            elif node.module.startswith(f"{package}."):
                name = node.module[len(package) + 1 :]
                if name not in wanted:
                    continue
                if any(alias.name == "*" for alias in node.names):
                    raise CallError(
                        f"`from {node.module} import *` is not a spelling a "
                        "test can use: the handler module is composed into "
                        "the test program, so the names it defines are not "
                        "knowable until it runs. Import the module "
                        f"(`import {node.module} as {name}`) or name what "
                        "you need."
                    )
                parts.append(f"nt__mod = nt__module_{name}()")
                parts.extend(
                    f"{alias.asname or alias.name} = nt__mod.{alias.name}"
                    for alias in node.names
                )
        if parts:
            edits.append((node, "; ".join(parts)))
    return edits
