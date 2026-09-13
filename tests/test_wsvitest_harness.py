"""The ws-vitest harness and its driver: what the page supplies, what
it refuses, and what the browser is allowed to reach.

These drive the harness directly (``run_js_tests``), below the verb —
what is tested here is the JavaScript surface and the hermetic rules,
not the flag parsing. Every case needs Chromium, so the whole module
skips where the ``[apps]`` browser is not installed.
"""

import pytest

from nontainer import Workspace
from nontainer.apps.jsharness import UNIT_CSP, run_js_tests
from nontainer.providers import KvgitProvider

UTIL = """\
export function add(a, b) {
  return a + b;
}

export const LIMIT = 3;
"""


@pytest.fixture
def ws(chromium_available):
    w = Workspace(KvgitProvider.open(None, session="wsvitest-harness"))
    w.files.fs.write("/workspace/app/util.js", UTIL.encode())
    yield w
    w.close()


def write(ws, rel, source):
    ws.files.fs.write(f"/workspace/{rel}", source.encode())
    return rel


def run(ws, rel, source, **kw):
    write(ws, rel, source)
    results = run_js_tests(ws, [rel], **kw)
    assert len(results) == 1
    return results[0]


def outcome(result, name):
    for o in result.outcomes:
        if o.name.endswith(name):
            return o
    raise AssertionError(
        f"no outcome named {name!r} in {[o.name for o in result.outcomes]}"
    )


# -- the harness surface ------------------------------------------------


def test_a_passing_file_reports_every_test(ws):
    result = run(
        ws,
        "tests/pass.test.js",
        """
        describe('add', () => {
          it('adds', () => { expect(1 + 1).toBe(2); });
          test('test is it', () => { expect([1, 2]).toHaveLength(2); });
        });
        """,
    )
    assert result.load_error is None, result.load_error
    assert result.collection_error is None, result.collection_error
    assert [o.status for o in result.outcomes] == ["passed", "passed"]
    assert outcome(result, "adds").name == "tests/pass.test.js > add > adds"


def test_a_failing_matcher_reads_like_vitests(ws):
    result = run(
        ws,
        "tests/fail.test.js",
        "it('is wrong', () => { expect(3).toBe(4); });\n",
    )
    o = outcome(result, "is wrong")
    assert o.status == "failed"
    assert o.message == "AssertionError: expected 3 to be 4 // Object.is equality"


def test_a_thrown_error_names_the_test_files_line(ws):
    result = run(
        ws,
        "tests/throw.test.js",
        "it('boom', () => {\n  throw new Error('kaboom');\n});\n",
    )
    o = outcome(result, "boom")
    assert o.status == "failed"
    assert o.message == "Error: kaboom"
    assert o.file == "tests/throw.test.js"
    assert o.line == 2, o.frames
    assert o.frames[-1].column > 0
    assert o.frames[-1].source[-1][1].strip() == "throw new Error('kaboom');"


def test_an_async_test_is_awaited(ws):
    result = run(
        ws,
        "tests/async.test.js",
        """
        it('awaits', async () => {
          const v = await Promise.resolve(7);
          expect(v).toBe(7);
        });
        it('reports an async failure', async () => {
          await Promise.resolve();
          expect(1).toBe(2);
        });
        """,
    )
    assert outcome(result, "awaits").status == "passed"
    assert outcome(result, "reports an async failure").status == "failed"


def test_before_each_runs_before_every_test(ws):
    result = run(
        ws,
        "tests/hooks.test.js",
        """
        let n = 0;
        beforeEach(() => { n += 1; });
        afterEach(() => { n += 10; });
        it('first', () => { expect(n).toBe(1); });
        it('second', () => { expect(n).toBe(12); });
        """,
    )
    assert [o.status for o in result.outcomes] == ["passed", "passed"]


