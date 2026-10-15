"""Skills: /skills/<name>/SKILL.md convention — mounts, library-embedded
discovery, and the catalog primer."""

import importlib
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


def _write(ws, name, content):
    """An agent's own skill: files it writes, like any other."""
    path = f"{skills.skills_root(ws)}/{name}/SKILL.md"
    ws.files.fs.makedirs(path.rsplit("/", 1)[0], exist_ok=True)
    ws.files.fs.write(path, content)
    return path


def test_frontmatter_tolerates_utf8_bom(tmp_path):
    """A Windows-authored SKILL.md leads with a BOM; the frontmatter
    fields must still parse instead of failing startswith('---')."""
    bom_skill = b"\xef\xbb\xbf" + SKILL_MD
    assert skills.frontmatter(bom_skill)["name"] == "EV Data Cleaning"
    d = tmp_path / "bom"
    d.mkdir()
    (d / "SKILL.md").write_bytes(bom_skill)
    assert list(skills.mounts(d)) == ["/workspace/skills/ev-data-cleaning"]


def test_a_name_of_dots_alone_is_refused(tmp_path):
    """``name: ..`` would mount the skill at skills/.., outside the
    skills directory."""
    for name in ("..", ".", "..."):
        d = tmp_path / f"dots{len(name)}"
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody")
        with pytest.raises(ValueError, match="dots alone"):
            skills.mounts(d)


def test_leading_dots_are_dropped(tmp_path):
    """No skill lands as a hidden directory."""
    d = tmp_path / "hidden"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: .secret\n---\nbody")
    assert list(skills.mounts(d)) == ["/workspace/skills/secret"]
    (d / "SKILL.md").write_text("---\nname: ../escape\n---\nbody")
    assert list(skills.mounts(d)) == ["/workspace/skills/escape"]


def _demolib(tmp_path, monkeypatch, pkgname="demolib"):
    pkg = tmp_path / pkgname
    (pkg / "skills" / f"using-{pkgname}").mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "sub.py").write_text("y = 2\n")
    (pkg / "skills" / f"using-{pkgname}" / "SKILL.md").write_text(
        textwrap.dedent(f"""\
        ---
        name: using-{pkgname}
        description: how to drive {pkgname}
        ---
        call {pkgname}.x
        """)
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return importlib.import_module(pkgname)


def test_mounts_from_granted_modules(tmp_path, monkeypatch):
    """The convention: a granted library shipping <pkg>/skills/ teaches
    the agent how to use it — one gesture."""
    demolib = _demolib(tmp_path, monkeypatch)
    sub = importlib.import_module("demolib.sub")

    from nontainer import ModuleGrant

    python = PythonConfig(modules=[[demolib], ModuleGrant(sub)])
    try:
        points = skills.mounts_from_modules(python)
        assert list(points) == ["/workspace/skills/using-demolib"]  # one package, once
        assert points == skills.mounts_from_modules(python.modules)
        assert points == skills.mounts_from_modules(["demolib"])
        assert list(skills.mounts_from_modules(python, root="/")) == [
            "/skills/using-demolib"
        ]

        w = workspace(
            "skills-mod", store=tmp_path / "store", python=python, mounts=points
        )
        try:
            assert "- using-demolib: how to drive demolib" in skills.catalog(w)
            assert w.files.fs.exists("/workspace/skills/using-demolib/SKILL.md")
        finally:
            w.close()
    finally:
        sys.modules.pop("demolib", None)
        sys.modules.pop("demolib.sub", None)


def test_mounts_from_modules_with_no_skills_is_empty():
    import json

    assert skills.mounts_from_modules([json, "nosuchpackage_xyz"]) == {}
    assert skills.mounts_from_modules(PythonConfig()) == {}


def test_mounts_from_modules_refuses_two_skills_with_one_name(tmp_path, monkeypatch):
    a = _demolib(tmp_path, monkeypatch, "liba")
    b = _demolib(tmp_path, monkeypatch, "libb")
    clash = tmp_path / "libb" / "skills" / "using-libb" / "SKILL.md"
    clash.write_text("---\nname: using-liba\n---\n")
    try:
        with pytest.raises(ValueError, match="two skills named 'using-liba'"):
            skills.mounts_from_modules([a, b])
    finally:
        sys.modules.pop("liba", None)
        sys.modules.pop("libb", None)


def test_catalog_lists_frontmatter(ws):
    assert skills.catalog(ws) == ""  # no /skills: no primer text
    _write(ws, "ev-data-cleaning", SKILL_MD)
    _write(ws, "bare", b"---\nname: bare\n---\nno description")
    text = skills.catalog(ws)
    assert "- ev-data-cleaning: handling this dataset's NaN" in text
    assert "- bare" in text
    assert "ls /workspace/skills" in text


def test_agno_toolkit_instructions_include_catalog(ws):
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    _write(ws, "ev-data-cleaning", SKILL_MD)
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


def test_mounts_name_each_skill_by_its_frontmatter(starter, tmp_path):
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

        # the agent's own skill sits beside them, and is its work
        w.terminal("mkdir -p /workspace/skills/mine")
        w.terminal("echo '---\nname: mine\n---' > /workspace/skills/mine/SKILL.md")
        assert "- mine" in skills.catalog(w)
        status = w.terminal("cd /workspace && ws-git status --porcelain").stdout
        assert status == " A skills/mine/SKILL.md\n"

        # a fork sees the same skills
        kid = w.fork("mounted.kid", inherit="fresh")
        try:
            assert "- making-videos:" in skills.catalog(kid)
        finally:
            kid.close()
    finally:
        w.close()


def test_mounts_normalize_the_root_as_the_workspace_does(starter, tmp_path):
    points = skills.mounts(starter, root="//deep//nest//")
    assert "/deep/nest/skills/building-apps" in points
    w = workspace(
        "odd-root", store=tmp_path / "store", root="//deep//nest//", mounts=points
    )
    try:
        assert "- building-apps:" in skills.catalog(w)
    finally:
        w.close()
    with pytest.raises(ValueError, match=r"\. or \.\."):
        skills.mounts(starter, root="/a/../b")


def test_a_skill_inside_a_zip_is_refused(tmp_path):
    import zipfile

    archive = tmp_path / "lib.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("ziplib/skills/zipped/SKILL.md", "---\nname: zipped\n---\n")
    resource = zipfile.Path(archive, "ziplib/skills/zipped/")
    with pytest.raises(ValueError, match="nothing to mount"):
        skills.mounts(resource)
