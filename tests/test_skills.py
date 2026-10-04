"""Skills: /skills/<name>/SKILL.md convention — install helpers,
library-embedded discovery, and the catalog primer."""

import sys
import textwrap

import pytest

from nontainer import PythonConfig, skills, workspace

SKILL_MD = b"""---
name: EV Data Cleaning
description: handling this dataset's NaN and mixed-type columns
---
# EV Data Cleaning

Drop NaNs before sorting: sorted(df['County'].dropna().unique())
"""


@pytest.fixture
def ws(tmp_path):
    w = workspace("skills-test", store=tmp_path)
    yield w
    w.close()


def test_install_bytes_names_from_frontmatter(ws):
    name = skills.install(ws, SKILL_MD)
    assert name == "ev-data-cleaning"  # slugified frontmatter name
    assert ws.files.fs.read("/workspace/skills/ev-data-cleaning/SKILL.md") == SKILL_MD
    # idempotent overwrite
    assert skills.install(ws, SKILL_MD) == "ev-data-cleaning"


def test_frontmatter_tolerates_utf8_bom(ws):
    """A Windows-authored SKILL.md leads with a BOM; the frontmatter
    fields must still parse instead of failing startswith('---')."""
    bom_skill = b"\xef\xbb\xbf" + SKILL_MD
    assert skills.frontmatter(bom_skill)["name"] == "EV Data Cleaning"
    assert skills.install(ws, bom_skill) == "ev-data-cleaning"


def test_install_skips_non_regular_files(ws, tmp_path):
    """A broken symlink (or fifo) in a skill directory is skipped, not
    read — read_bytes on it would raise (or block, for pipes)."""
    d = tmp_path / "my-skill"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"---\nname: linky\n---\nbody")
    (d / "dangling").symlink_to(tmp_path / "does-not-exist")
    assert skills.install(ws, d) == "linky"
    assert not ws.files.fs.exists("/workspace/skills/linky/dangling")


def test_install_directory_with_references(ws, tmp_path):
    d = tmp_path / "my-skill"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_bytes(b"---\ndescription: a demo\n---\nbody")
    (d / "references" / "guide.md").write_bytes(b"deep dive")
    name = skills.install(ws, d)
    assert name == "my-skill"  # no frontmatter name: directory fallback
    assert (
        ws.files.fs.read("/workspace/skills/my-skill/references/guide.md")
        == b"deep dive"
    )

    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ValueError, match="SKILL.md"):
        skills.install(ws, bare)


def test_install_md_file_fallback_names(ws, tmp_path):
    f = tmp_path / "publishing.md"
    f.write_bytes(b"# no frontmatter")
    assert skills.install(ws, f) == "publishing"

    d = tmp_path / "checklist"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"# body")
    assert skills.install(ws, d / "SKILL.md") == "checklist"  # parent dir name


def test_install_from_granted_modules(ws, tmp_path, monkeypatch):
    """The convention: a granted library shipping <pkg>/skills/ teaches
    the agent how to use it — one gesture."""
    pkg = tmp_path / "demolib"
    (pkg / "skills" / "using-demolib").mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "skills" / "using-demolib" / "SKILL.md").write_text(
        textwrap.dedent("""\
        ---
        name: using-demolib
        description: how to drive demolib
        ---
        call demolib.x
        """)
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import demolib  # noqa: F401

    w = workspace(
        "skills-mod", store=tmp_path / "store", python=PythonConfig(modules=[demolib])
    )
    try:
        installed = skills.install_from_modules(w)
        assert installed == ["using-demolib"]
        assert w.files.fs.exists("/workspace/skills/using-demolib/SKILL.md")
    finally:
        w.close()
        sys.modules.pop("demolib", None)


def test_install_lands_the_skill_over_an_open_composition(ws):
    """Installing a skill is the framework writing, at a moment the
    agent did not choose. It commits everything, which costs the agent
    nothing: ws-git measures against the agent's own last commit, so a
    composition in flight reads exactly as it did before."""
    ws.files.fs.write("/workspace/staged.txt", b"staged")
    ws.files.fs.write("/workspace/loose.txt", b"work in progress")
    ws.index.stage(["/workspace/staged.txt"])
    ws.index.commit("agent base")
    ws.files.fs.write("/workspace/loose.txt", b"still going")
    before = ws.index.status()

    name = skills.install(ws, SKILL_MD)

    installed = f"{skills.skills_root(ws)}/{name}/SKILL.md"
    assert ws.files.exists(installed)
    assert list(ws.log(limit=1))[0].info == {"tool": "skill", "skill": name}
    # the skill is durable, and so is the agent's work in progress
    head = ws._provider.files_at(ws.head)
    assert installed in head
    assert head["/workspace/loose.txt"] == b"still going"

    # the composition is exactly where the agent left it — plus the
    # skill's own file, which is a new file in the tree like any other
    after = ws.index.status()
    assert after.staged == before.staged
    assert set(after.unstaged) - set(before.unstaged) == {installed}
    assert "/workspace/loose.txt" in after.unstaged


def test_catalog_lists_frontmatter(ws):
    assert skills.catalog(ws) == ""  # no /skills: no primer text
    skills.install(ws, SKILL_MD)
    skills.install(ws, b"---\nname: bare\n---\nno description")
    text = skills.catalog(ws)
    assert "- ev-data-cleaning: handling this dataset's NaN" in text
    assert "- bare" in text
    assert "ls /workspace/skills" in text


def test_agno_toolkit_instructions_include_catalog(ws):
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    skills.install(ws, SKILL_MD)
    tk = WorkspaceTools(ws)
    assert "ev-data-cleaning" in tk.instructions
    assert "cat /workspace/skills/<name>/SKILL.md" in tk.instructions


# -- mounted skills: never the agent's to change ----------------------------------


def _skill(dirpath, name, description):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nDo it.\n"
    )
    return dirpath


