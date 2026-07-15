"""a2ui projection: a reply (prose + artifacts) → declarative agent-UI.

a2ui (Agent-to-User Interface) is adopted as an EGRESS format at the edge,
never as the internal representation — see ``docs`` and the plan. This
adapter is split into two layers so the volatile part stays small:

- Layer 1 (this file's bulk): SEMANTICS — stable, renderer-agnostic, fully
  tested. ``splice`` is the canonical definition of how a reply interleaves
  with its artifacts; ``component_for`` maps one artifact to a neutral,
  a2ui-shaped component fragment. Both are pure functions: plain dicts and
  tuples in and out, no I/O, no new dependencies (callers pass the artifact
  bytes and a ``file_url`` resolver).

- Layer 2 (``turn_to_a2ui`` + ``BASIC_CATALOG``): the version-specific
  ENVELOPE, now PINNED to A2UI v0.9 (the stable production family; v1.0 is
  still a release candidate). It composes layer 1's nested fragments into
  the v0.9 wire sequence — one ``createSurface``, one ``updateComponents``
  carrying a FLAT adjacency list, then one ``updateDataModel`` per data-
  model entry. Every version-specific field name stays isolated here, so
  when the consumer moves to v1.0 only this half of the file churns.

  Two v0.9 quirks the envelope absorbs so layer 1 stays neutral: (1) the
  component list is flat — containers reference children by id, not by
  nesting — so ``turn_to_a2ui`` flattens each fragment into deterministic
  ids (root ``"root"``, segment roots ``"seg0"``, ``"seg1"``, ..., nested
  children ``"seg3-1"``, ``"seg3-1-value-1"``). (2) the basic-catalog
  ``Text`` has no ``role`` prop, so layer 1's Text ``role`` is folded into
  the component id (``"seg3-1-value-1"``) and dropped from the emitted
  component. Layer 1's ``{"$ref": key}`` markers become v0.9 JSON-Pointer
  bindings ``{"path": "/artifacts/{name}/{key}"}`` during flattening, the
  value delivered by ``updateDataModel``. Basic-catalog ``Text`` renders
  Markdown, so ``("md", text)`` segments ship verbatim as Text components.

The component vocabulary here is deliberately neutral-but-a2ui-shaped:
basic-catalog components (``Card``/``Row``/``Column``/``Text``/``Image``)
plus the nontainer catalog's extension types (``docs/a2ui/catalog.json``,
``NONTAINER_CATALOG``): ``Chart`` for plotly specs (always emitted — a
figure has no basic approximation), and ``Stat``/``Callout`` for card items
(emitted only when the surface's catalog is ``NONTAINER_CATALOG``; under
any other catalog, cards degrade to a schema-valid Card/Column/Text
approximation). A fragment is ``{"component": <tree>, "data_model":
{ref_key: value}}``; components carry ``{"$ref": key}`` where a value lives
in the data model (today: only the plotly spec, which is bulky and
de-facto-standard — the consumer brings the renderer).
"""

from __future__ import annotations

import json
import re
from typing import Callable, Union

from .render import artifact_kind

# ("md", text) | ("artifact", name, path). Matches the two shapes studio's
# AgentMessage.svelte emits from its own splice.
Segment = Union[tuple[str, str], tuple[str, str, str]]

# The SAME regex studio's AgentMessage.svelte uses to split prose on image
# refs. The two implementations are kept deliberately in sync: this Python
# function is the canonical spec (and the a2ui converter's input); the Svelte
# copy renders it client-side. Change one, change the other.
_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\((/[^)\s]+)\)")


