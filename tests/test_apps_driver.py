"""The driver seam: the half of test_app that needs no browser.

Every test here runs a FAKE driver — one that answers a canned report
and keeps the spec it was handed — so what is under test is the neutral
half: what a spec promises a driver, how a report becomes a result, and
which driver a run picks. If any of this needed Chromium, the seam
would not be real.
"""

import pytest

from nontainer import Workspace
from nontainer.apps import AppsConfig, enable_apps, render_test_app, testapp
from nontainer.apps.driver import (
    ActionOutcome,
    DriveReport,
    DriveSpec,
    PageError,
    Refusal,
    pick_driver,
)
from nontainer.providers import KvgitProvider

INDEX = b"""<!doctype html>
<html><body><div id="out">hi</div>
<script src="app.js"></script>
</body></html>
"""

APP_JS = b"""function boom() {
  throw new Error('from the agent');
}
"""

HANDLER = b"""
def get(req):
    return {"scores": ["alice"]}
"""


class FakeDriver:
    """Answers a canned report and remembers what it was asked to do."""

    def __init__(self, build=None):
        self._build = build or (lambda spec: DriveReport(loaded=True))
        self.specs = []

    def run(self, spec: DriveSpec) -> DriveReport:
        self.specs.append(spec)
        return self._build(spec)


def make_ws(session, build=None, **cfg):
    driver = FakeDriver(build)
    ws = Workspace(KvgitProvider.open(None, session=session))
    rt = enable_apps(ws, AppsConfig(driver=driver, **cfg))
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write("/workspace/app/index.html", INDEX)
    ws.files.fs.write("/workspace/app/app.js", APP_JS)
    ws.files.fs.write("/workspace/app/api/scores.py", HANDLER)
    ws.commit()
    return ws, rt, driver


# -- what the spec carries --------------------------------------------------


def test_the_spec_serves_the_app_without_a_browser():
    """The invariant the seam exists for: a driver is handed the
    dispatch the publication will use, so anything that can call a
    Python function can verify the app."""
    ws, rt, driver = make_ws("spec-serve")
    try:
        rt.test_app([])
        (spec,) = driver.specs
        wire = spec.serve("GET", "/api/scores", b"", {})
        assert wire.status == 200
        assert b"alice" in wire.content
        page = spec.serve("GET", "/", b"", {})
        assert page.content == INDEX
        # The served policy rides on the HTML, because a policy governs
        # behaviour (eval, blob workers) that no interception can see.
        assert "script-src" in page.headers["content-security-policy"]
    finally:
        ws.close()


def test_the_spec_resolves_what_the_driver_should_not_decide():
    ws, rt, driver = make_ws("spec-fields")
    try:
        rt.test_app([{"read": "#out"}], viewport="mobile", max_screenshots=2)
        (spec,) = driver.specs
        assert spec.base_url == "https://nontainer.test/apps/t-test/"
        assert (spec.width, spec.height) == (390, 844)
        assert spec.actions == ({"read": "#out"},)
        assert spec.max_screenshots == 2
        assert spec.screenshot_dir == "/workspace/app/screenshots"
        assert "esm.sh" in spec.script_hosts
        assert "absolute path" in spec.off_base_body
    finally:
        ws.close()


def test_a_custom_policy_joins_the_script_hosts_the_driver_gets():
    """Interception has to agree with the policy on the wire: a host the
    served policy allows must not be aborted as a false red."""
    ws, rt, driver = make_ws(
        "spec-csp", csp="default-src 'self'; script-src 'self' https://esm.corp"
    )
    try:
        rt.test_app([])
        (spec,) = driver.specs
        assert "esm.corp" in spec.script_hosts
        assert spec.csp.startswith("default-src 'self'")
    finally:
        ws.close()


# -- what the report becomes ------------------------------------------------


def test_screenshots_are_written_from_the_report():
    """Bytes come back from the drive and land in the workspace, so they
    version, fork and check out with the session."""
    png = b"\x89PNG\r\n\x1a\nfake"

    def build(spec):
        path = f"{spec.screenshot_dir}/shot-1.png"
        return DriveReport(
            loaded=True,
            actions=(ActionOutcome(0, ok=True, value=path),),
            screenshots={path: png},
        )

    ws, rt, driver = make_ws("shots", build)
    try:
        result = rt.test_app([{"screenshot": True}])
        assert result.ok, render_test_app(result)
        assert result.screenshots == ("/workspace/app/screenshots/shot-1.png",)
        assert ws.files.fs.read(result.screenshots[0]) == png
        assert result.screenshots[0] in render_test_app(result)
    finally:
        ws.close()


