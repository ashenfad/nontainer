"""Skills: packaged instructions that live IN the workspace.

Convention (Claude-Code-compatible): ``/skills/<name>/SKILL.md`` with
YAML frontmatter (``name``, ``description``) plus any sibling
reference/script files. The pieces divide cleanly:

- DISCOVERY is a primer job — the agno adapter catalogs frontmatter
  into the toolkit instructions (:func:`catalog`).
- ACCESS needs no new tools — the terminal and ``open()`` read skill
  files, and skill scripts run through run_python/terminal, inside the
  sandbox like all agent code (no host-execution side channel).
- STORAGE comes two ways. An agent's own skills are ordinary workspace
  files it writes: they version, fork, publish and rewind with
  everything else, and it can improve them like any other file.
  Skills that are never the agent's to change (an embedder's starter
  set, a library's) are MOUNTED: :func:`mounts` makes a read-only
  ``Mount`` per skill at ``<root>/skills/<name>``. The catalog lists
  both, and being mounts the embedder's are not versioned, so ws-git
  never sees them and no commit carries them — a skill the framework
  copied in would read as work the agent never did. An agent that
  wants to adapt a mounted skill copies it under a new name.

Python libraries can EMBED skills: a package shipping
``<pkg>/skills/<name>/SKILL.md`` teaches every agent it's granted to
(:func:`mounts_from_modules`) — granting a module and mounting its
usage guide become one gesture.
"""

from __future__ import annotations

import os
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
    """A skill's directory name. A name of dots alone would put the
    skill's mount point at ``skills/..`` (outside the skills directory)
    or ``skills/.``, so it is refused; leading dots are dropped, so no
    skill lands hidden."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")
    if slug and not slug.strip("."):
        raise ValueError(f"a skill can't be named {name!r}: a name of dots alone")
    return slug.lstrip(".-") or "skill"


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


def mounts(*sources: Any, root: str = "/workspace") -> "dict[str, Mount]":
    """Read-only mounts for skills that are never the agent's to change.

    Each source is a directory on disk: one skill (it holds a
    ``SKILL.md``) or a directory of them (``<name>/SKILL.md`` children),
    as a path or a package resource (``discover(module)`` returns those).
    Returns ``{"<root>/skills/<name>": Mount(dir, readonly=True)}``,
    each named by its frontmatter ``name`` (or its directory's), for
    ``workspace(..., mounts=...)``::

        ws = store.open("chat", mounts=skills.mounts(STARTER_SKILLS))

    A mounted skill is a live, read-only view of its directory: it is
    not versioned, so ws-git never lists it and no commit, fork point or
    publication copies it, and a fork sees the same directory. The
    catalog lists mounted skills beside the agent's own.

    Raises ``ValueError`` for a source that is not a directory on disk
    (a package imported from a zip has none to mount), for one with no
    skill in it, for two skills with one name, and for a skill named
    with dots alone.
    """
    from pathlib import Path

    from .workspace import Mount, normalize_root

    # normalized as the workspace normalizes its root, or the points
    # would not be where the catalog looks
    root = normalize_root(root)
    base = "" if root == "/" else root
    out: dict[str, Mount] = {}
    for source in sources:
        if not isinstance(source, (str, os.PathLike)):
            # a package resource with no directory behind it (a zip
            # import's zipfile.Path): nothing on disk to mount
            raise ValueError(
                f"skill source {source!s} is not a directory on disk, "
                "so there is nothing to mount (a package imported from a "
                "zip has none)"
            )
        path = Path(os.fspath(source))
        if not path.is_dir():
            raise ValueError(
                f"skill source {source!s} is not a directory on disk, "
                "so there is nothing to mount (a package imported from a "
                "zip has none)"
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


def mounts_from_modules(
    modules: Any, *, root: str = "/workspace"
) -> "dict[str, Mount]":
    """The library-embedded-skill convention: mounts for the skills
    every granted module's package ships (``<pkg>/skills/<name>/``), so
    granting a library and teaching the agent to use it are one
    gesture::

        python = PythonConfig(modules=[mylib, dataframes()])
        ws = store.open("chat", python=python,
                        mounts=skills.mounts_from_modules(python))

    ``modules`` is a :class:`PythonConfig` or its ``modules`` list: bare
    modules, ``ModuleGrant`` entries, module names, and nested lists of
    them (a preset's grants), each package looked at once. A package
    whose skills have no directory on disk (one imported from a zip) is
    skipped rather than refused, since there is nothing to mount. Named
    as :func:`mounts` names them, and like it raises ``ValueError`` for
    two skills with one name.
    """
    entries = getattr(modules, "modules", modules)

    def flat(seq: Any) -> Iterator[Any]:
        for entry in seq:
            if isinstance(entry, (list, tuple)):
                yield from flat(entry)
            else:
                yield entry

    dirs: list[Any] = []
    seen: set[str] = set()
    for entry in flat(entries or ()):
        mod = getattr(entry, "module", entry)
        name = mod if isinstance(mod, str) else getattr(mod, "__name__", "")
        top = name.split(".")[0]
        if not top or top in seen:
            continue
        seen.add(top)
        dirs.extend(
            skill_dir
            for skill_dir in discover(top)
            if isinstance(skill_dir, (str, os.PathLike)) and os.path.isdir(skill_dir)
        )
    return mounts(*dirs, root=root) if dirs else {}


def _skill_name(skill_dir: Any) -> str:
    """What a skill directory is called once mounted: its frontmatter
    name, or the directory's own."""
    try:
        meta = frontmatter((skill_dir / "SKILL.md").read_bytes())
    except Exception:
        meta = {}
    return _slug(meta.get("name") or getattr(skill_dir, "name", "") or "skill")


def catalog(ws: "Workspace") -> str:
    """The discovery primer: one line per skill under the skills
    directory, mounted or the agent's own, for the toolkit
    instructions. Empty string when there are no skills."""
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
