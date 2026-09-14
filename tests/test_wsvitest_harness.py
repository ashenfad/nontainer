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


# -- deep equality over the built-in collection types --------------------


def test_maps_compare_by_their_entries(ws):
    result = run(
        ws,
        "tests/eqmap.test.js",
        """
        it('compares maps', () => {
          expect(new Map([['a', 1]])).toEqual(new Map([['a', 1]]));
          expect(new Map([['b', 2], ['a', 1]])).toEqual(new Map([['a', 1], ['b', 2]]));
          expect(new Map([['a', {x: 1}]])).toEqual(new Map([['a', {x: 1}]]));
          expect(new Map([['a', 1]])).not.toEqual(new Map([['a', 2]]));
          expect(new Map([['a', 1]])).not.toEqual(new Map([['b', 1]]));
          expect(new Map([['a', 1]])).not.toEqual(new Map());
          expect(new Map([['a', {x: 1}]])).not.toEqual(new Map([['a', {x: 2}]]));
        });
        """,
    )
    o = outcome(result, "compares maps")
    assert o.status == "passed", o.message


def test_sets_compare_by_their_members(ws):
    result = run(
        ws,
        "tests/eqset.test.js",
        """
        it('compares sets', () => {
          expect(new Set([1, 2])).toEqual(new Set([2, 1]));
          expect(new Set([{x: 1}])).toEqual(new Set([{x: 1}]));
          expect(new Set([1, 2])).not.toEqual(new Set([1, 3]));
          expect(new Set([1])).not.toEqual(new Set([1, 2]));
          expect(new Set([{x: 1}])).not.toEqual(new Set([{x: 2}]));
        });
        """,
    )
    o = outcome(result, "compares sets")
    assert o.status == "passed", o.message


def test_regexps_compare_by_source_and_flags(ws):
    result = run(
        ws,
        "tests/eqre.test.js",
        """
        it('compares regexps', () => {
          expect(/ab+/gi).toEqual(/ab+/gi);
          expect(/ab+/g).not.toEqual(/ab+/i);
          expect(/ab+/g).not.toEqual(/ac+/g);
        });
        """,
    )
    o = outcome(result, "compares regexps")
    assert o.status == "passed", o.message


def test_dates_and_typed_arrays_compare_by_value(ws):
    result = run(
        ws,
        "tests/eqbin.test.js",
        """
        it('compares dates and typed arrays', () => {
          expect(new Date(5)).toEqual(new Date(5));
          expect(new Date(5)).not.toEqual(new Date(6));
          expect(new Uint8Array([1, 2])).toEqual(new Uint8Array([1, 2]));
          expect(new Uint8Array([1, 2])).not.toEqual(new Uint8Array([1, 3]));
          expect(new Uint8Array([1, 2])).not.toEqual(new Uint8Array([1]));
          expect(new Float64Array([1.5])).not.toEqual(new Float64Array([2.5]));
        });
        """,
    )
    o = outcome(result, "compares dates and typed arrays")
    assert o.status == "passed", o.message


def test_an_unequal_collection_argument_fails_a_call_assertion(ws):
    result = run(
        ws,
        "tests/eqcall.test.js",
        """
        it('sees the difference in a call argument', () => {
          const f = vi.fn();
          f(new Map([['a', 1]]));
          expect(f).toHaveBeenCalledWith(new Map([['a', 1]]));
          expect(f).not.toHaveBeenCalledWith(new Map([['a', 2]]));
        });
        it('names the values it compared', () => {
          expect(new Map([['a', 1]])).toEqual(new Map([['a', 2]]));
        });
        """,
    )
    assert outcome(result, "sees the difference in a call argument").status == "passed"
    failed = outcome(result, "names the values it compared")
    assert failed.status == "failed"
    assert "Map" in failed.message, failed.message
    assert '"a"' in failed.message, failed.message


def test_an_array_of_a_different_length_is_not_equal(ws):
    result = run(
        ws,
        "tests/eqarr.test.js",
        """
        it('compares arrays by length too', () => {
          expect([1]).not.toEqual([1, undefined]);
          expect([1, 2]).toEqual([1, 2]);
        });
        """,
    )
    o = outcome(result, "compares arrays by length too")
    assert o.status == "passed", o.message


# -- errors nothing in the test observed ---------------------------------


