"""The ws-vitest harness is package data, so it has to be IN the package.

Moving the harness out of a Python string and into ``harness.js`` gives
it syntax highlighting, a linter and a readable diff — and one new way
to break: a file the wheel does not carry. An installed nontainer whose
harness is missing serves an empty module, and every JavaScript test in
every workspace fails with nothing to explain it. So the resource is
resolved the way the running code resolves it, and a real wheel is
built and looked inside.
"""

import shutil
import subprocess
import zipfile
from importlib import resources
from pathlib import Path

import pytest

import nontainer
from nontainer.apps import jsharness

ASSETS = (jsharness.HARNESS_FILE, jsharness.PAGE_FILE)

ROOT = Path(nontainer.__file__).resolve().parent.parent


def test_the_assets_resolve_from_the_package():
    for name in ASSETS:
        data = resources.files("nontainer.apps").joinpath(name).read_bytes()
        assert data, f"{name} is empty"
        assert jsharness.asset(name) == data


def test_the_harness_is_the_javascript_the_page_is_served():
    js = jsharness.harness_js()
    assert js.startswith(b"/* ws-vitest harness")
    assert b"export async function __run" in js
    assert b"function stubFetch" in js


def test_the_page_shell_keeps_exactly_its_two_inline_scripts():
    """The import map and the entry module stay inline — that is what
    `'unsafe-inline'` in the unit policy is for. The harness is a file,
    so `'self'` covers it, and nothing else may become inline without
    the policy changing to match."""
    page = jsharness.asset(jsharness.PAGE_FILE)
    assert page.count(b"<script") == 2
    assert b'<script type="importmap">' in page
    assert b'<script type="module">' in page
    assert b"./__nt/harness.js" in page
    assert jsharness.HARNESS_PATH.encode() in page


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_a_built_wheel_carries_the_assets(tmp_path):
    """The wheel target builds from the package directory, so the two
    data files ship with no config entry naming them. That is a fact
    about hatchling's defaults rather than a promise this repo makes, so
    it is checked against a wheel rather than trusted."""
    if not (ROOT / "pyproject.toml").exists():
        pytest.skip("not a source checkout")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    built = list(tmp_path.glob("*.whl"))
    assert len(built) == 1, built
    with zipfile.ZipFile(built[0]) as wheel:
        names = set(wheel.namelist())
        for name in ASSETS:
            member = f"nontainer/apps/{name}"
            assert member in names, sorted(
                n for n in names if n.startswith("nontainer/apps/")
            )
            assert wheel.read(member) == jsharness.asset(name)