def splice(prose: str, artifacts: list[tuple[str, str]]) -> list[Segment]:
    """Canonical interleaving of a reply with its artifacts.

    Prose is split at markdown image refs ``![alt](/path)``. A ref splices
    an ``("artifact", name, path)`` segment at its position; runs of text
    between refs become ``("md", text)`` segments (never empty ones). The
    artifact LIST is authoritative for names — a ref whose path is in the
    list uses the note's name, not the prose alt-text (they may differ).
    A ref whose path is NOT in the list still splices (using its alt-text
    as name), so workspace images an agent embeds by hand render too,
    mirroring studio.

    The Jupyter rule: artifacts from the list whose path was never
    referenced in prose append as trailing ``("artifact", ...)`` segments
    in list order. Each artifact appends at most once even if the list
    names it twice; a path referenced N times in prose splices at each
    reference and is not also appended.
    """
    # First path wins the name if the note lists a path twice.
    name_by_path: dict[str, str] = {}
    for name, path in artifacts:
        name_by_path.setdefault(path, name)

    out: list[Segment] = []
    referenced: set[str] = set()
    last = 0
    for m in _IMAGE_REF.finditer(prose):
        if m.start() > last:
            out.append(("md", prose[last : m.start()]))
        alt, path = m.group(1), m.group(2)
        out.append(("artifact", name_by_path.get(path, alt), path))
        referenced.add(path)
        last = m.end()
    if last < len(prose):
        out.append(("md", prose[last:]))

    appended: set[str] = set()
    for name, path in artifacts:
        if path in referenced or path in appended:
            continue
        out.append(("artifact", name, path))
        appended.add(path)
    return out


# ---------------------------------------------------------------------------
# component_for: one artifact -> a renderer-agnostic fragment
# ---------------------------------------------------------------------------


def component_for(
    name: str,
    path: str,
    data: bytes | None,
    file_url: Callable[[str], str],
    *,
    extension_cards: bool = False,
) -> dict:
    """Project one artifact into an a2ui fragment.

    Dispatches on ``artifact_kind(path)``. ``data`` is the artifact's raw
    bytes, or ``None`` when the caller could not read the file — every kind
    that needs the bytes then degrades to the Text+link fallback (``image``
    still works, being URL-only). Malformed JSON where JSON is expected
    degrades the same way; this function never raises.

    ``extension_cards=True`` emits card items as the nontainer catalog's
    ``Stat``/``Callout`` extension components (one node per item, flat
    props) instead of the basic-catalog Card/Column/Text approximation.
    Callers set it when the surface's catalog declares those components
    (``turn_to_a2ui`` does, for ``NONTAINER_CATALOG``). ``Chart`` has no
    such switch: a plotly figure has no basic-catalog approximation worth
    shipping, so it is always the extension component and lenient basic
    consumers simply skip it.

    Returns ``{"component": <tree>, "data_model": {...}}``.
    """
    kind = artifact_kind(path)

    if kind == "image":
        return _fragment({"componentType": "Image", "url": file_url(path)})

    # The builders parse agent-writable files (near-miss adoption promotes
    # DIRECT /ui writes into the note), so a malformed payload here is a
    # reachable input, not a programming error. The except makes the
    # never-raises contract structurally true: any builder surprise lands
    # in the fallback instead of breaking an egress stream mid-turn.
    if data is not None:
        try:
            if kind == "cards":
                payload = _try_json(data)
                if isinstance(payload, dict):
                    return _cards(payload, extension_cards)
            elif kind == "table":
                payload = _try_json(data)
                if isinstance(payload, dict):
                    return _table(payload)
            elif kind == "plotly":
                spec = _try_json(data)
                if isinstance(spec, dict):
                    return _chart(spec)
            elif kind == "json":
                # .json undersells itself: agents reach for
                # write_json('/ui/x.json'). Content-sniff a plotly spec
                # (studio's looksLikePlotly, full-parse only since we hold
                # the whole file); otherwise fall to the link.
                spec = _try_json(data)
                if _looks_like_plotly(spec):
                    return _chart(spec)
        except Exception:
            pass

    # html/text/json-non-plotly/binary, and every bytes-needing kind that
    # could not parse: a Text label + a link. Never ship raw HTML across a2ui.
    return _fragment(
        {"componentType": "Text", "text": f"artifact: {name}", "link": file_url(path)}
    )


def _fragment(component: dict, data_model: dict | None = None) -> dict:
    return {"component": component, "data_model": data_model or {}}


def _text(value: object, role: str) -> dict:
    return {"componentType": "Text", "text": str(value), "role": role}


