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


def too_large_note(name: str, size: int, mod: str) -> str:
    """Actionable diagnosis for the cap: the agent reads this in the
    tool result and self-corrects; the human sees it where the figure
    would have been."""
    note = (
        f"ui artifact {name!r} NOT rendered: too large "
        f"({size / 1e6:.1f}MB > {MAX_ARTIFACT_BYTES / 1e6:.0f}MB cap)."
    )
    if mod.startswith("plotly"):
        return note + (
            " The usual culprit is per-point customdata/hover text — drop or"
            " aggregate it. Coordinates are cheap (binary-encoded); WebGL"
            " traces (scattergl, scattermapbox) render 100k+ points fine,"
            " but the spec must still fit the cap."
        )
    return note + " Downsample or aggregate before assigning to `ui`."


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