def test_the_matcher_set_covers_the_named_family(ws):
    result = run(
        ws,
        "tests/matchers.test.js",
        """
        it('covers them', async () => {
          expect({a: [1]}).toEqual({a: [1]});
          expect({a: 1}).toStrictEqual({a: 1});
          expect([1, 2]).toContain(2);
          expect('abc').toContain('b');
          expect(1).toBeTruthy();
          expect(0).toBeFalsy();
          expect(null).toBeNull();
          expect(undefined).toBeUndefined();
          expect(1).toBeDefined();
          expect(() => { throw new Error('x'); }).toThrow('x');
          expect('hello').toMatch(/ell/);
          expect(3).toBeGreaterThan(2);
          expect(3).toBeGreaterThanOrEqual(3);
          expect(2).toBeLessThan(3);
          expect(2).toBeLessThanOrEqual(2);
          expect(0.1 + 0.2).toBeCloseTo(0.3);
          expect(1).not.toBe(2);
          await expect(Promise.resolve(1)).resolves.toBe(1);
          await expect(Promise.reject(new Error('no'))).rejects.toThrow('no');
        });
        """,
    )
    assert outcome(result, "covers them").status == "passed", outcome(
        result, "covers them"
    ).message


def test_vi_fn_records_its_calls(ws):
    result = run(
        ws,
        "tests/vifn.test.js",
        """
        it('records', () => {
          const f = vi.fn((x) => x * 2);
          expect(f(3)).toBe(6);
          expect(f).toHaveBeenCalled();
          expect(f).toHaveBeenCalledTimes(1);
          expect(f).toHaveBeenCalledWith(3);
          expect(f.mock.calls).toEqual([[3]]);
        });
        it('spies on an object we own', () => {
          const obj = {greet: (n) => 'hi ' + n};
          const spy = vi.spyOn(obj, 'greet');
          obj.greet('ann');
          expect(spy).toHaveBeenCalledWith('ann');
          spy.mockRestore();
        });
        it('jest is vi', () => { expect(jest).toBe(vi); });
        """,
    )
    assert [o.status for o in result.outcomes] == ["passed"] * 3, [
        (o.name, o.message) for o in result.outcomes
    ]


def test_an_import_of_a_module_under_app_resolves(ws):
    result = run(
        ws,
        "tests/imports.test.js",
        """
        import { add, LIMIT } from '../app/util.js';
        it('imports the module under test', () => {
          expect(add(2, 3)).toBe(5);
          expect(LIMIT).toBe(3);
        });
        """,
    )
    assert outcome(result, "imports the module under test").status == "passed"


def test_the_import_map_spelling_works(ws):
    result = run(
        ws,
        "tests/importmap.test.js",
        """
        import { describe, it, expect } from 'vitest';
        import { test } from '@jest/globals';
        describe('imported', () => {
          it('resolves vitest', () => { expect(typeof expect).toBe('function'); });
          test('resolves @jest/globals', () => { expect(1).toBe(1); });
        });
        """,
    )
    assert [o.status for o in result.outcomes] == ["passed", "passed"], [
        (o.name, o.message) for o in result.outcomes
    ]


# -- the refusals -------------------------------------------------------


def test_vi_mock_is_refused_with_the_idiom_that_replaces_it(ws):
    result = run(
        ws,
        "tests/vimock.test.js",
        "it('mocks', () => { vi.mock('../app/util.js'); });\n",
    )
    o = outcome(result, "mocks")
    assert o.status == "failed"
    assert "vi.mock" in o.message
    assert "vi.spyOn" in o.message or "stubFetch" in o.message


def test_snapshots_and_expect_extend_are_refused(ws):
    result = run(
        ws,
        "tests/refused.test.js",
        """
        it('snapshots', () => { expect(1).toMatchSnapshot(); });
        it('extends', () => { expect.extend({toBeOdd: () => ({pass: true})}); });
        """,
    )
    assert outcome(result, "snapshots").status == "failed"
    assert "snapshot" in outcome(result, "snapshots").message
    assert outcome(result, "extends").status == "failed"
    assert "expect.extend" in outcome(result, "extends").message


# -- hermetic -----------------------------------------------------------


