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
    assert ws.fs.read("/skills/ev-data-cleaning/SKILL.md") == SKILL_MD
    # idempotent overwrite
    assert skills.install(ws, SKILL_MD) == "ev-data-cleaning"


def test_install_directory_with_references(ws, tmp_path):
    d = tmp_path / "my-skill"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_bytes(b"---\ndescription: a demo\n---\nbody")
    (d / "references" / "guide.md").write_bytes(b"deep dive")
    name = skills.install(ws, d)
    assert name == "my-skill"  # no frontmatter name: directory fallback
    assert ws.fs.read("/skills/my-skill/references/guide.md") == b"deep dive"

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
        assert w.fs.exists("/skills/using-demolib/SKILL.md")
    finally:
        w.close()
        sys.modules.pop("demolib", None)


def test_catalog_lists_frontmatter(ws):
    assert skills.catalog(ws) == ""  # no /skills: no primer text
    skills.install(ws, SKILL_MD)
    skills.install(ws, b"---\nname: bare\n---\nno description")
    text = skills.catalog(ws)
    assert "- ev-data-cleaning: handling this dataset's NaN" in text
    assert "- bare" in text
    assert "ls /skills" in text


def test_agno_toolkit_instructions_include_catalog(ws):
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    skills.install(ws, SKILL_MD)
    tk = WorkspaceTools(ws)
    assert "ev-data-cleaning" in tk.instructions
    assert "cat /skills/<name>/SKILL.md" in tk.instructions
