"""What a rich ``ui`` value becomes once it is a file.

``ui = {name: value}`` is how agent code declares something to show.
Values that are *data* cross to the host as data. Values that are live
objects — a plotly ``Figure``, a pandas ``DataFrame``, a matplotlib
figure, a PIL image — cannot, so they are written to
``<root>/ui/<name>.<ext>`` and the binding is replaced with an
:class:`ArtifactPath` naming where it went.

That replacement happens in two different places for one honest
reason: on a VM rung the object cannot leave the guest at all, so it is
serialized there (see :mod:`nontainer.dud_outputs`); in-process it is
serialized host-side, where the object already lives. Either way the
consumer sees the same thing.

Before this existed the two rungs disagreed in a way nobody chose. The
in-process path left the live object in ``ui`` and materialized it only
if a particular adapter ran; the dud path wrote the file during
execution and then *deleted the key*, so the name the agent picked was
simply lost. Whether a chart became a file at all depended on your
executor and your adapter.
"""

from __future__ import annotations

#: Bytes past which an artifact is refused and replaced by a note. A
#: RENDERER limit, not a transport one: it is advertised to the agent
#: in the python primer ("Artifacts are capped at 8MB..."), and the
#: remediation it suggests is all about spec leanness. It also bounds
#: what a regenerated artifact commits into the version store on every
#: run.
MAX_ARTIFACT_BYTES = 8_000_000

#: (module prefix, method) pairs identifying values that cannot cross a
#: process or machine boundary as data. Duck-typed rather than
#: imported, so this costs nothing in an environment that has none of
#: them — and so the guest copy needs no third-party dependency.
_RICH = (
    ("plotly", "to_json"),
    ("pandas", "columns"),
    ("matplotlib", "savefig"),
    ("PIL", "save"),
)


def is_rich(value: object) -> bool:
    """Is this a live object that has to become a file to be seen?

    The one predicate both sides use. The guest hook serializes exactly
    these because it must — a VM has no shared address space — and the
    in-process path replaces exactly these so the two rungs agree. Any
    value NOT matching here crosses as ordinary data and stays itself
    in ``ui``; adapters still render it for display, but the binding
    the caller sees is untouched.
    """
    mod = type(value).__module__ or ""
    return any(mod.startswith(p) and hasattr(value, a) for p, a in _RICH)


class ArtifactTooLarge(ValueError):
    """A value over the artifact cap. ``size`` is its size in bytes, or,
    with ``at_least``, a lower bound on it: a value refused before it was
    fully serialized is known only to be at least that large."""

    def __init__(self, size: int, *, at_least: bool = False) -> None:
        super().__init__(f"artifact too large ({size} bytes)")
        self.size = size
        self.at_least = at_least


def capped_bytes(data: str | bytes) -> bytes:
    """``data`` as the bytes of an artifact, or :class:`ArtifactTooLarge`.

    A string is measured before it is encoded: a character is at least
    one byte in UTF-8, so a string longer than the cap is refused without
    building its bytes, and an ASCII one (what ``json.dumps`` and
    plotly's encoder produce) is exactly that long.
    """
    if isinstance(data, str):
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactTooLarge(len(data), at_least=not data.isascii())
        data = data.encode()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ArtifactTooLarge(len(data))
    return data


#: Trace properties that carry per-point arrays, at the top of a trace
#: and under its ``marker``. Only a lower bound is wanted, so a property
#: left out makes the bound lower, never wrong.
_PLOTLY_ARRAYS = (
    "x", "y", "z", "lat", "lon", "r", "theta", "a", "b", "c",
    "open", "high", "low", "close", "values", "labels", "ids", "parents",
    "text", "hovertext", "customdata", "locations",
    "i", "j", "k", "u", "v", "w", "intensity", "base", "width",
)  # fmt: skip
_PLOTLY_MARKER_ARRAYS = ("color", "size", "opacity", "symbol")


def _array_floor(value: object) -> int:
    """Fewest bytes an array-valued plotly property can serialize to.

    plotly writes an array either as a JSON list, where every element
    takes at least two bytes (a digit or quote pair, and a separator),
    or as base64 of its binary form. An integer array may be narrowed
    to a byte per element first, which base64 makes 4/3 of a byte, so
    an element of unknown type counts one byte. A float is never
    narrowed below four bytes, which base64 makes more than five, so a
    float element counts two.
    """
    size = getattr(value, "size", None)
    kind = getattr(getattr(value, "dtype", None), "kind", None)
    if isinstance(size, int) and kind is not None:  # a NumPy array
        return size * (2 if kind == "f" else 1)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 0