def test_a_forgotten_await_fails_the_test_that_started_it(ws):
    result = run(
        ws,
        "tests/forgotten.test.js",
        """
        it('forgets to await', () => {
          Promise.reject(new Error('lost'));
        });
        it('is unaffected', () => { expect(1).toBe(1); });
        """,
    )
    o = outcome(result, "forgets to await")
    assert o.status == "failed", [(x.name, x.status) for x in result.outcomes]
    assert "lost" in o.message
    assert outcome(result, "is unaffected").status == "passed"


def test_an_error_thrown_from_a_timer_fails_the_test_that_scheduled_it(ws):
    result = run(
        ws,
        "tests/timer.test.js",
        """
        it('throws later', () => {
          setTimeout(() => { throw new Error('later'); }, 0);
        });
        """,
    )
    o = outcome(result, "throws later")
    assert o.status == "failed"
    assert "later" in o.message


def test_an_error_with_no_test_running_fails_the_file(ws):
    result = run(
        ws,
        "tests/stray.test.js",
        """
        Promise.reject(new Error('at module scope'));
        it('still runs', () => { expect(1).toBe(1); });
        """,
    )
    assert outcome(result, "still runs").status == "passed"
    unhandled = [o for o in result.outcomes if o.status == "error"]
    assert unhandled, [(x.name, x.status) for x in result.outcomes]
    assert "at module scope" in unhandled[0].message
    assert result.failed


def test_a_path_with_a_space_or_a_hash_is_served(ws):
    """A URL carries a space, a `#` and a non-ASCII character
    percent-encoded, so a lookup against the literal encoded name
    reports a file that is right there as missing."""
    ws.files.fs.write(
        "/workspace/app/my util.js", b"export const add = (a, b) => a + b;\n"
    )
    result = run(
        ws,
        "tests/a spaced ünïcode #1.test.js",
        """
        import { add } from '../app/my util.js';
        it('runs from an encoded path', () => { expect(add(1, 2)).toBe(3); });
        it('names the encoded path in a frame', () => { expect(1).toBe(2); });
        """,
    )
    assert result.collection_error is None, result.collection_error
    o = outcome(result, "runs from an encoded path")
    assert o.status == "passed", o.message
    assert o.file == "tests/a spaced ünïcode #1.test.js"
    failed = outcome(result, "names the encoded path in a frame")
    assert failed.status == "failed"
    assert failed.frames[0].path == "tests/a spaced ünïcode #1.test.js"
    assert "%20" not in failed.traceback


def test_every_teardown_hook_runs_even_after_one_throws(ws):
    """A throwing afterEach used to exit both hook loops, so the outer
    suite's cleanup never ran and the next test saw leaked state."""
    result = run(
        ws,
        "tests/teardown.test.js",
        """
        let closed = 0;
        afterEach(() => { closed += 1; });
        describe('inner', () => {
          afterEach(() => { throw new Error('second teardown'); });
          afterEach(() => { throw new Error('first teardown'); });
          it('one', () => { expect(1).toBe(1); });
        });
        it('outer cleanup still ran', () => { expect(closed).toBe(1); });
        """,
    )
    one = outcome(result, "one")
    assert one.status == "failed"
    assert "first teardown" in one.message
    assert "second teardown" in one.message, one.message
    assert outcome(result, "outer cleanup still ran").status == "passed", outcome(
        result, "outer cleanup still ran"
    ).message


def test_unstubbing_an_absent_global_removes_it_again(ws):
    """Restoration restores existence, not just value: a stub for an
    API the browser does not have must leave `in globalThis` false, or
    a later test's feature check sees one that is not there."""
    result = run(
        ws,
        "tests/absent.test.js",
        """
        it('stubs an API this browser does not have', () => {
          expect('__ntAbsentApi' in globalThis).toBe(false);
          vi.stubGlobal('__ntAbsentApi', () => 'stubbed');
          expect(globalThis.__ntAbsentApi()).toBe('stubbed');
          vi.unstubAllGlobals();
          expect('__ntAbsentApi' in globalThis).toBe(false);
        });
        it('restores a global that did exist', () => {
          const real = globalThis.fetch;
          vi.stubGlobal('fetch', vi.fn());
          expect(globalThis.fetch).not.toBe(real);
          vi.unstubAllGlobals();
          expect(globalThis.fetch).toBe(real);
        });
        """,
    )
    assert [o.status for o in result.outcomes] == ["passed", "passed"], [
        (o.name, o.message) for o in result.outcomes
    ]