def _cards(payload: dict, extension: bool = False) -> dict:
    """The ``.cards.json`` payload ``{"items": [...]}`` → a Row of card
    components, dispatched per item ``type`` (mirrors studio's CardRow).

    ``extension=True`` (the nontainer catalog): one flat component per
    item — ``Stat {label, value, sublabel?}`` / ``Callout {title?, body?,
    tone}`` — the wire says what it means.

    ``extension=False`` (basic-catalog approximation): the v0.9 ``Card``
    takes a SINGULAR required ``child`` id (issue #16), so each item is a
    Card whose ``child`` is a Column of Texts roled label / value /
    sublabel (stat) or title / body (callout, each only when non-empty).
    The callout's ``tone`` rides the Card as a passthrough prop — a
    DOCUMENTED DEVIATION: the basic Card schema is closed
    (``unevaluatedProperties: false``), so a strictly validating consumer
    rejects it, but lenient renderers (the reference implementation) drop
    or forward it, and it is the only sentiment channel this shape has.
    Consumers that validate strictly should use ``NONTAINER_CATALOG``.

    Values go inline — card text is small, so no data-model indirection.
    Items with no recognizable type render nothing."""
    items = payload.get("items")
    cards = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "stat":
            # an explicit null reads as "absent", never as the text "None"
            # (direct /ui writes bypass materialize's normalization)
            label = item.get("label")
            label = "" if label is None else str(label)
            value = item.get("value")
            value = "" if value is None else str(value)
            sublabel = item.get("sublabel")
            if extension:
                stat = {
                    "componentType": "Stat",
                    "label": label,
                    "value": value,
                }
                if sublabel is not None:
                    stat["sublabel"] = str(sublabel)
                cards.append(stat)
                continue
            children = [
                _text(label, "label"),
                _text(value, "value"),
            ]
            if sublabel is not None:
                children.append(_text(sublabel, "sublabel"))
            cards.append(
                {
                    "componentType": "Card",
                    "child": {"componentType": "Column", "children": children},
                }
            )
        elif kind == "callout":
            # clamp: direct /ui writes bypass materialize's tone clamp,
            # and the catalog declares tone as a closed enum
            tone = item.get("tone")
            tone = tone if tone in ("info", "success", "warning") else "info"
            if extension:
                callout = {
                    "componentType": "Callout",
                    "tone": tone,
                }
                if item.get("title"):
                    callout["title"] = str(item["title"])
                if item.get("body"):
                    callout["body"] = str(item["body"])
                cards.append(callout)
                continue
            children = []
            if item.get("title"):
                children.append(_text(item["title"], "title"))
            if item.get("body"):
                children.append(_text(item["body"], "body"))
            cards.append(
                {
                    "componentType": "Card",
                    "tone": tone,
                    "child": {"componentType": "Column", "children": children},
                }
            )
        # unrecognized type: skip, never raise
    return _fragment({"componentType": "Row", "children": cards})


_TABLE_ROW_CAP = 50


def _table(payload: dict) -> dict:
    """The ``.table.json`` split-orient payload ``{"columns": [...], "data":
    [[...]], "total": N}`` → a Column: a header Row of Text, then up to 50
    data Rows. When ``total`` exceeds the rendered count, a trailing Text
    notes ``showing N of M rows`` (the payload is already head-capped
    upstream, so a big table degrades gracefully)."""
    # both guarded the same way: these fields come from agent-writable
    # files, and a truthy non-list ({"columns": 5}) must degrade, not raise
    columns = payload.get("columns")
    columns = columns if isinstance(columns, list) else []
    rows = payload.get("data")
    rows = rows if isinstance(rows, list) else []
    total = payload.get("total")

    children = [
        {
            "componentType": "Row",
            "children": [_text(c, "header") for c in columns],
        }
    ]
    rendered = rows[:_TABLE_ROW_CAP]
    for row in rendered:
        cells = row if isinstance(row, list) else [row]
        children.append(
            {
                "componentType": "Row",
                "children": [_text(cell, "cell") for cell in cells],
            }
        )
    if isinstance(total, int) and total > len(rendered):
        children.append(_text(f"showing {len(rendered)} of {total} rows", "caption"))
    return _fragment({"componentType": "Column", "children": children})