def plotly_size_floor(value: object) -> int:
    """A lower bound on the size of a plotly figure's JSON, read from its
    trace arrays without serializing anything.

    A ``Figure``'s traces are read through their attributes, which hand
    back the arrays the figure holds rather than copies; a spec dict's
    ``data`` entries are read as the dicts they are.
    """
    if isinstance(value, dict):
        traces = value.get("data") or ()
    else:
        traces = getattr(value, "data", ()) or ()
    floor = 0
    for trace in traces:
        floor += sum(_array_floor(_prop(trace, p)) for p in _PLOTLY_ARRAYS)
        marker = _prop(trace, "marker")
        if marker is not None:
            floor += sum(_array_floor(_prop(marker, p)) for p in _PLOTLY_MARKER_ARRAYS)
    return floor


def _prop(obj: object, name: str) -> object:
    """A plotly object's or spec dict's property, or ``None`` where it
    has none (a trace type without it raises rather than answering)."""
    try:
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    except Exception:
        return None


def check_plotly_floor(value: object) -> None:
    """Refuse a figure whose arrays alone are over the cap, before the
    encoder builds what would be refused anyway."""
    floor = plotly_size_floor(value)
    if floor > MAX_ARTIFACT_BYTES:
        raise ArtifactTooLarge(floor, at_least=True)


def too_large_note(name: str, size: int, mod: str, *, at_least: bool = False) -> str:
    """Actionable diagnosis for the cap: the agent reads this in the
    tool result and self-corrects; the human sees it where the figure
    would have been."""
    over = "at least " if at_least else ""
    note = (
        f"ui artifact {name!r} NOT rendered: too large "
        f"({over}{size / 1e6:.1f}MB > {MAX_ARTIFACT_BYTES / 1e6:.0f}MB cap)."
    )
    if mod.startswith("plotly"):
        return note + (
            " The usual culprit is per-point customdata/hover text — drop or"
            " aggregate it. Coordinates are cheap (binary-encoded); WebGL"
            " traces (scattergl, scattermapbox) render 100k+ points fine,"
            " but the spec must still fit the cap."
        )
    return note + " Downsample or aggregate before assigning to `ui`."


def renderer_failed_note(name: object, value: object, error: BaseException) -> str:
    """Actionable diagnosis for a value in the supported set whose
    renderer raised.

    Named, with the error, because the agent has to know WHICH of its
    assignments failed and why — a figure whose serializer blew up is a
    fixable mistake. It goes to the problems channel and no file is
    written: a capped repr sitting in the artifact slot, announced as
    an artifact, reads as the figure having arrived.

    One function, because both rungs hit this condition — in-process
    where the object lives, and guest-side where it cannot leave — and
    an agent must not have to know which one served it.
    """
    return (
        f"{str(name)!r} could not be rendered: "
        f"{type(error).__name__}: {error}. The value is a "
        f"{type(value).__name__}; fix what it holds, or write the file "
        f"yourself and assign its path."
    )


def looks_like_plotly(obj: object) -> bool:
    """Whether a decoded JSON value is a plotly figure spec: a dict with
    a ``data`` list and a ``layout`` dict.

    The same rule studio's ``looksLikePlotly`` applies to a bare
    ``.json`` artifact, kept in one place so what the harness writes and
    what a consumer sniffs cannot drift apart.
    """
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("data"), list)
        and isinstance(obj.get("layout"), dict)
    )


def artifact_kind(path: str) -> str:
    """Suffix -> render kind, the single source of truth mirroring
    studio's ``Artifact.svelte`` dispatch. The compound spec suffixes
    (``.plotly.json`` / ``.table.json`` / ``.cards.json``) MUST be
    tested before the bare ``.json`` floor — a plain ``.json`` still
    means ``"json"`` here, though consumers may content-sniff it as
    plotly, as studio does."""
    lower = path.lower()
    if lower.endswith(".plotly.json"):
        return "plotly"
    if lower.endswith(".table.json"):
        return "table"
    if lower.endswith(".cards.json"):
        return "cards"
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if lower.endswith(".html"):
        return "html"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".txt"):
        return "text"
    return "binary"


class ArtifactPath(str):
    """A workspace path that ``ui`` holds in place of a rendered value.

    A ``str`` subclass on purpose, so that knowing about this type is
    optional. An embedder who has never heard of it still gets a
    working absolute path: it compares equal to one, joins like one,
    and serializes as one. An embedder who cares asks
    ``isinstance(v, ArtifactPath)`` and gets an unambiguous answer —
    which a bare path string could not give, since an agent may put an
    ordinary string in ``ui`` too.

    ``kind`` is derived, never stored. The suffix already *is* the
    type, and a stored copy could disagree with it — a ``.png``
    labelled ``"table"`` would be expressible, and eventually written.
    One fact, one place.
    """

    __slots__ = ()

    @property
    def kind(self) -> str:
        """``"plotly"``, ``"table"``, ``"image"``, ... — see
        :func:`artifact_kind`."""
        return artifact_kind(self)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"ArtifactPath({str.__repr__(self)}, kind={self.kind!r})"
