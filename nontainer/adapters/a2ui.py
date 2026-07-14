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

- Layer 2 (NOT in this file yet): the version-specific ENVELOPE
  (``turn_to_a2ui`` — surface/begin-rendering, component tree, data model,
  done). It lands separately once the target a2ui spec version is pinned
  against a real consumer; keeping it out means layer 1 does not wait on
  that decision, and every version-specific field name stays isolated in
  the one function that churns.

The component vocabulary here is deliberately neutral-but-a2ui-shaped:
catalog components (``Card``/``Row``/``Column``/``Text``/``Image``) plus a
single extension type ``Chart`` for plotly specs. A fragment is
``{"component": <tree>, "data_model": {ref_key: value}}``; components carry
``{"$ref": key}`` where a value lives in the data model (today: only the
plotly spec, which is bulky and de-facto-standard — the consumer brings the
renderer).
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
) -> dict:
    """Project one artifact into an a2ui fragment.

    Dispatches on ``artifact_kind(path)``. ``data`` is the artifact's raw
    bytes, or ``None`` when the caller could not read the file — every kind
    that needs the bytes then degrades to the Text+link fallback (``image``
    still works, being URL-only). Malformed JSON where JSON is expected
    degrades the same way; this function never raises.

    Returns ``{"component": <tree>, "data_model": {...}}``.
    """
    kind = artifact_kind(path)

    if kind == "image":
        return _fragment({"componentType": "Image", "url": file_url(path)})

    if data is not None:
        if kind == "cards":
            payload = _try_json(data)
            if isinstance(payload, dict):
                return _cards(payload)
        elif kind == "table":
            payload = _try_json(data)
            if isinstance(payload, dict):
                return _table(payload)
        elif kind == "plotly":
            spec = _try_json(data)
            if isinstance(spec, dict):
                return _chart(spec)
        elif kind == "json":
            # .json undersells itself: agents reach for write_json('/ui/x.json').
            # Content-sniff a plotly spec (studio's looksLikePlotly, full-parse
            # only since we hold the whole file); otherwise fall to the link.
            spec = _try_json(data)
            if _looks_like_plotly(spec):
                return _chart(spec)

    # html/text/json-non-plotly/binary, and every bytes-needing kind that
    # could not parse: a Text label + a link. Never ship raw HTML across a2ui.
    return _fragment(
        {"componentType": "Text", "text": f"artifact: {name}", "link": file_url(path)}
    )


def _fragment(component: dict, data_model: dict | None = None) -> dict:
    return {"component": component, "data_model": data_model or {}}


def _text(value: object, role: str) -> dict:
    return {"componentType": "Text", "text": str(value), "role": role}


def _cards(payload: dict) -> dict:
    """The ``.cards.json`` payload ``{"items": [{label, value, delta?,
    unit?}]}`` → a Row of Cards. Each Card carries a Text child per present
    field; values go inline in the Text (KPI values are small, so no
    data-model indirection needed)."""
    items = payload.get("items")
    cards = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        children = [_text(item.get("label", ""), "label")]
        if "value" in item:
            children.append(_text(item["value"], "value"))
        if "delta" in item:
            children.append(_text(item["delta"], "delta"))
        if "unit" in item:
            children.append(_text(item["unit"], "unit"))
        cards.append({"componentType": "Card", "children": children})
    return _fragment({"componentType": "Row", "children": cards})


_TABLE_ROW_CAP = 50


def _table(payload: dict) -> dict:
    """The ``.table.json`` split-orient payload ``{"columns": [...], "data":
    [[...]], "total": N}`` → a Column: a header Row of Text, then up to 50
    data Rows. When ``total`` exceeds the rendered count, a trailing Text
    notes ``showing N of M rows`` (the payload is already head-capped
    upstream, so a big table degrades gracefully)."""
    columns = payload.get("columns") or []
    rows = payload.get("data") or []
    total = payload.get("total")

    children = [
        {
            "componentType": "Row",
            "children": [_text(c, "header") for c in columns],
        }
    ]
    rendered = rows[:_TABLE_ROW_CAP] if isinstance(rows, list) else []
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
