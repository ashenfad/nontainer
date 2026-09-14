"""Host-side materialization of the agent's ``ui = {...}`` binding.

``ui = {"chart": fig}`` is the whole convention for "show this".
:mod:`nontainer.artifacts` holds its vocabulary — what an artifact
path is, which values are too live to cross as data, the size cap;
:mod:`nontainer.dud_outputs` is the guest-side half, which serializes
those four types inside a VM because they cannot leave it. This is the
host-side half: the full tier cascade (spec formats, cards, pixels,
html) that turns one value into one workspace file, plus the
diagnostics an agent reads when a value renders as nothing.

Its own module rather than folded into ``artifacts.py``: that module
is the small shared vocabulary both rungs import, and the renderers
here are neither small nor shared — the guest copy is deliberately
narrower.

Core rather than an adapter. ``Workspace.run_python`` calls this on
every rung, so a figure becomes a file the same way regardless of who
is rendering the observation. While it lived in an adapter, whether a
chart was materialized at all depended on which adapter happened to
run, and core had to import upward to reach it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactPath,
    looks_like_plotly,
    too_large_note,
)

if TYPE_CHECKING:
    from .workspace import Workspace


def ui_root(ws: Workspace) -> str:
    """Where `ui = {...}` artifacts land: ``<ws.root>/ui``."""
    base = "" if ws.root == "/" else ws.root
    return f"{base}/ui"


class _NotRenderable(ValueError):
    """A value the ``ui`` namespace has no component for — it carries
    the diagnostic the agent reads in place of a file.

    Raised rather than returned so the sniff order reads as one
    cascade: a tier that can render says so by writing, and the floor
    says why nothing could.
    """

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


class _ArtifactTooLarge(ValueError):
    """A materialized value blew the artifact cap — carries the size so
    the observation can say WHY (and how to shrink it) instead of
    silently degrading to a repr."""

    def __init__(self, size: int) -> None:
        super().__init__(f"artifact too large ({size} bytes)")
        self.size = size


_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpg",
    b"GIF8": "gif",
    b"RIFF": "webp",
}


def _ui_write(ws: Workspace, path: str, data: bytes) -> str:
    if len(data) > MAX_ARTIFACT_BYTES:
        raise _ArtifactTooLarge(len(data))
    ws.files.write(path, data)
    return path


# Coarse column kinds for a `.table.json` artifact, keyed by numpy /
# pandas dtype ``kind`` (extension dtypes implement it too, so Int64 and
# a tz-aware datetime classify like their numpy counterparts). Anything
# unlisted — object, category, string, timedelta — is "string".
#
# A consumer rendering a real grid has to pick alignment and sort order
# per column, and every cell crosses as a JSON scalar: an ISO timestamp
# is indistinguishable from a string that happens to look like one, and
# a numeric column sorts lexically unless someone says otherwise.
# pandas knows; carrying it beats making each renderer guess.
_COLUMN_KINDS = {
    "i": "number",
    "u": "number",
    "f": "number",
    "b": "boolean",
    "M": "datetime",
}


def _column_types(frame: Any) -> list[str] | None:
    """Per-column kinds for ``frame``, or None if they can't be read —
    metadata must never be the reason an artifact fails to render."""
    try:
        return [
            _COLUMN_KINDS.get(getattr(dt, "kind", ""), "string") for dt in frame.dtypes
        ]
    except Exception:
        return None


def _is_stat(i: object) -> bool:
    """A stat tile: any dict carrying label + value. Untagged is fine —
    it is the shape agents naturally emit — so a tagged callout that also
    happens to hold label/value is disambiguated by ``_is_callout`` first."""
    return isinstance(i, dict) and "label" in i and "value" in i


def _is_callout(i: object) -> bool:
    """A callout: a TAGGED dict (``type == "callout"``) with a title or
    body. The tag is required — an untagged {title, body} would collide
    with too many ordinary dicts to duck-type safely."""
    return (
        isinstance(i, dict)
        and i.get("type") == "callout"
        and ("title" in i or "body" in i)
    )


def _card_row_near_miss(name: object, value: object) -> str | None:
    """The dict-native version of a constructor error: a list where MOST
    items duck-type as cards but some don't misses the cards tier and
    renders nothing — say which item broke the row and why, in the
    problems channel the agent already reads (the 8MB cap's lesson: name
    the fix, not just the failure). None when the list isn't card-shaped
    enough to diagnose, which leaves the general rule to say why nothing
    rendered."""
    # A lone stat, unwrapped. Not adopted the way a lone callout is —
    # {label, value} is too ordinary a shape to claim — but silence is
    # what made the callout case a bug report, so say the fix.
    # `not _is_callout`: the two predicates overlap — a tagged callout
    # carrying label/value metadata satisfies both — and for those
    # `_materialize_one` DOES render a one-item row. Without this the
    # note would tell an agent to fix a value that already worked,
    # which is worse than the silence it replaced.
    if isinstance(value, dict) and _is_stat(value) and not _is_callout(value):
        return (
            f"{str(name)!r} looks like a single stat, but a card row is a "
            f"LIST — wrap it as [{{...}}] to render it as a card. On its "
            f"own it is a plain dict, which renders nothing."
        )
    if not isinstance(value, list) or not value:
        return None
    matched = sum(1 for i in value if _is_stat(i) or _is_callout(i))
    if matched == len(value) or matched * 2 < len(value):
        return None  # a real card row, or not plausibly one
    import reprobate

    bad = next(i for i in value if not (_is_stat(i) or _is_callout(i)))
    return (
        f"{str(name)!r} looks like a card row, but this item is neither a "
        f"stat (needs 'label' and 'value') nor a tagged callout (needs "
        f"'type': 'callout' plus a 'title' or 'body'): "
        f"{reprobate.render(bad, budget=200)}. Fix that item to render cards."
    )


def _not_an_artifact(name: object, value: object) -> str:
    """Why a value is data rather than an artifact, and what to assign
    instead.

    The set that renders is closed, and consumers have no component for
    anything outside it — they render nothing, or degrade to a link.
    Writing a file anyway and announcing it in the artifacts note tells
    the agent a rendering happened, and it goes on to cite the path in
    its prose. So no file is written and this is what the agent gets:
    the value's own shape, so it can tell WHICH assignment is meant,
    and the shapes that do render.
    """
    import reprobate

    if isinstance(value, (dict, list)):
        kind = f"plain {'list' if isinstance(value, list) else 'dict'}"
    else:
        kind = type(value).__name__
    return (
        f"{str(name)!r} is a {kind}, which is data and not a UI artifact "
        f"— nothing renders it, so no file was written: "
        f"{reprobate.render(value, budget=200)}. To show it, assign a "
        f"pandas DataFrame (a table), a plotly figure (a chart), a "
        f"matplotlib figure or an image (a picture), or a list of card "
        f"rows ([{{'label': ..., 'value': ...}}]). To keep it as data, "
        f"print it or write it to a file yourself."
    )


def _renderer_failed(name: object, value: object, error: BaseException) -> str:
    """A value in the supported set whose renderer raised.

    Named, with the error, because the agent has to know WHICH of its
    assignments failed and why — a figure whose serializer blew up is a
    fixable mistake, and the silence that used to stand in for it (a
    capped repr in a .txt slot, announced as an artifact) read as
    success.
    """
    return (
        f"{str(name)!r} could not be rendered: "
        f"{type(error).__name__}: {error}. The value is a "
        f"{type(value).__name__}; fix what it holds, or write the file "
        f"yourself and assign its path."
    )


def _normalize_card(i: dict) -> dict:
    """One duck-typed item -> its canonical card dict. Callouts checked
    first so a tagged callout never masquerades as a stat. Unknown keys
    are dropped; legacy stat shapes (delta -> sublabel, unit -> value)
    are folded so older agent output still renders."""
    if _is_callout(i):
        tone = i.get("tone")
        if tone not in ("info", "success", "warning"):
            tone = "info"  # never infer sentiment; unknown/absent -> info
        return {
            "type": "callout",
            "title": str(i["title"]) if "title" in i else "",
            "body": str(i["body"]) if "body" in i else "",
            "tone": tone,
        }
    item: dict = {"type": "stat", "label": str(i["label"]), "value": i["value"]}
    if "unit" in i:
        item["value"] = f"{i['value']}{i['unit']}"  # legacy: unit onto value
    if "sublabel" in i:
        item["sublabel"] = str(i["sublabel"])
    elif "delta" in i:
        item["sublabel"] = str(i["delta"])  # legacy: delta folds into sublabel
    return item


def _plotly_json(name: str, spec: dict) -> tuple[bytes, str | None]:
    """A plotly spec dict as JSON bytes, plus a note when the encoding
    had to approximate.

    A figure OBJECT is encoded by ``Figure.to_json()`` — plotly's own
    encoder, which knows what a NumPy array, a pandas index and a
    timestamp mean inside a spec. The same dict reached by
    ``fig.to_dict()`` holds those same values, so it is encoded the same
    way: one encoder for both spellings, or a chart would depend on
    which one the agent assigned.

    Without plotly installed there is no such encoder. A spec that is
    already plain JSON encodes identically anyway and costs nothing. One
    that is not gets ``default=str``, which keeps the artifact but turns
    an array into its printed form — so that case, and only that case,
    comes back with a note saying what was approximated and how to
    avoid it.
    """
    import json as _json

    try:
        from plotly.io.json import to_json_plotly
    except ImportError:
        pass
    else:
        return to_json_plotly(spec).encode(), None
    try:
        return _json.dumps(spec).encode(), None
    except (TypeError, ValueError):
        return (
            _json.dumps(spec, default=str).encode(),
            f"{name!r} is a plotly spec holding values only plotly can "
            "encode (a NumPy array, a timestamp), and plotly is not "
            "importable here, so they were written as text and may not "
            "plot. Assign the Figure itself, or convert those values to "
            "lists and ISO strings first.",
        )


def _materialize_one(
    ws: Workspace, name: str, value: object, notes: list[str] | None = None
) -> str:
    """One value -> one workspace file. The sniff order is a THEMING
    hierarchy, most-declarative first: spec formats let the shell
    render (and theme) the artifact itself; html gives it partial say;
    pixels give it none. See the adapter docs in docs/api.md.

    The set is closed and there is no floor under it: a value no tier
    claims raises :class:`_NotRenderable`, and the caller turns that
    into a problem note. A file holding a bare number or a loose string
    is not an artifact — no consumer has a component for one — and
    announcing it told the agent a rendering had happened, which it
    then cited in its prose. The one value outside the set that still
    lands is a string naming a workspace file the agent saved itself:
    that is a pointer to an artifact, not a value to render.
    """
    import json as _json

    mod = type(value).__module__ or ""

    # reference tier: a string naming an existing workspace file is a
    # POINTER, not content. Agents predictably save a file themselves
    # (plt.savefig(...)) and put its path in `ui` — honor the near-miss
    # instead of json-encoding the path string.
    if isinstance(value, str) and value.startswith("/"):
        try:
            if ws.files.fs.exists(value) and not ws.files.fs.isdir(value):
                return value
        except Exception:
            pass  # unreadable path: it names no artifact, so it is not one

    # spec tier: shell-rendered, shell-themed
    if mod.startswith("plotly") and hasattr(value, "to_json"):
        return _ui_write(
            ws, f"{ui_root(ws)}/{name}.plotly.json", value.to_json().encode()
        )
    # A figure serialized to a plain dict (fig.to_dict(), a spec built
    # by hand, one loaded from JSON) is the one dict shape with a real
    # component behind it. Named by its suffix rather than left for a
    # consumer to content-sniff out of a bare `.json`.
    if looks_like_plotly(value):
        data, note = _plotly_json(name, value)
        if note is not None and notes is not None:
            notes.append(note)
        return _ui_write(ws, f"{ui_root(ws)}/{name}.plotly.json", data)
    if mod.startswith("pandas") and hasattr(value, "columns"):
        total = len(value)
        payload = _json.loads(
            value.head(200).to_json(orient="split", date_format="iso")
        )
        payload["total"] = total  # renderers say "showing N of total"
        kinds = _column_types(value)
        if kinds is not None:
            payload["columnTypes"] = kinds
        return _ui_write(
            ws, f"{ui_root(ws)}/{name}.table.json", _json.dumps(payload).encode()
        )

    # cards tier: a list of stat / callout dicts is a dashboard row — a
    # declarative shape with no other plausible rendering, so duck-type it
    # (zero sandbox imports) rather than demand a marker. A stat is any dict
    # with label+value (tagged or not — the shape agents naturally produce);
    # a callout must be tagged (type "callout") with a title or body. If a
    # single element is neither, the whole list falls through and is
    # diagnosed rather than rendered. The renderer never infers sentiment from a value's sign —
    # direction lives in the sublabel's words, tone only on callouts.
    # A bare TAGGED callout is adopted as a one-item row. Same
    # forgiveness `materialize_ui` already applies to a bare list
    # assigned straight to `ui`, one level in: the item is perfect, only
    # the list wrapper is missing. Observed in the wild — a single
    # caveat callout rendered as raw JSON, and said nothing about why.
    #
    # Callouts only. `type: "callout"` is an explicit marker nobody
    # writes by accident, so there is nothing to guess; a bare
    # {label, value} stat is too ordinary a shape to claim, so that one
    # gets a note naming the wrapper it is missing rather than a guess
    # (see `_card_row_near_miss`).
    cards = (
        value if isinstance(value, list) else [value] if _is_callout(value) else None
    )
    if cards and all(_is_stat(i) or _is_callout(i) for i in cards):
        items = [_normalize_card(i) for i in cards[:24]]  # cap: a wall past
        # two dozen is noise. default=str: stat values are routinely numpy
        # scalars (df.sum()), which json.dumps rejects — degrade them to
        # strings, not the repr fallback.
        return _ui_write(
            ws,
            f"{ui_root(ws)}/{name}.cards.json",
            _json.dumps({"items": items}, default=str).encode(),
        )

    # pixel tier
    if mod.startswith("matplotlib") and hasattr(value, "savefig"):
        import io as _io

        buf = _io.BytesIO()
        value.savefig(buf, format="png", bbox_inches="tight")
        return _ui_write(ws, f"{ui_root(ws)}/{name}.png", buf.getvalue())
    if mod.startswith("PIL") and hasattr(value, "save"):
        import io as _io

        buf = _io.BytesIO()
        value.save(buf, format="PNG")
        return _ui_write(ws, f"{ui_root(ws)}/{name}.png", buf.getvalue())
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        for magic, ext in _IMAGE_MAGIC.items():
            if data.startswith(magic):
                return _ui_write(ws, f"{ui_root(ws)}/{name}.{ext}", data)
        return _ui_write(ws, f"{ui_root(ws)}/{name}.bin", data)

    # html tier: the scientific-python display ecosystem for free
    bundle_fn = getattr(value, "_repr_mimebundle_", None)
    if callable(bundle_fn):
        try:
            bundle = bundle_fn()
            if isinstance(bundle, tuple):
                bundle = bundle[0]
        except Exception:
            bundle = {}
        if isinstance(bundle, dict):
            if "text/html" in bundle:
                return _ui_write(
                    ws, f"{ui_root(ws)}/{name}.html", str(bundle["text/html"]).encode()
                )
            if "image/png" in bundle:
                import base64 as _b64

                raw = bundle["image/png"]
                data = _b64.b64decode(raw) if isinstance(raw, str) else raw
                return _ui_write(ws, f"{ui_root(ws)}/{name}.png", data)
    html_fn = getattr(value, "_repr_html_", None)
    if callable(html_fn):
        return _ui_write(ws, f"{ui_root(ws)}/{name}.html", str(html_fn()).encode())

    # No tier claimed it, so nothing renders it. There is no JSON floor:
    # a file holding a bare literal is not an artifact, and announcing
    # one told the agent its figure had arrived.
    raise _NotRenderable(_not_an_artifact(name, value))


def materialize_ui(
    ws: Workspace, ui: object, *, claims: dict | None = None
) -> tuple[list[tuple[str, str]], list[str]]:
    """Turn the agent's ``ui = {name: value}`` namespace binding into
    workspace artifacts under ``/ui/`` (committed writes). Returns
    ``(artifacts, problems)``: ``[(name, path)]`` for the observation
    note, plus diagnosis strings for values that could not be rendered
    as intended — the size cap, a value there is no component for, and
    a renderer that raised — which the adapter puts in the tool result
    so the agent can self-correct. Every one of those yields a problem
    and NO artifact: announcing a file nothing renders would tell the
    agent its figure arrived.

    ``claims``, when given, is filled with ``{original_key:
    ArtifactPath}`` so a caller can swap the rendered values out of the
    agent's own dict. An out-param rather than a second return value
    because the ``(artifacts, problems)`` pair is public and unpacked
    by name at call sites — and rather than re-deriving the mapping
    from the returned *sanitized* names, which two different keys can
    collide on."""
    import re as _re

    if not isinstance(ui, dict):
        # Envelope forgiveness: agents predictably assign the card LIST
        # straight to `ui` (observed twice, different models — the items
        # were perfect, only the dict wrapper was missing). A bare list
        # with exactly one plausible meaning is adopted under a default
        # name; any other non-dict still renders nothing.
        if (
            isinstance(ui, list)
            and ui
            and all(_is_stat(i) or _is_callout(i) for i in ui)
        ):
            ui = {"cards": ui}
        else:
            near_miss = _card_row_near_miss("ui", ui)
            return [], ([near_miss] if near_miss else [])
    out: list[tuple[str, str]] = []
    problems: list[str] = []
    for raw_name, value in list(ui.items())[:20]:
        near_miss = _card_row_near_miss(raw_name, value)
        if near_miss:
            problems.append(near_miss)
        name = _re.sub(r"[^\w.-]+", "-", str(raw_name)).strip("-.") or "artifact"
        try:
            path = _materialize_one(ws, name, value, problems)
        except _NotRenderable as e:
            # One diagnosis per value: where a near-miss already named
            # the item that broke the card row, the general rule adds
            # noise to an agent that has been told exactly what to fix.
            if not near_miss:
                problems.append(e.note)
            continue
        except _ArtifactTooLarge as e:
            # the one failure agents hit in practice — say WHY, in both
            # the artifact slot (human) and the problems note (agent)
            msg = too_large_note(str(raw_name), e.size, type(value).__module__ or "")
            problems.append(msg)
            try:
                path = _ui_write(ws, f"{ui_root(ws)}/{name}.txt", msg.encode())
            except Exception:
                continue
        except Exception as e:
            # A renderer in the supported set raised. The value was
            # meant to be an artifact, so the agent is told which one
            # failed and why — a capped repr announced as an artifact
            # said a figure had arrived when none had.
            problems.append(_renderer_failed(raw_name, value, e))
            continue
        out.append((name, path))
        if claims is not None:
            claims[raw_name] = ArtifactPath(path)
    return out, problems
