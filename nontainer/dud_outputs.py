"""Guest-side flattening of rich ``ui`` values, for dud's ``outputs_hook``.

This used to live inside dud, as ``dud/guest/ui.py``. dud 0.4.0 moved it
out: the guest no longer knows what a plotly figure is, nor that anyone
calls their output dict ``ui``. It offers every top-level binding to a
hook the host names, and drops the names the hook returns. So the
vocabulary (``ui``) and the formats (``.plotly.json``, ``.table.json``)
are nontainer's again, which is where they belong — they are this
layer's convention, and dud having known them meant dud knew about the
layer above it.

**Why a guest-side copy exists at all.** Most ``ui`` values cross the
wire as ordinary codec values and
:mod:`nontainer.adapters.render` materializes them host-side, which
keeps one authority for the shape rules. Four types cannot cross: a
plotly ``Figure``, a pandas ``DataFrame``, a matplotlib figure and a
PIL image are live objects, not data. On the ``subprocess`` rung that
is merely inconvenient; on a VM rung there is no shared address space
at all. So those four are serialized *here*, where the libraries and
the objects live, writing the same files the host would — and they
ride home as ordinary workspace writes.

Everything else is deliberately left alone. The host renderer has tiers
this does not (cards, html, image magic bytes, the json floor), and
duplicating them would be two implementations of one contract drifting
apart on someone else's schedule.

**The failure this replaced is worth recording**, because nothing
caught it: with no hook configured, a ``ui`` dict holding one
DataFrame is wholly unrepresentable, so dud drops the *entire binding*
— including the plain strings beside it. The observable symptom is
``namespace["ui"]`` coming back ``None``, which reads like the agent
never set it.

Detection is duck-typed by module name plus method, so this imports no
third-party library and costs nothing in an image that has none of
them.
"""

from __future__ import annotations

import io
import json
import os
import re
from typing import Any

from .artifacts import MAX_ARTIFACT_BYTES, is_rich, too_large_note

#: Wire tag for "this value became a file at <workspace-relative path>".
#: Read by ``DudExecutor._map_result``, which is the only place that
#: knows how a guest path maps onto the host's workspace root.
_CLAIM = "__nt_artifact__"

#: Optional companion to a claim: why the value did not render as
#: intended. Rides the envelope so `ui_problems` reaches the agent on
#: this rung too -- without it the cap message existed only in-process.
_PROBLEM = "__nt_problem__"

#: numpy dtype kind -> the column type the shell themes on. Mirrors
#: `render._COLUMN_KINDS`.
_COLUMN_KINDS = {
    "i": "number",
    "u": "number",
    "f": "number",
    "b": "boolean",
    "M": "datetime",
}


def flatten(harvest: dict, workspace: str) -> set[str]:
    """dud's ``outputs_hook``: serialize rich ``ui`` values to files.

    Receives *every* top-level binding (a shallow copy dud owns) and
    returns the names it fully consumed. Touches only ``ui``, and only
    the values in it that cannot cross the wire — a bare top-level
    DataFrame is the agent's variable, not a declared output, and
    turning it into an artifact would be this hook inventing a
    convention nobody asked for.

    Rebinds ``ui`` to the remainder rather than mutating it in place:
    the dict handed over is the agent's own object, and editing it
    would make a hook that raised halfway leave visible damage.
    """
    ui = harvest.get("ui")
    if not isinstance(ui, dict):
        return set()

    remaining: dict[Any, Any] = {}
    consumed = False
    for raw_name, value in ui.items():
        name = _safe_name(raw_name)
        problem = None
        try:
            written, problem = _materialize(name, value, workspace)
        except Exception as exc:  # noqa: BLE001 - serializers raise anything
            # Handing the live object back would recreate exactly the
            # data loss this hook exists to prevent: it is the thing
            # dud cannot encode, so the whole `ui` binding becomes
            # unrepresentable and its plain siblings vanish with it.
            # Consume it and say why, as the oversized path does.
            problem = (
                f"ui artifact {raw_name!r} NOT rendered: "
                f"{type(value).__name__} failed to serialize "
                f"({type(exc).__name__}: {exc})."
            )
            written = _note(workspace, name, problem)
        if written is None:
            remaining[raw_name] = value
        else:
            # A CLAIM, not a removal. The name the agent chose is the
            # only place the artifact is identified, so dropping it lost
            # information the host had no way to recover. The executor
            # turns this into an `ArtifactPath` once it can resolve the
            # guest path against the host root; the relative form is
            # what keeps this module ignorant of a namespace it cannot
            # see. `ui` therefore stays fully representable and crosses
            # as data.
            claim = {_CLAIM: written}
            if problem is not None:
                # Carried home rather than left in the file: this text
                # is what the agent reads to self-correct, and the host
                # has no way to reconstruct it from a path.
                claim[_PROBLEM] = problem
            remaining[raw_name] = claim
            consumed = True

    if consumed:
        harvest["ui"] = remaining
    return set()