def test_a_page_error_is_annotated_against_the_workspace():
    """The driver reports a stack unparsed; picking the agent's own
    frame and quoting the line means reading the workspace, which is the
    host's to do."""
    stack = (
        "Error: from the agent\n"
        "    at nodeModules (https://esm.sh/x.js:9:1)\n"
        "    at boom (https://nontainer.test/apps/t-test/app.js:2:9)\n"
    )
    build = lambda spec: DriveReport(  # noqa: E731
        loaded=True,
        page_errors=(PageError("Error", "from the agent", stack),),
    )
    ws, rt, driver = make_ws("annotate", build)
    try:
        result = rt.test_app([])
        assert not result.ok  # a page error fails the run
        (page_error,) = result.page_errors
        assert "at boom (app.js:2:9)" in page_error
        assert "+1 frame above it in library code" in page_error
        assert "throw new Error('from the agent');" in page_error
    finally:
        ws.close()


@pytest.mark.parametrize(
    "refusal, ok, expected",
    [
        # An absolute url is always an app bug, and the 404 carries a
        # body — so an app that skips the .ok check renders as if fine.
        (Refusal("off-base", "/api/x"), False, "absolute path"),
        # Interception refused it; the page never ran it and never said
        # so, but the policy is what fails a run.
        (
            Refusal("script", "https://evil.test/x.js", resource_type="script"),
            True,
            "scripts may only load",
        ),
        (
            Refusal("resource", "https://evil.test/x.png", resource_type="image"),
            True,
            "img-src",
        ),
        # Code that did not run: a run reporting PASS here would leave
        # the false green intact one layer up.
        (
            Refusal("policy", "blob:nontainer.test/1", directive="script-src-elem"),
            False,
            "a refused script does not throw",
        ),
    ],
)
def test_each_refusal_is_phrased_as_the_fix(refusal, ok, expected):
    """A driver reports what was refused; the words the agent repairs
    from are written here, and so is the verdict."""
    build = lambda spec: DriveReport(loaded=True, refusals=(refusal,))  # noqa: E731
    ws, rt, driver = make_ws(f"refuse-{refusal.kind}", build)
    try:
        result = rt.test_app([])
        assert result.ok is ok, render_test_app(result)
        (note,) = result.rejected
        assert expected in note
    finally:
        ws.close()


def test_a_refused_image_is_a_warning_not_a_failure():
    """A blemish on a page that otherwise works: only code that did not
    RUN turns a run red."""
    build = lambda spec: DriveReport(  # noqa: E731
        loaded=True,
        refusals=(Refusal("policy", "https://x/y.png", directive="img-src"),),
    )
    ws, rt, driver = make_ws("refuse-image", build)
    try:
        result = rt.test_app([])
        assert result.ok, render_test_app(result)
        assert "img-src" in result.rejected[0]
    finally:
        ws.close()


def test_console_repeats_collapse_into_a_count():
    build = lambda spec: DriveReport(  # noqa: E731
        loaded=True, console=(("[warning] cdn", 32), ("[log] booted", 1))
    )
    ws, rt, driver = make_ws("console", build)
    try:
        result = rt.test_app([])
        assert result.console == ("[warning] cdn (x32)", "[log] booted")
    finally:
        ws.close()


def test_an_action_value_is_described_by_the_action_that_asked():
    """``eval`` answers with a VALUE, so it is repr'd: ``'2'`` and ``2``
    are different answers, and a bare 2 hides which one came back."""

    def build(spec):
        return DriveReport(
            loaded=True,
            actions=(
                ActionOutcome(0, ok=True, value="hi"),
                ActionOutcome(1, ok=True, value=2),
                ActionOutcome(2, ok=False, value=False, error="assertion is falsy"),
            ),
        )

    ws, rt, driver = make_ws("values", build)
    try:
        result = rt.test_app([{"read": "#out"}, {"eval": "2"}, {"assert": "1 === 2"}])
        assert not result.ok
        assert [r.value for r in result.results] == ["hi", "2", "False"]
        assert result.results[2].error == "assertion is falsy"
    finally:
        ws.close()