def _chart(spec: dict) -> dict:
    """Plotly spec → the one extension component. The spec is bulky and the
    de-facto standard, so it rides in the data model and the component
    references it — the consumer brings a plotly renderer."""
    return _fragment(
        {"componentType": "Chart", "spec": {"$ref": "spec"}},
        {"spec": spec},
    )


def _try_json(data: bytes) -> object | None:
    try:
        return json.loads(data)
    except (ValueError, TypeError):
        return None


def _looks_like_plotly(obj: object) -> bool:
    """Port of studio's ``looksLikePlotly`` (frontend/src/lib/sniff.js),
    full-parse branch only: a top-level dict with a ``data`` list and a
    ``layout`` dict."""
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("data"), list)
        and isinstance(obj.get("layout"), dict)
    )


# ---------------------------------------------------------------------------
# Layer 2: the A2UI v0.9 envelope
# ---------------------------------------------------------------------------

# The v0.9 basic-catalog URL. createSurface must name a catalog; consumers
# with a Chart-capable custom catalog pass their own catalog_id instead.
BASIC_CATALOG = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

# The nontainer extension catalog (docs/a2ui/catalog.json in the repo):
# re-exports the basic components this adapter emits and adds ``Stat``,
# ``Callout``, and ``Chart``. Passing it as ``catalog_id`` opts the surface
# into extension card components; any OTHER custom id keeps the basic-shaped
# cards, since we can't know what a foreign catalog declares.
NONTAINER_CATALOG = (
    "https://raw.githubusercontent.com/ashenfad/nontainer/main/docs/a2ui/catalog.json"
)

_VERSION = "v0.9"


def turn_to_a2ui(
    prose: str,
    artifacts: list[tuple[str, str]],
    read_bytes: Callable[[str], bytes | None],
    file_url: Callable[[str], str],
    *,
    surface_id: str,
    catalog_id: str = BASIC_CATALOG,
) -> list[dict]:
    """Compose one reply (prose + artifacts) into an A2UI v0.9 message list.

    ``artifacts`` is the ``parse_artifacts_note`` output — ``(name, path)``
    pairs. ``read_bytes(path) -> bytes | None`` fetches an artifact's payload
    so the envelope owns no I/O policy (callers do reads and their own
    caching; ``None`` means unreadable, which degrades inside
    ``component_for``). ``file_url(path)`` resolves an artifact's public URL.

    ``catalog_id`` names the surface's catalog and doubles as the capability
    switch: ``NONTAINER_CATALOG`` opts card items into the ``Stat``/
    ``Callout`` extension components; any other id (including a consumer's
    own custom catalog) keeps the basic-catalog card approximation, since we
    can't know what a foreign catalog declares.

    Pipeline: ``splice`` interleaves prose and artifacts into ordered
    segments; each ``("artifact", ...)`` segment is projected by
    ``component_for``; every nested fragment is FLATTENED into a v0.9
    adjacency list with deterministic ids. ``("md", text)`` segments become
    Markdown ``Text`` components verbatim (basic-catalog Text renders
    Markdown, so no conversion).

    Emits, in order, each carrying ``"version": "v0.9"``:

    1. one ``createSurface`` (``surfaceId``, ``catalogId``);
    2. one ``updateComponents`` with the full flat component list, root
       ``Column`` (id ``"root"``) FIRST so a buffering consumer can start
       rendering before the tail arrives;
    3. one ``updateDataModel`` per data-model entry, keyed by JSON Pointer
       ``/artifacts/{name}/{key}`` — the same path the flattened components
       bind to (layer 1's ``{"$ref": key}`` markers are rewritten to
       ``{"path": ...}`` bindings during flattening).

    Ids are stable across identical inputs and unique: ``"root"``, segment
    roots ``"seg{i}"`` (i = splice index), nested children ``"{parent}-{n}"``
    or, for a Text carrying a layer-1 ``role``, ``"{parent}-{role}-{k}"``
    (the ``role`` prop is then dropped — v0.9 basic Text has no such prop).

    Empty prose with no artifacts still yields a VALID surface: a
    ``createSurface`` plus an ``updateComponents`` whose root ``Column`` has
    no children (no ``updateDataModel``). Never raises on any splice /
    ``component_for`` output; a ``read_bytes`` that itself raises is treated
    as an unreadable artifact.
    """
    extension_cards = catalog_id == NONTAINER_CATALOG
    components: list[dict] = [{"id": "root", "component": "Column", "children": []}]
    root_children: list[str] = components[0]["children"]
    data_entries: list[tuple[str, object]] = []

    for i, seg in enumerate(splice(prose, artifacts)):
        seg_id = f"seg{i}"
        root_children.append(seg_id)
        if seg[0] == "md":
            components.append({"id": seg_id, "component": "Text", "text": seg[1]})
            continue
        _, name, path = seg
        try:
            data = read_bytes(path)
        except Exception:
            # read_bytes is the caller's I/O; the envelope stays total.
            data = None
        frag = component_for(
            name, path, data, file_url, extension_cards=extension_cards
        )
        _flatten(frag.get("component") or {}, seg_id, name, components)
        for key, value in (frag.get("data_model") or {}).items():
            data_entries.append((f"/artifacts/{name}/{key}", value))

    messages: list[dict] = [
        {
            "version": _VERSION,
            "createSurface": {"surfaceId": surface_id, "catalogId": catalog_id},
        },
        {
            "version": _VERSION,
            "updateComponents": {"surfaceId": surface_id, "components": components},
        },
    ]
    for path, value in data_entries:
        messages.append(
            {
                "version": _VERSION,
                "updateDataModel": {
                    "surfaceId": surface_id,
                    "path": path,
                    "value": value,
                },
            }
        )
    return messages