@pytest.fixture
def starter(tmp_path):
    root = tmp_path / "starter"
    _skill(root / "building-apps", "building-apps", "how apps are built here")
    _skill(root / "vids", "Making Videos", "how videos are made here")
    (root / "notes").mkdir()  # no SKILL.md: not a skill
    return root


def test_mounts_name_each_skill_as_install_would(starter, tmp_path):
    points = skills.mounts(starter)
    assert sorted(points) == [
        "/workspace/skills/building-apps",
        "/workspace/skills/making-videos",  # from its frontmatter, slugged
    ]
    assert all(m.readonly for m in points.values())
    one = skills.mounts(starter / "vids", root="/")
    assert list(one) == ["/skills/making-videos"]


def test_mounts_refuse_what_cannot_be_mounted(starter, tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        skills.mounts(tmp_path / "missing")
    with pytest.raises(ValueError, match="no skills"):
        skills.mounts(starter / "notes")
    _skill(tmp_path / "other" / "building-apps", "building-apps", "a second one")
    with pytest.raises(ValueError, match="two skills named 'building-apps'"):
        skills.mounts(starter, tmp_path / "other")


def test_a_mounted_skill_is_listed_read_only_and_never_work(starter, tmp_path):
    from nontainer.wsgit import register_wsgit

    w = workspace("mounted", store=tmp_path / "store", mounts=skills.mounts(starter))
    register_wsgit(w)
    try:
        catalog = skills.catalog(w)
        assert "- building-apps: how apps are built here" in catalog
        assert "- making-videos: how videos are made here" in catalog
        assert w.terminal("cat /workspace/skills/vids/SKILL.md").exit_code != 0
        assert (
            "Do it."
            in w.terminal("cat /workspace/skills/making-videos/SKILL.md").stdout
        )
        # never work: no status, and no write
        assert w.terminal("cd /workspace && ws-git status --porcelain").stdout == ""
        r = w.terminal("echo x > /workspace/skills/building-apps/SKILL.md")
        assert r.exit_code != 0 and "Read-only" in (r.stdout + r.stderr)

        # an installed skill sits beside them, and is the agent's
        skills.install(w, SKILL_MD)
        assert "- ev-data-cleaning:" in skills.catalog(w)
        with pytest.raises(Exception, match="mounted read-only"):
            skills.install(w, starter / "building-apps")

        # a fork sees the same skills
        kid = w.fork("mounted.kid", inherit="fresh")
        try:
            assert "- making-videos:" in skills.catalog(kid)
        finally:
            kid.close()
    finally:
        w.close()


def test_install_from_modules_leaves_a_mounted_library_skill_alone(
    tmp_path, monkeypatch
):
    pkg = tmp_path / "mountlib"
    _skill(pkg / "skills" / "using-mountlib", "using-mountlib", "how to drive mountlib")
    (pkg / "__init__.py").write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import mountlib

    w = workspace(
        "skills-mounted-mod",
        store=tmp_path / "store",
        python=PythonConfig(modules=[mountlib]),
        mounts=skills.mounts(*skills.discover(mountlib)),
    )
    try:
        assert skills.install_from_modules(w) == []
        assert "- using-mountlib: how to drive mountlib" in skills.catalog(w)
    finally:
        w.close()
        sys.modules.pop("mountlib", None)
