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
- Skills that are never the agent's to change (an embedder's starter
  set, a library's) can be MOUNTED instead: :func:`mounts` makes a
  read-only ``Mount`` per skill at ``<root>/skills/<name>``. They sit
  beside installed ones, the catalog lists both, and being mounts they
  are not versioned, so ws-git never sees them and no commit carries
  them. An agent that wants to adapt one copies it under a new name.

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
    from .workspace import Mount, Workspace

SKILLS_DIR = "skills"


def skills_root(ws: "Workspace") -> str:
    """Where skills live in this workspace: ``<ws.root>/skills``."""
    base = "" if ws.root == "/" else ws.root
    return f"{base}/{SKILLS_DIR}"


def frontmatter(content: bytes) -> dict[str, str]:
    """Top-level ``key: value`` pairs from a leading ``---`` block
    (minimal on purpose — no yaml dependency)."""
    # utf-8-sig: a Windows-authored SKILL.md's BOM would otherwise
    # defeat the startswith("---") check and silently drop the fields
    text = content.decode("utf-8-sig", errors="replace")
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
            elif child.is_file():  # skips broken symlinks, fifos, sockets
                files[rel] = child.read_bytes()

    walk(source, "")
    return files


def install(ws: "Workspace", source: Any) -> str:
    """Install a skill into the workspace at ``<ws.root>/skills/<name>/``.

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
    root = skills_root(ws)
    if f"{root}/{name}" in _mounted(ws):
        from .errors import WorkspaceError

        raise WorkspaceError(
            f"skill {name!r} is mounted read-only at {root}/{name}; "
            "install this one under another name"
        )
    with ws.lock:
        written = []
        for rel, data in files.items():
            path = f"{root}/{name}/{rel}"
            ws.files.fs.makedirs(posixpath.dirname(path), exist_ok=True)
            ws.files.fs.write(path, data)
            written.append(path)
        # Durable now: installing a skill is the framework writing, at
        # a moment it chose. It takes everything uncommitted with it,
        # which costs the agent nothing — ws-git measures against the
        # agent's own last commit, so a composition in flight reads
        # exactly as it did before this commit.
        if ws.caps.versioned and not ws.frozen and ws.uncommitted:
            ws.commit(info={"tool": "skill", "skill": name})
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
    for entry in entries(ws.runtime.python_config.modules or ()):
        mod = getattr(entry, "module", entry)
        name = mod if isinstance(mod, str) else getattr(mod, "__name__", "")
        top = name.split(".")[0]
        if not top or top in seen:
            continue
        seen.add(top)
        mounted = _mounted(ws)
        for skill_dir in discover(top):
            if f"{skills_root(ws)}/{_skill_name(skill_dir)}" in mounted:
                continue  # the embedder mounted this one; nothing to copy
            installed.append(install(ws, skill_dir))
    return installed


def mounts(*sources: Any, root: str = "/workspace") -> "dict[str, Mount]":
    """Read-only mounts for skills that are never the agent's to change.

    Each source is a directory on disk: one skill (it holds a
    ``SKILL.md``) or a directory of them (``<name>/SKILL.md`` children),
    as a path or a package resource (``discover(module)`` returns those).
    Returns ``{"<root>/skills/<name>": Mount(dir, readonly=True)}``,
    named as :func:`install` would name them, for ``workspace(...,
    mounts=...)``::

        ws = store.open("chat", mounts=skills.mounts(STARTER_SKILLS))

    A mounted skill is a live, read-only view of its directory: it is
    not versioned, so ws-git never lists it and no commit, fork point or
    publication copies it, and a fork sees the same directory. The
    catalog lists mounted and installed skills alike, and
    :func:`install` refuses a name that is mounted.

    Raises ``ValueError`` for a source that is not a directory on disk
    (a package inside a zip has no directory to mount; ``install`` it),
    for one with no skill in it, and for two skills with one name.
    """
    from pathlib import Path

    from .workspace import Mount, normalize_root

    # normalized as the workspace normalizes its root, or the points
    # would not be where install() and the catalog look
    root = normalize_root(root)
    base = "" if root == "/" else root
    out: dict[str, Mount] = {}
    for source in sources:
        if not isinstance(source, (str, os.PathLike)):
            # a package resource with no directory behind it (a zip
            # import's zipfile.Path): nothing on disk to mount
            raise ValueError(
                f"skill source {source!s} is not a directory on disk; "
                "install() a skill that has no directory to mount"
            )
        path = Path(os.fspath(source))
        if not path.is_dir():
            raise ValueError(
                f"skill source {source!s} is not a directory on disk; "
                "install() a skill that has no directory to mount"
            )
        dirs = (
            [path]
            if (path / "SKILL.md").is_file()
            else sorted(
                c for c in path.iterdir() if c.is_dir() and (c / "SKILL.md").is_file()
            )
        )
        if not dirs:
            raise ValueError(f"no skills in {path} (a SKILL.md, or <name>/SKILL.md)")
        for skill_dir in dirs:
            point = f"{base}/{SKILLS_DIR}/{_skill_name(skill_dir)}"
            if point in out:
                raise ValueError(
                    f"two skills named {point.rsplit('/', 1)[1]!r}: {out[point].path} and {skill_dir}"
                )
            out[point] = Mount(str(skill_dir.resolve()), readonly=True)
    return out


def _skill_name(skill_dir: Any) -> str:
    """What a skill directory is called once installed or mounted: its
    frontmatter name, or the directory's own."""
    try:
        meta = frontmatter((skill_dir / "SKILL.md").read_bytes())
    except Exception:
        meta = {}
    return _slug(meta.get("name") or getattr(skill_dir, "name", "") or "skill")


def _mounted(ws: "Workspace") -> set[str]:
    """The workspace's mount points."""
    settings = getattr(ws, "_settings", None)
    return set(getattr(settings, "mounts", None) or ())


def catalog(ws: "Workspace") -> str:
    """The discovery primer: one line per installed skill, for the
    toolkit instructions. Empty string when there are no skills."""
    rows: list[str] = []
    root = skills_root(ws)
    try:
        with ws.lock:
            if not ws.files.fs.isdir(root):
                return ""
            for name in sorted(ws.files.fs.list(root)):
                path = f"{root}/{name}/SKILL.md"
                if not ws.files.fs.exists(path):
                    continue
                desc = frontmatter(ws.files.fs.read(path)).get("description", "")
                rows.append(f"- {name}: {desc}" if desc else f"- {name}")
    except Exception:
        return ""  # a broken skills dir must never block agent setup
    if not rows:
        return ""
    return (
        f"\n\nSkills — packaged guidance under {root}; when a task "
        "matches one, read its instructions first "
        f"(cat {root}/<name>/SKILL.md):\n"
        + "\n".join(rows)
        + f"\nSkills may be added mid-session: `ls {root}` to re-check."
    )