def _flatten(node: dict, node_id: str, artifact: str, out: list[dict]) -> None:
    """Append ``node`` and its subtree to ``out`` as flat v0.9 components.

    Pre-order (parent before children) so the list stays buffering-friendly.
    ``componentType`` becomes ``component``; ``children`` become a list of
    generated child ids; a dict-valued ``child`` (the v0.9 Card's SINGULAR
    slot — issue #16) becomes one generated ``"{node_id}-body"`` id; ``role``
    is dropped (folded into the child id by ``_child_id``); a ``{"$ref":
    key}`` prop value is rewritten to a ``/artifacts/{artifact}/{key}``
    JSON-Pointer binding.
    """
    flat: dict = {"id": node_id, "component": node.get("componentType")}
    for key, val in node.items():
        if key in ("componentType", "children", "child", "role"):
            continue
        flat[key] = _bind(val, artifact)

    children = node.get("children")
    child_pairs: list[tuple[dict, str]] = []
    if isinstance(children, list):
        seen: dict[str, int] = {}
        ids: list[str] = []
        for idx, child in enumerate(children, start=1):
            cid = _child_id(node_id, idx, child, seen)
            ids.append(cid)
            child_pairs.append((child, cid))
        flat["children"] = ids

    child = node.get("child")
    if isinstance(child, dict):
        # "-body" is a word, sibling positions are numbers, and roled ids
        # carry a trailing counter ("-body-1") — no collisions possible.
        cid = f"{node_id}-body"
        flat["child"] = cid
        child_pairs.append((child, cid))

    out.append(flat)
    for child, cid in child_pairs:
        _flatten(child, cid, artifact, out)


def _child_id(parent_id: str, index: int, child: object, seen: dict[str, int]) -> str:
    """A stable child id. A Text with a ``role`` folds the role in (v0.9 Text
    drops the prop), disambiguated by per-role occurrence so a header Row's
    several ``role: header`` cells stay unique; everything else uses its
    1-based sibling position. Roles are words, positions are numbers, so the
    two schemes never collide even inside a mixed container (a table Column
    holds numbered Rows plus a ``caption`` Text)."""
    role = child.get("role") if isinstance(child, dict) else None
    if role:
        seen[role] = seen.get(role, 0) + 1
        return f"{parent_id}-{role}-{seen[role]}"
    return f"{parent_id}-{index}"


def _bind(val: object, artifact: str) -> object:
    """Rewrite a layer-1 ``{"$ref": key}`` marker into a v0.9 JSON-Pointer
    binding; pass everything else through untouched."""
    if isinstance(val, dict) and set(val) == {"$ref"}:
        return {"path": f"/artifacts/{artifact}/{val['$ref']}"}
    return val