def test_a_load_error_comes_through_as_a_result():
    """test_app answers with a result for browser problems too: a run
    that never started is FAIL, not a raise."""
    build = lambda spec: DriveReport(load_error="net::ERR_ABORTED")  # noqa: E731
    ws, rt, driver = make_ws("load", build)
    try:
        result = rt.test_app([{"read": "#out"}])
        assert not result.ok
        assert result.load_error == "net::ERR_ABORTED"
        assert result.results == ()
        assert "[load error] net::ERR_ABORTED" in render_test_app(result)
    finally:
        ws.close()


def test_a_report_that_did_not_load_is_a_load_failure():
    """A driver that says the page did not load, and phrases nothing,
    still fails the run: an empty action list on an unloaded page is
    not a pass."""
    build = lambda spec: DriveReport(loaded=False)  # noqa: E731
    ws, rt, driver = make_ws("unloaded", build)
    try:
        result = rt.test_app([])
        assert not result.ok
        assert result.load_error == "the page did not load"
    finally:
        ws.close()


def test_a_screenshot_that_cannot_be_saved_fails_its_action(monkeypatch):
    """The driver captured it, the workspace could not keep it: that is
    the screenshot action's failure, and the rest of the run stands."""
    png = b"\x89PNG\r\n\x1a\nfake"

    def build(spec):
        path = f"{spec.screenshot_dir}/shot-1.png"
        return DriveReport(
            loaded=True,
            actions=(
                ActionOutcome(0, ok=True, value=path),
                ActionOutcome(1, ok=True, value="42"),
            ),
            screenshots={path: png},
        )

    ws, rt, driver = make_ws("unsaved", build)
    try:

        def refuse(runtime, path, png):
            raise OSError("disk full")

        monkeypatch.setattr(testapp, "_save_screenshot", refuse)
        result = rt.test_app([{"screenshot": True}, {"read": "#out"}])
        assert not result.ok
        assert result.screenshots == ()
        assert result.results[0].ok is False
        assert "screenshot not saved: disk full" == result.results[0].error
        assert result.results[1].ok and result.results[1].value == "42"
    finally:
        ws.close()


def test_a_driver_that_raises_is_a_load_error_not_a_raise():
    def build(spec):
        raise RuntimeError("no browser here")

    ws, rt, driver = make_ws("raises", build)
    try:
        result = rt.test_app([])
        assert not result.ok
        assert "FakeDriver failed: no browser here" == result.load_error
    finally:
        ws.close()


def test_the_request_log_is_flushed_whatever_the_driver_did():
    def build(spec):
        spec.serve("GET", "/api/scores", b"", {})
        raise RuntimeError("and then it died")

    ws, rt, driver = make_ws("flush", build)
    try:
        rt.test_app([])
        log = ws.files.fs.read("/workspace/app/logs/api.log").decode()
        assert "GET /api/scores -> 200" in log
    finally:
        ws.close()


# -- who picks --------------------------------------------------------------


class _Runtime:
    def __init__(self, app_driver):
        self.app_driver = app_driver


class _Workspace:
    def __init__(self, app_driver):
        self.runtime = _Runtime(app_driver)


def test_the_config_driver_wins():
    config, executors = AppsConfig(driver="from-config"), _Workspace("from-executor")
    assert pick_driver(config, executors) == "from-config"


def test_the_executor_offers_one_when_the_config_does_not():
    assert pick_driver(AppsConfig(), _Workspace("from-executor")) == "from-executor"


def test_playwright_is_the_fallback():
    from nontainer.apps.driver_playwright import PlaywrightDriver

    assert isinstance(pick_driver(AppsConfig(), _Workspace(None)), PlaywrightDriver)
    assert isinstance(pick_driver(), PlaywrightDriver)


def test_an_executor_offers_no_driver_by_default():
    """Local and guest rungs verify on the host's browser: a rung that
    could run the app the way a visitor will says so by defining the
    name, and none does yet."""
    ws = Workspace(KvgitProvider.open(None, session="no-driver"))
    try:
        assert ws.runtime.app_driver is None
    finally:
        ws.close()