def test_a_fetch_to_an_api_route_fails(ws):
    ws.files.fs.write(
        "/workspace/app/api/scores.py", b"def get(req):\n    return {'x': 1}\n"
    )
    result = run(
        ws,
        "tests/api.test.js",
        """
        it('reaches no api route', async () => {
          const r = await fetch('api/scores');
          expect(r.ok).toBe(true);
        });
        """,
    )
    o = outcome(result, "reaches no api route")
    assert o.status == "failed"


def test_a_stubbed_fetch_succeeds(ws):
    result = run(
        ws,
        "tests/stub.test.js",
        """
        it('reads the stub', async () => {
          vi.stubFetch({'api/scores': {scores: ['ann']}});
          const r = await fetch('api/scores');
          expect(r.status).toBe(200);
          expect(await r.json()).toEqual({scores: ['ann']});
        });
        it('an unstubbed route still fails', async () => {
          vi.stubFetch({'api/scores': {}});
          await expect(fetch('api/other')).rejects.toThrow('no stub');
        });
        """,
    )
    assert [o.status for o in result.outcomes] == ["passed", "passed"], [
        (o.name, o.message) for o in result.outcomes
    ]


def test_an_external_fetch_is_blocked_by_the_policy_not_the_network(ws):
    result = run(
        ws,
        "tests/external.test.js",
        """
        it('cannot reach the internet', async () => {
          await expect(fetch('https://example.com/x')).rejects.toThrow();
        });
        """,
    )
    assert outcome(result, "cannot reach the internet").status == "passed"
    assert any("connect-src" in v for v in result.violations), result.violations
    assert any("example.com" in v for v in result.violations), result.violations


def test_the_unit_policy_is_stricter_than_the_apps_one():
    """The unit tier writes its own policy: `AppsConfig.csp_extend` is
    extend-only, so no configuration path tightens `connect-src 'self'
    https:` down to the page's own origin."""
    assert "connect-src 'self';" in UNIT_CSP
    assert "https:" not in UNIT_CSP
    assert "script-src 'self' 'unsafe-inline';" in UNIT_CSP


def test_a_module_level_throw_is_a_load_error_not_a_test(ws):
    result = run(
        ws,
        "tests/broken.test.js",
        "throw new Error('this file does not load');\nit('never', () => {});\n",
    )
    assert result.outcomes == ()
    assert result.collection_error is not None
    assert "this file does not load" in result.collection_error


def test_backend_source_is_not_served_to_the_page(ws):
    """`app/api/` is backend source and the app's own static dispatch
    refuses that subtree, so the harness refuses it too: a test cannot
    import it even by spelling the path out."""
    ws.files.fs.write("/workspace/app/api/_lib.js", b"export const secret = 1;\n")
    result = run(
        ws,
        "tests/backend.test.js",
        """
        it('cannot read app/api', async () => {
          const r = await fetch('app/api/_lib.js');
          expect(r.status).toBe(404);
          expect(await r.text()).not.toContain('secret');
        });
        """,
    )
    assert outcome(result, "cannot read app/api").status == "passed"


def test_spying_on_a_modules_export_says_what_the_browser_refuses(ws):
    """A module namespace object is read-only in a browser, so
    `vi.spyOn(mod, 'fn')` cannot replace anything. The message names
    the two spellings that work instead of leaving a silent no-op."""
    result = run(
        ws,
        "tests/spymodule.test.js",
        """
        import * as util from '../app/util.js';
        it('refuses to spy on a module', () => {
          vi.spyOn(util, 'add');
        });
        """,
    )
    o = outcome(result, "refuses to spy on a module")
    assert o.status == "failed"
    assert "read-only" in o.message
    assert "vi.stubFetch" in o.message or "argument" in o.message


def test_a_hook_below_its_test_still_applies(ws):
    """A block is collected before anything in it runs, so a beforeEach
    written under an it() is still that test's hook."""
    result = run(
        ws,
        "tests/latehook.test.js",
        """
        describe('order', () => {
          it('sees the hook below it', () => { expect(globalThis.__n).toBe(1); });
          beforeEach(() => { globalThis.__n = 1; });
        });
        """,
    )
    assert outcome(result, "sees the hook below it").status == "passed"
