"""Skills: packaged instructions that live IN the workspace.

Convention (Claude-Code-compatible): ``/skills/<name>/SKILL.md`` with
YAML frontmatter (``name``, ``description``) plus any sibling
reference/script files. The pieces divide cleanly:

- DISCOVERY is a primer job — the agno adapter catalogs frontmatter
  into the toolkit instructions (:func:`catalog`).
- ACCESS needs no new tools — the terminal and ``open()`` read skill
  files, and skill scripts run through run_python/terminal, inside the
  sandbox like all agent code (no host-execution side channel).
- STORAGE is ordinary workspace files — skills version, fork, publish,
  and rewind with everything else, and agents can author or improve
  them like any other file.

Python libraries can EMBED skills: a package shipping
``<pkg>/skills/<name>/SKILL.md`` teaches every agent it's granted to
(:func:`install_from_modules`) — granting a module and installing its
usage guide become one gesture.
"""

from __future__ import annotations

import os
import posixpath
import re
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from .workspace import Workspace

SKILLS_ROOT = "/skills"


def frontmatter(content: bytes) -> dict[str, str]:
    """Top-level ``key: value`` pairs from a leading ``---`` block
    (minimal on purpose — no yaml dependency)."""
    text = content.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if line.startswith((" ", "\t")):
            continue  # nested yaml: not ours to parse
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip().lower()] = value.strip().strip("'\"")
    return fields


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _walk(source: Any) -> dict[str, bytes]:
    """Flatten a directory-ish source (Path or importlib Traversable)
    into {relative path: bytes}."""
    files: dict[str, bytes] = {}

    def walk(node: Any, prefix: str) -> None:
        for child in node.iterdir():
            rel = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_dir():
                walk(child, rel)
            else:
                files[rel] = child.read_bytes()

    walk(source, "")
    return files


def install(ws: "Workspace", source: Any) -> str:
    """Install a skill into the workspace at ``/skills/<name>/``.

    ``source``: raw bytes (a SKILL.md), a ``.md`` file, or a directory
    containing ``SKILL.md`` — as a ``Path`` or an importlib
    ``Traversable`` (so packages can ship skills:
    ``install(ws, files("mylib") / "skills" / "mylib")``).

    The name comes from SKILL.md frontmatter, falling back to the
    file/directory name. Re-installing overwrites (idempotent
    updates). Returns the installed skill name."""
    from pathlib import Path

    if isinstance(source, str):
        source = Path(source)

    if isinstance(source, bytes):
        files = {"SKILL.md": source}
        fallback = "skill"
    elif hasattr(source, "is_dir") and source.is_dir():
        files = _walk(source)
        if "SKILL.md" not in files:
            raise ValueError(f"directory skill needs SKILL.md at its root: {source}")
        fallback = getattr(source, "name", None) or "skill"
    elif hasattr(source, "read_bytes"):
        files = {"SKILL.md": source.read_bytes()}
        fname = getattr(source, "name", None) or "skill"
        if fname == "SKILL.md":
            parent = getattr(source, "parent", None)
            fallback = getattr(parent, "name", None) or "skill"
        else:
            fallback = os.path.splitext(fname)[0]
    else:
        raise TypeError(
            "install() expects bytes, a .md file, or a skill directory "
            f"(Path or Traversable) — got {type(source).__name__}"
        )

    name = _slug(frontmatter(files["SKILL.md"]).get("name") or fallback)
    with ws.lock:
        for rel, data in files.items():
            path = f"{SKILLS_ROOT}/{name}/{rel}"
            ws.fs.makedirs(posixpath.dirname(path), exist_ok=True)
            ws.fs.write(path, data)
        if ws.caps.versioned and ws.dirty:
            ws.checkpoint(info={"tool": "skill", "skill": name})
    return name


def discover(module: Any) -> list[Any]:
    """Embedded skills shipped by a module's top-level package:
    ``<pkg>/skills/<name>/SKILL.md`` directories, as Traversables."""
    from importlib.resources import files as pkg_files

    mod = getattr(module, "module", module)  # ModuleGrant passthrough
    name = mod if isinstance(mod, str) else getattr(mod, "__name__", "")
    top = name.split(".")[0]
    if not top:
        return []
    try:
        root = pkg_files(top) / "skills"
        if not root.is_dir():
            return []
        return [
            child
            for child in root.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        ]
    except Exception:
        return []  # namespace packages / zip apps / no resources: no skills


def install_from_modules(ws: "Workspace") -> list[str]:
    """The library-embedded-skill convention: every GRANTED module
    whose package ships ``<pkg>/skills/`` gets those skills installed —
    granting a library and teaching the agent to use it become one
    gesture. Call once at workspace setup; idempotent."""

    def entries(seq: Any) -> Iterator[Any]:
        for entry in seq:
            if isinstance(entry, (list, tuple)):
                yield from entries(entry)
            else:
                yield entry

    installed: list[str] = []
    seen: set[str] = set()
    for entry in entries(ws.python_config.modules or ()):
        mod = getattr(entry, "module", entry)
        name = mod if isinstance(mod, str) else getattr(mod, "__name__", "")
        top = name.split(".")[0]
        if not top or top in seen:
            continue
        seen.add(top)
        for skill_dir in discover(top):
            installed.append(install(ws, skill_dir))
    return installed


def catalog(ws: "Workspace") -> str:
    """The discovery primer: one line per installed skill, for the
    toolkit instructions. Empty string when there are no skills."""
    rows: list[str] = []
    try:
        with ws.lock:
            if not ws.fs.isdir(SKILLS_ROOT):
                return ""
            for name in sorted(ws.fs.list(SKILLS_ROOT)):
                path = f"{SKILLS_ROOT}/{name}/SKILL.md"
                if not ws.fs.exists(path):
                    continue
                desc = frontmatter(ws.fs.read(path)).get("description", "")
                rows.append(f"- {name}: {desc}" if desc else f"- {name}")
    except Exception:
        return ""  # a broken skills dir must never block agent setup
    if not rows:
        return ""
    return (
        "\n\nSkills — packaged guidance under /skills; when a task "
        "matches one, read its instructions first "
        "(cat /skills/<name>/SKILL.md):\n"
        + "\n".join(rows)
        + "\nSkills may be added mid-session: `ls /skills` to re-check."
    )