def _safe_name(raw_name: Any) -> str:
    """The filename for a ``ui`` key, sanitized exactly as the host
    renderer sanitizes it (``adapters/render.py``).

    Not cosmetic. A key like ``"sales chart"`` written verbatim
    produces a path with a space, and the artifacts note is parsed as
    ``([\\w.-]+) -> (/\\S+)`` — so the file would be written and
    consumed and then fail to display. A key containing ``/`` would
    additionally create a nested directory the adapter's flat ``ui``
    scan never looks in.

    And it has to be the *same* transform on both sides, or the same
    figure lands at two different paths depending on which rung
    produced it — which is the exact parity this module exists to keep.
    """
    return re.sub(r"[^\w.-]+", "-", str(raw_name)).strip("-.") or "artifact"


def _note(workspace: str, name: str, message: str) -> str | None:
    """Write a representable explanation in place of an artifact.

    Consuming the name and leaving a note beats returning the live
    object, which dud cannot encode and which therefore takes the whole
    ``ui`` binding down with it. Returns None only if even this fails,
    where there is nothing better left to do than let the value cross
    and be dropped.
    """
    try:
        # `.txt`, matching what the host renderer writes for the same
        # condition. A `.json` here meant one rung produced kind "json"
        # and the other "text" for an identical failure.
        return _put(workspace, f"ui/{name}.txt", message.encode())
    except OSError:
        return None


def _put(workspace: str, relpath: str, data: bytes) -> str:
    full = os.path.join(workspace, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return relpath


def _write(
    workspace: str, relpath: str, data: bytes, name: str, mod: str
) -> tuple[str | None, str | None]:
    """Write one artifact, or a note saying why it could not be.

    Over the cap the *note* is still written and the name still
    consumed, for the same reason a failed serializer is: returning
    None here would leave a live object in ``ui``, which makes the
    whole binding unrepresentable and silently takes its representable
    siblings down with it.
    """
    if len(data) > MAX_ARTIFACT_BYTES:
        # Same text the host renderer produces for the same condition,
        # from the same function -- two copies of this message drifted
        # once already.
        problem = too_large_note(name, len(data), mod)
        return _note(workspace, name, problem), problem
    return _put(workspace, relpath, data), None


def _materialize(
    name: str, value: Any, workspace: str
) -> tuple[str | None, str | None]:
    """One rich value -> one ``ui/`` file. None if it can cross as data.

    Gated on the shared predicate rather than on these branches alone,
    so the set of types this claims cannot drift from the set the
    in-process path replaces.
    """
    if not is_rich(value):
        return None, None
    mod = type(value).__module__ or ""

    if mod.startswith("plotly") and hasattr(value, "to_json"):
        return _write(
            workspace, f"ui/{name}.plotly.json", value.to_json().encode(), name, mod
        )

    if mod.startswith("pandas") and hasattr(value, "columns"):
        payload = json.loads(value.head(200).to_json(orient="split", date_format="iso"))
        payload["total"] = len(value)  # renderers say "showing N of total"
        kinds = _column_types(value)
        if kinds is not None:
            payload["columnTypes"] = kinds
        return _write(
            workspace, f"ui/{name}.table.json", json.dumps(payload).encode(), name, mod
        )

    if mod.startswith("matplotlib") and hasattr(value, "savefig"):
        buf = io.BytesIO()
        value.savefig(buf, format="png", bbox_inches="tight")
        return _write(workspace, f"ui/{name}.png", buf.getvalue(), name, mod)

    if mod.startswith("PIL") and hasattr(value, "save"):
        buf = io.BytesIO()
        value.save(buf, format="PNG")
        return _write(workspace, f"ui/{name}.png", buf.getvalue(), name, mod)

    return None, None  # representable: let it cross to the host renderer


def _column_types(frame: Any) -> list[str] | None:
    """Per-column kinds, or None if unreadable — metadata must never be
    the reason an artifact fails to render."""
    try:
        return [
            _COLUMN_KINDS.get(getattr(dt, "kind", ""), "string") for dt in frame.dtypes
        ]
    except Exception:  # noqa: BLE001 - pandas internals, best effort
        return None
