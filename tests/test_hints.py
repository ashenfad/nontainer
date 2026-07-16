"""error_hint: every predictable sandbox collision gets its door labeled.

Unit tests pin the matchers to the exact error phrasings the sandbox
(and plotly) actually emit; the round-trip tests prove the hint reaches
the rendered observation an agent would read.
"""

from nontainer import PythonConfig, Workspace
from nontainer.adapters.render import render_python
from nontainer.hints import error_hint
from nontainer.providers import KvgitProvider


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="hints"), **kwargs)


# -- matchers (unit) ----------------------------------------------------------


def test_blocked_import_still_routes_through_error_hint():
    hint = error_hint("ImportError: Import of 'requests' is not allowed (line 1)")
    assert hint and "curl" in hint


def test_shutil_gets_a_file_ops_redirect():
    hint = error_hint("ImportError: Import of 'shutil' is not allowed (line 2)")
    assert hint and "cp" in hint and "open()" in hint


def test_dunder_import_redirects_to_plain_import():
    hint = error_hint("sandtrap.errors.StValidationError: Cannot access '__import__'")
    assert hint and "import statements work" in hint


def test_kaleido_matches_the_plotly5_message():
    # verbatim from a session transcript (plotly 5.x)
    text = (
        "ValueError: \n"
        'Image export using the "kaleido" engine requires the Kaleido package,\n'
        "which can be installed using pip:\n"
        "\n"
        "    $ pip install --upgrade kaleido\n"
    )
    hint = error_hint(text)
    assert hint and "ui = {...}" in hint and "matplotlib" in hint


def test_kaleido_matches_the_kaleido_v1_phrasing():
    # plotly 6 / kaleido v1 words it differently but still says both
    text = (
        "ValueError: Kaleido is required for image export but is not "
        "installed. Install it with: pip install kaleido"
    )
    assert error_hint(text) is not None


def test_tick_limit_says_vectorize():
    hint = error_hint(
        "sandtrap.errors.StTickLimit: Execution exceeded 1000000 tick limit"
    )
    assert hint and "vector" in hint


def test_unrelated_errors_get_no_hint():
    assert error_hint("ValueError: cannot convert float NaN to integer") is None
    assert error_hint("") is None


# -- round trips: the hint reaches the agent's observation --------------------


def test_shutil_import_renders_the_hint():
    ws = make_ws()
    text = render_python(ws.run_python("import shutil"))
    assert "[hint: " in text and "cp" in text
    ws.close()


def test_dunder_import_renders_the_hint():
    ws = make_ws()
    text = render_python(ws.run_python("np = __import__('numpy')"))
    assert "[hint: " in text and "import statements work" in text
    ws.close()


def test_tick_limit_renders_the_hint():
    ws = make_ws(python=PythonConfig(tick_limit=500))
    text = render_python(ws.run_python("t = 0\nfor i in range(100000):\n    t += i"))
    assert "tick limit" in text
    assert "[hint: " in text and "vector" in text
    ws.close()
