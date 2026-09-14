"""The ``ws-vitest`` harness and its driver: JavaScript unit tests in a
headless browser, against the files the workspace holds.

Requires the ``[apps]`` extra (playwright) plus ``playwright install
chromium`` — the same browser ``test_app`` drives, on the same shared
loop-thread and semaphore, with a context of its own.

Two things make this a driver rather than a ``test_app`` flag.

**The synthetic root has two trees under it.** ``app/`` and ``tests/``
are served as siblings::

    https://nontainer.test/apps/t-unit/            the harness page
    https://nontainer.test/apps/t-unit/__nt/harness.js
    https://nontainer.test/apps/t-unit/app/util.js
    https://nontainer.test/apps/t-unit/tests/util.test.js

so ``import { add } from '../app/util.js'`` resolves from a test file —
the import an agent writes anyway. test_app's base is the *app* root,
where ``tests/`` has no URL at all.

**The run is hermetic.** No api routes come up, nothing outside the
synthetic origin is reachable, and the policy on the wire is stricter
than the app's own: ``connect-src 'self'`` rather than ``'self'
https:``. A unit test that silently reaches the network passes for the
wrong reason and fails in production, so a forgotten stub fails on the
fetch — which is the outcome that teaches. The policy is written here
because ``AppsConfig.csp_extend`` adds sources and never removes them,
so no configuration path tightens the derived one.

Requests are answered by intercepting them (``context.route``) rather
than by a listener: no port to bind, no server to reap, and the same
pattern test_app already proves against this browser. Results come back
through ``page.evaluate``, which goes over CDP and is not subject to the
page's own policy.

The browser lives on the HOST on every rung. On a VM rung the guest
never sees the test file; the workspace filesystem is read here.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..wspytest import TestFrame, TestOutcome
from .dispatch import _content_type
from .testapp import parse_frames

_HOST = "nontainer.test"
_TOKEN = "t-unit"
_PREFIX = f"/apps/{_TOKEN}"
BASE_URL = f"https://{_HOST}{_PREFIX}/"

#: Where the harness module is served from, under the synthetic root.
HARNESS_PATH = "__nt/harness.js"

#: The trees served under the synthetic root, in workspace coordinates.
SERVED_DIRS = ("app", "tests")

#: The subtree no static path may reach — ``app/api/`` is backend source
#: and the app's own static dispatch refuses it too.
API_DIR = "app/api"

#: The policy the harness page is served under. Stricter than the app's
#: on purpose: ``connect-src 'self'`` is what makes a forgotten fetch
#: stub fail here instead of quietly reaching a real host, and
#: ``'unsafe-inline'`` is what the inline import map and the inline
#: module entry need. ``AppsConfig.csp_extend`` appends sources and
#: never removes them, so the derived app policy cannot be narrowed to
#: this; the unit tier writes its own.
UNIT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "base-uri 'none'; "
    "form-action 'none'"
)

#: A request the driver refuses, as JSON — a test that calls
#: ``res.json()`` without checking ``.ok`` reads the reason instead of a
#: second, misleading parse error.
_HERMETIC_BODY = json.dumps(
    {
        "error": (
            "ws-vitest: no api routes are up. A unit test reaches the files "
            "under test and nothing else, so a forgotten stub fails here "
            "rather than passing for the wrong reason. Stub the boundary: "
            "vi.stubFetch({'api/scores': {scores: []}})."
        )
    }
).encode()

_NOT_FOUND_BODY = json.dumps(
    {
        "error": (
            "ws-vitest: not found. The harness serves app/ and tests/ as "
            "siblings under one root, and nothing else."
        )
    }
).encode()

#: Cap on a quoted source line, and on the file a line is quoted from.
_MAX_QUOTED_LINE = 200
_MAX_ANNOTATED_BYTES = 512_000


# ---------------------------------------------------------------------------
# the harness: vitest's surface, as page JavaScript
# ---------------------------------------------------------------------------

HARNESS_JS = r"""/* ws-vitest harness: vitest's shape, supplied by nontainer.
 *
 * Loaded as a module from the synthetic root, assigned to the page's
 * globals, and resolvable as 'vitest' and '@jest/globals' through the
 * page's import map. Not vitest: the matchers and the mock surface
 * named below, and a message naming the replacement for everything
 * else.
 */

const BASE = new URL('.', location.href).pathname;

function refuse(what, instead) {
  throw new Error('ws-vitest: ' + what + ' is not available here — ' + instead);
}

/* ---- formatting values for a message ---- */

function fmt(v, depth) {
  depth = depth || 0;
  if (typeof v === 'string') return JSON.stringify(v);
  if (v === null) return 'null';
  if (v === undefined) return 'undefined';
  if (typeof v === 'bigint') return String(v) + 'n';
  if (typeof v === 'function') return v.name ? '[Function ' + v.name + ']' : '[Function]';
  if (typeof v !== 'object') return String(v);
  if (v instanceof Error) return '[' + (v.name || 'Error') + ': ' + v.message + ']';
  if (v instanceof Date) return v.toISOString();
  if (v instanceof RegExp) return String(v);
  if (depth > 2) return Array.isArray(v) ? '[Array]' : '[Object]';
  if (Array.isArray(v)) {
    return v.length ? '[ ' + v.map((x) => fmt(x, depth + 1)).join(', ') + ' ]' : '[]';
  }
  if (v instanceof Map) {
    const entries = Array.from(v.entries(), (e) =>
      fmt(e[0], depth + 1) + ' => ' + fmt(e[1], depth + 1)
    );
    return 'Map { ' + (entries.join(', ') || '') + ' }';
  }
  if (v instanceof Set) {
    return 'Set { ' + Array.from(v, (x) => fmt(x, depth + 1)).join(', ') + ' }';
  }
  if (ArrayBuffer.isView(v) && !(v instanceof DataView)) {
    return (
      v.constructor.name +
      ' [ ' +
      Array.from(v, (x) => String(x)).join(', ') +
      ' ]'
    );
  }
  const keys = Object.keys(v);
  if (!keys.length) return '{}';
  return '{ ' + keys.map((k) => k + ': ' + fmt(v[k], depth + 1)).join(', ') + ' }';
}

/* ---- equality ---- */

function kind(v) {
  return Object.prototype.toString.call(v);
}

/* Own enumerable keys are all a Map, a Set, a RegExp or a typed array
 * has — which is none — so comparing those by keys alone calls every
 * pair of them equal. Each built-in below is compared by what it
 * actually holds, and only a plain object or an array falls through to
 * its keys. */

function pairsEqual(mine, theirs, strict) {
  // Order-insensitive, and matched on key AND value together so a
  // duplicate deep-equal key cannot be paired with the wrong value.
  const rest = theirs.slice();
  for (const [k, v] of mine) {
    const i = rest.findIndex(
      (entry) => equals(entry[0], k, strict) && equals(entry[1], v, strict)
    );
    if (i < 0) return false;
    rest.splice(i, 1);
  }
  return true;
}

function elementsEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (!Object.is(a[i], b[i])) return false;
  return true;
}

function bytesOf(v) {
  return v instanceof ArrayBuffer
    ? new Uint8Array(v)
    : new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
}

function equals(a, b, strict) {
  if (Object.is(a, b)) return true;
  if (a === null || b === null) return false;
  if (typeof a !== 'object' || typeof b !== 'object') return false;
  const type = kind(a);
  // An Array is never a plain object, a Map is never a Set, and a
  // Uint8Array is never a Float64Array.
  if (type !== kind(b)) return false;
  if (type === '[object Date]') return a.getTime() === b.getTime();
  if (type === '[object RegExp]') {
    return a.source === b.source && a.flags === b.flags;
  }
  if (strict && Object.getPrototypeOf(a) !== Object.getPrototypeOf(b)) return false;
  if (type === '[object Map]') {
    return (
      a.size === b.size &&
      pairsEqual(Array.from(a.entries()), Array.from(b.entries()), strict)
    );
  }
  if (type === '[object Set]') {
    return (
      a.size === b.size &&
      pairsEqual(
        Array.from(a, (v) => [v, v]),
        Array.from(b, (v) => [v, v]),
        strict
      )
    );
  }
  if (type === '[object ArrayBuffer]' || type === '[object DataView]') {
    return elementsEqual(bytesOf(a), bytesOf(b));
  }
  if (ArrayBuffer.isView(a)) return elementsEqual(a, b);
  // An array's length is not an enumerable key, so without this
  // [1] and [1, undefined] would compare equal under toEqual.
  if (Array.isArray(a) && a.length !== b.length) return false;
  const pick = (o) => Object.keys(o).filter((k) => strict || o[k] !== undefined);
  const ka = pick(a);
  const kb = pick(b);
  if (ka.length !== kb.length) return false;
  return ka.every(
    (k) => Object.prototype.hasOwnProperty.call(b, k) && equals(a[k], b[k], strict)
  );
}

/* ---- expect ---- */

function fail(message, expected, actual) {
  const e = new Error(message);
  e.name = 'AssertionError';
  e.expected = expected;
  e.actual = actual;
  throw e;
}

function isMock(f) {
  return typeof f === 'function' && f.mock && Array.isArray(f.mock.calls);
}

function requireMock(f) {
  if (!isMock(f)) {
    fail('expected ' + fmt(f) + ' to be a mock — build it with vi.fn() or vi.spyOn()');
  }
}

function matchers(received, negated) {
  const ok = (pass, phrase, expected) => {
    if (pass === !negated) return;
    fail(
      'expected ' + fmt(received) + ' ' + (negated ? 'not ' : '') + phrase,
      expected,
      received
    );
  };
  const threw = (fn) => {
    if (typeof fn !== 'function') {
      fail('expected ' + fmt(fn) + ' to be a function, so toThrow could call it');
    }
    try {
      fn();
    } catch (e) {
      return e;
    }
    return null;
  };
  const m = {
    toBe(e) {
      ok(Object.is(received, e), 'to be ' + fmt(e) + ' // Object.is equality', e);
    },
    toEqual(e) {
      ok(equals(received, e, false), 'to deeply equal ' + fmt(e), e);
    },
    toStrictEqual(e) {
      ok(equals(received, e, true), 'to strictly equal ' + fmt(e), e);
    },
    toContain(e) {
      const has =
        typeof received === 'string'
          ? received.includes(e)
          : !!received && typeof received.includes === 'function' && received.includes(e);
      ok(has, 'to contain ' + fmt(e), e);
    },
    toHaveLength(n) {
      const len = received === null || received === undefined ? undefined : received.length;
      ok(len === n, 'to have a length of ' + n + ', got ' + fmt(len), n);
    },
    toBeTruthy() {
      ok(!!received, 'to be truthy');
    },
    toBeFalsy() {
      ok(!received, 'to be falsy');
    },
    toBeNull() {
      ok(received === null, 'to be null', null);
    },
    toBeUndefined() {
      ok(received === undefined, 'to be undefined', undefined);
    },
    toBeDefined() {
      ok(received !== undefined, 'to be defined');
    },
    toThrow(expected) {
      const e = threw(received);
      if (expected === undefined) {
        ok(e !== null, 'to throw');
        return;
      }
      const text = e === null ? '' : String((e && e.message) || e);
      const hit =
        e !== null &&
        (expected instanceof RegExp ? expected.test(text) : text.includes(String(expected)));
      ok(hit, 'to throw an error matching ' + fmt(expected) + ', got ' + fmt(e), expected);
    },
    toMatch(expected) {
      const text = String(received);
      const hit =
        expected instanceof RegExp ? expected.test(text) : text.includes(String(expected));
      ok(hit, 'to match ' + fmt(expected), expected);
    },
    toBeGreaterThan(n) {
      ok(received > n, 'to be greater than ' + fmt(n), n);
    },
    toBeGreaterThanOrEqual(n) {
      ok(received >= n, 'to be greater than or equal to ' + fmt(n), n);
    },
    toBeLessThan(n) {
      ok(received < n, 'to be less than ' + fmt(n), n);
    },
    toBeLessThanOrEqual(n) {
      ok(received <= n, 'to be less than or equal to ' + fmt(n), n);
    },
    toBeCloseTo(n, digits) {
      const places = digits === undefined ? 2 : digits;
      const delta = Math.abs(received - n);
      ok(delta < Math.pow(10, -places) / 2, 'to be close to ' + fmt(n), n);
    },
    toHaveBeenCalled() {
      requireMock(received);
      ok(received.mock.calls.length > 0, 'to have been called');
    },
    toHaveBeenCalledTimes(n) {
      requireMock(received);
      ok(
        received.mock.calls.length === n,
        'to have been called ' + n + ' time(s), got ' + received.mock.calls.length,
        n
      );
    },
    toHaveBeenCalledWith() {
      requireMock(received);
      const want = Array.prototype.slice.call(arguments);
      const hit = received.mock.calls.some((call) => equals(call, want, false));
      ok(
        hit,
        'to have been called with ' + fmt(want) + ', got ' + fmt(received.mock.calls),
        want
      );
    },
    toMatchSnapshot() {
      refuse(
        'toMatchSnapshot',
        'there is no snapshot plane here — assert the value itself with toEqual'
      );
    },
    toMatchInlineSnapshot() {
      refuse(
        'toMatchInlineSnapshot',
        'there is no snapshot plane here — assert the value itself with toEqual'
      );
    },
    toMatchFileSnapshot() {
      refuse(
        'toMatchFileSnapshot',
        'there is no snapshot plane here — assert the value itself with toEqual'
      );
    },
  };
  Object.defineProperty(m, 'not', { get: () => matchers(received, !negated) });
  Object.defineProperty(m, 'resolves', {
    get: () => settled(received, false, negated),
  });
  Object.defineProperty(m, 'rejects', { get: () => settled(received, true, negated) });
  return m;
}

const MATCHER_NAMES = Object.keys(matchers(undefined, false));

function settled(promise, wantReject, negated) {
  const facet = {};
  for (const name of MATCHER_NAMES) {
    facet[name] = async function () {
      const args = Array.prototype.slice.call(arguments);
      let value;
      let rejected = false;
      try {
        value = await promise;
      } catch (e) {
        rejected = true;
        value = e;
      }
      if (wantReject && !rejected) {
        fail('expected promise to reject, but it resolved with ' + fmt(value));
      }
      if (!wantReject && rejected) {
        fail('expected promise to resolve, but it rejected with ' + fmt(value));
      }
      const target = wantReject && name === 'toThrow' ? () => { throw value; } : value;
      return matchers(target, negated)[name].apply(null, args);
    };
  }
  Object.defineProperty(facet, 'not', {
    get: () => settled(promise, wantReject, !negated),
  });
  return facet;
}

function expect(received) {
  return matchers(received, false);
}

expect.extend = () =>
  refuse(
    'expect.extend',
    'the matcher set is fixed here — assert the condition directly, or write a ' +
      'helper function your test calls'
  );

/* ---- vi ---- */

const spies = [];
const stubbedGlobals = [];

function viFn(impl) {
  const f = function () {
    const args = Array.prototype.slice.call(arguments);
    f.mock.calls.push(args);
    try {
      const value = f._impl ? f._impl.apply(this, args) : undefined;
      f.mock.results.push({ type: 'return', value: value });
      return value;
    } catch (e) {
      f.mock.results.push({ type: 'throw', value: e });
      throw e;
    }
  };
  f._impl = impl;
  f.mock = { calls: [], results: [] };
  f.mockImplementation = (i) => ((f._impl = i), f);
  f.mockReturnValue = (v) => ((f._impl = () => v), f);
  f.mockResolvedValue = (v) => ((f._impl = () => Promise.resolve(v)), f);
  f.mockRejectedValue = (v) => ((f._impl = () => Promise.reject(v)), f);
  f.mockClear = () => ((f.mock.calls = []), (f.mock.results = []), f);
  f.mockReset = () => ((f._impl = undefined), f.mockClear());
  return f;
}

function spyOn(obj, key) {
  const original = obj ? obj[key] : undefined;
  if (typeof original !== 'function') {
    throw new Error(
      'ws-vitest: vi.spyOn(obj, ' + JSON.stringify(key) + ') — that property is ' +
        'not a function (' + fmt(original) + ')'
    );
  }
  const spy = viFn(function () {
    return original.apply(obj, Array.prototype.slice.call(arguments));
  });
  spy.mockRestore = () => {
    obj[key] = original;
    return spy;
  };
  try {
    obj[key] = spy;
  } catch (e) {
    /* fall through to the check below, which says what to do instead */
  }
  if (obj[key] !== spy) {
    throw new Error(
      "ws-vitest: vi.spyOn could not replace '" + key + "' — a module's exports " +
        'are read-only in a browser. Spy on an object your own code owns, pass ' +
        'the dependency in as an argument, or stub the fetch with vi.stubFetch.'
    );
  }
  spies.push(spy);
  return spy;
}

function stubGlobal(name, value) {
  // Whether the property EXISTED, not just what it held. A stub for an
  // API this browser does not have is the common case — restoring it as
  // `undefined` would leave `'IntersectionObserver' in globalThis` true,
  // and the next test's feature check would reach for one that is not
  // there.
  stubbedGlobals.push([
    name,
    Object.prototype.hasOwnProperty.call(globalThis, name),
    globalThis[name],
  ]);
  globalThis[name] = value;
  return vi;
}

function unstubAllGlobals() {
  while (stubbedGlobals.length) {
    const entry = stubbedGlobals.pop();
    if (entry[1]) globalThis[entry[0]] = entry[2];
    else delete globalThis[entry[0]];
  }
  return vi;
}

function route(p) {
  let s = String(p).split('?')[0].split('#')[0];
  if (s.startsWith(BASE)) s = s.slice(BASE.length);
  return s.replace(/^\/+/, '');
}

function stubFetch(routes) {
  const table = new Map();
  for (const key of Object.keys(routes || {})) table.set(route(key), routes[key]);
  const stub = viFn(async (input, init) => {
    const raw = typeof input === 'string' ? input : input && input.url;
    const key = route(new URL(raw, location.href).pathname);
    if (!table.has(key)) {
      throw new TypeError(
        'ws-vitest: fetch(' + JSON.stringify(String(raw)) + ') has no stub — ' +
          'vi.stubFetch keys this run: ' +
          (Array.from(table.keys()).join(', ') || '(none)')
      );
    }
    const spec = table.get(key);
    if (spec instanceof Response) return spec;
    return new Response(JSON.stringify(spec === undefined ? null : spec), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
  stubGlobal('fetch', stub);
  return stub;
}

const vi = {
  fn: viFn,
  spyOn: spyOn,
  stubGlobal: stubGlobal,
  unstubAllGlobals: unstubAllGlobals,
  stubFetch: stubFetch,
  clearAllMocks: () => (spies.forEach((s) => s.mockClear()), vi),
  resetAllMocks: () => (spies.forEach((s) => s.mockReset()), vi),
  restoreAllMocks: () => (
    spies.splice(0).forEach((s) => s.mockRestore && s.mockRestore()),
    unstubAllGlobals()
  ),
  mock: () =>
    refuse(
      'vi.mock',
      'module mocking needs a loader hook a browser does not have — pass the ' +
        'dependency in as an argument, vi.spyOn an object your code owns, or ' +
        'stub the boundary with vi.stubFetch({"api/x": {...}})'
    ),
  doMock: () => vi.mock(),
  unmock: () => vi.mock(),
  importActual: () => vi.mock(),
  importMock: () => vi.mock(),
};

/* ---- collection ---- */

const suites = [];
const beforeFrames = [[]];
const afterFrames = [[]];
const tests = [];

function describe(name, fn) {
  suites.push(String(name));
  beforeFrames.push([]);
  afterFrames.push([]);
  try {
    fn();
  } finally {
    suites.pop();
    beforeFrames.pop();
    afterFrames.pop();
  }
}

function it(name, fn) {
  tests.push({
    name: suites.concat([String(name)]).join(' > '),
    fn: fn,
    // The live frames, not a flattened copy: a beforeEach declared
    // BELOW an it() in the same block still applies to it, because the
    // whole block is collected before anything runs.
    before: beforeFrames.slice(),
    after: afterFrames.slice(),
  });
}

function beforeEach(fn) {
  beforeFrames[beforeFrames.length - 1].push(fn);
}

function afterEach(fn) {
  afterFrames[afterFrames.length - 1].push(fn);
}

for (const entry of [[describe, 'describe'], [it, 'it']]) {
  for (const word of ['skip', 'only', 'todo', 'each', 'concurrent', 'runIf', 'skipIf']) {
    entry[0][word] = () =>
      refuse(
        entry[1] + '.' + word,
        'choose what runs from the command line: a path filter, or -t NAME'
      );
  }
}

function beforeAll() {
  refuse(
    'beforeAll',
    'each test file is one page of its own, so build shared state at module ' +
      'scope, or set it up in beforeEach'
  );
}

function afterAll() {
  refuse(
    'afterAll',
    'each test file is one page of its own, so nothing outlives it to tear down'
  );
}

/* ---- the run ---- */

function serialize(e) {
  if (!(e instanceof Error)) {
    return { name: 'Error', message: 'thrown value: ' + fmt(e), stack: '' };
  }
  return {
    name: e.name || 'Error',
    message: e.message === undefined ? String(e) : e.message,
    stack: e.stack || '',
    expected: 'expected' in e ? fmt(e.expected) : null,
    actual: 'actual' in e ? fmt(e.actual) : null,
  };
}

/* A test that starts async work and neither returns nor awaits it —
 * the forgotten `await` — throws where no `try` can see it, and a
 * timer that throws does too. Left alone both are recorded as a pass,
 * which is the one outcome a test framework must never invent. So the
 * page listens, and whatever fires is charged to the test that was
 * running; what fires with none running is the file's. */

let running = null;
const stray = [];

function observed(value) {
  const record = serialize(value);
  if (running) {
    if (!running.error) running.error = record;
  } else if (stray.length < 20) {
    stray.push(record);
  }
}

addEventListener('unhandledrejection', (e) => observed(e.reason));
addEventListener('error', (e) => observed(e.error || e.message));

/* Two task boundaries, which is what it takes. An unhandled rejection
 * is reported only after the task the promise was rejected in has
 * ended and nothing has handled it — one boundary later than a timer
 * that throws. Measured in Chromium: with one turn a forgotten `await`
 * is charged to whatever ran next; with two it is charged to the test
 * that started it. */
function drain() {
  return new Promise((resolve) => setTimeout(() => setTimeout(resolve, 0), 0));
}

export async function __run(load, options) {
  const opts = options || {};
  let loadError = null;
  try {
    await load();
  } catch (e) {
    loadError = serialize(e);
  }
  // Before the first test, so a module-scope failure is the file's
  // rather than the first test's.
  await drain();
  if (loadError) {
    return { results: [], collected: 0, error: loadError, stray: stray };
  }

  const wanted = tests.filter((t) => !opts.name || t.name.includes(opts.name));
  const results = [];
  for (const t of wanted) {
    const started = performance.now();
    const state = { error: null };
    running = state;
    try {
      for (const frame of t.before) for (const hook of frame) await hook();
      // Called through a comma expression, not as t.fn(): a method call
      // would put the harness's own property name ("Object.fn") into
      // every stack frame the agent reads.
      await (0, t.fn)();
    } catch (e) {
      if (!state.error) state.error = serialize(e);
    }
    // Every teardown runs. Letting one throw out of both loops would
    // skip the rest of the file's cleanup — the outer suite's included —
    // and the leaked state would surface as a later test failing for a
    // reason nothing in it explains. The first error is the failure;
    // the rest are named beneath it.
    const teardown = [];
    for (const frame of t.after.slice().reverse()) {
      for (const hook of frame.slice().reverse()) {
        try {
          await hook();
        } catch (e) {
          teardown.push(serialize(e));
        }
      }
    }
    if (teardown.length) {
      if (!state.error) state.error = teardown.shift();
      if (teardown.length) state.error.also = teardown;
    }
    await drain();
    running = null;
    results.push({
      name: t.name,
      ok: state.error === null,
      ms: performance.now() - started,
      error: state.error,
    });
  }
  return { results: results, collected: tests.length, error: null, stray: stray };
}

Object.assign(globalThis, {
  describe,
  it,
  test: it,
  beforeEach,
  afterEach,
  beforeAll,
  afterAll,
  expect,
  vi,
  jest: vi,
});

export { describe, it, it as test, beforeEach, afterEach, beforeAll, afterAll };
export { expect, vi, vi as jest };
"""

#: The harness page. One inline import map (so ``from 'vitest'`` and
#: ``from '@jest/globals'`` both resolve to the harness) and one inline
#: module entry that imports the test file and parks the run's promise
#: on ``window``. Both are inline, which is what ``'unsafe-inline'`` in
#: the unit policy is for.
PAGE_HTML = """<!doctype html>
<meta charset="utf-8">
<title>ws-vitest</title>
<script type="importmap">
{"imports": {
  "vitest": "./__nt/harness.js",
  "vitest/globals": "./__nt/harness.js",
  "@jest/globals": "./__nt/harness.js"
}}
</script>
<body>
<script type="module">
import { __run } from './__nt/harness.js';
window.__nt_done = __run(() => import('./__SPEC__'), __OPTS__);
</script>
</body>
"""


def page_html(rel: str, options: dict[str, Any]) -> bytes:
    """The harness page for one test file: the import map, and the entry
    that imports it. ``rel`` is workspace-relative (``tests/x.test.js``),
    which is also its path under the synthetic root."""
    spec = "/".join(quote(part, safe="") for part in rel.split("/"))
    return (
        PAGE_HTML.replace("__SPEC__", spec)
        .replace("__OPTS__", json.dumps(options))
        .encode()
    )


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileResult:
    """One test file's run. ``collection_error`` is a file that threw
    before any test could run — an import that failed, a bad specifier,
    a ``vi.mock`` at module scope — which is the JavaScript spelling of
    the thing pytest calls a collection error."""

    file: str
    outcomes: tuple[TestOutcome, ...] = ()
    duration: float = 0.0
    collected: int = 0
    """Tests the file defined, before ``-t`` narrowed them."""
    console: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    """What the page was refused: a Content-Security-Policy violation,
    or a request the driver aborted."""
    collection_error: str | None = None
    load_error: str | None = None
    """The harness page itself did not come up — a browser problem, not
    the agent's code."""
    frames: tuple[TestFrame, ...] = ()
    """The collection error's own frames, where it had any."""

    @property
    def failed(self) -> bool:
        return (
            self.collection_error is not None
            or self.load_error is not None
            or any(o.status != "passed" for o in self.outcomes)
        )


# ---------------------------------------------------------------------------
# stack frames, in workspace coordinates
# ---------------------------------------------------------------------------


class _Sources:
    """Test and module files, read once each, for the lines a report
    shows. Resolved against the WORKSPACE root: the harness serves
    ``app/`` and ``tests/`` as siblings, so a frame in a test file has no
    meaning under the app root."""

    def __init__(self, ws: Any):
        self._ws = ws
        self._lines: dict[str, list[str]] = {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            text = ""
            try:
                path = _abs(self._ws, rel)
                fs = self._ws.files.fs
                if fs.exists(path) and fs.isfile(path):
                    data = fs.read(path)
                    if len(data) <= _MAX_ANNOTATED_BYTES:
                        text = data.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — a diagnostic never breaks a run
                text = ""
            self._lines[rel] = text.splitlines()
        return self._lines[rel]

    def line(self, rel: str, no: int) -> str:
        lines = self.lines(rel)
        if not 0 < no <= len(lines):
            return ""
        text = lines[no - 1]
        return text[:_MAX_QUOTED_LINE] if len(text) > _MAX_QUOTED_LINE else text


def _abs(ws: Any, rel: str) -> str:
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    return f"{root}/{rel.lstrip('/')}"


def workspace_frames(stack: str, sources: "_Sources") -> list[TestFrame]:
    """A V8 stack as frames in workspace coordinates.

    A frame is kept when it names a file the agent wrote — something
    under ``app/`` or ``tests/`` on the synthetic origin. The harness
    module and the page's own inline entry are plumbing, and a report
    that shows them teaches the agent to read past it.
    """
    out: list[TestFrame] = []
    for frame in parse_frames(stack or ""):
        url = frame.url
        if not url.startswith(BASE_URL):
            continue
        rel = unquote(url[len(BASE_URL) :].split("?", 1)[0].split("#", 1)[0])
        if not any(rel == d or rel.startswith(d + "/") for d in SERVED_DIRS):
            continue
        out.append(
            TestFrame(
                path=rel,
                line=frame.line,
                function=frame.fn or "",
                source=((frame.line, sources.line(rel, frame.line)),),
                column=frame.col,
            )
        )
    return out


def render_frames(frames: list[TestFrame] | tuple[TestFrame, ...]) -> str:
    """A failure's frames in vitest's stack shape: the innermost first,
    each naming ``file:line:column`` with the line it happened on."""
    out: list[str] = []
    for frame in frames:
        where = f"{frame.path}:{frame.line}:{frame.column}"
        out.append(f" ❯ {where}" + (f" {frame.function}" if frame.function else ""))
        text = frame.source[-1][1] if frame.source else ""
        if text.strip():
            out.append(f"   {frame.line}| {text.rstrip()}")
    return "\n".join(out)


def _message(error: dict) -> str:
    """One page-side error as a report message, in workspace
    coordinates: the synthetic origin is an implementation detail of the
    harness, and a message naming it sends the agent looking for a host
    that does not exist.

    ``also`` carries the errors a failure stood in front of — the
    teardown hooks that ran after the one that failed and threw too.
    They are named under it rather than dropped, since each is its own
    repair."""
    name = error.get("name") or "Error"
    text = error.get("message")
    text = "" if text is None else str(text)
    head = f"{name}: {text}".rstrip(": ").replace(BASE_URL, "")
    return "\n".join([head, *(f"also: {_message(e)}" for e in error.get("also") or ())])


def _unhandled(rel: str, error: dict, sources: "_Sources") -> TestOutcome:
    """An error no test was running to be charged with. It is the
    file's, not a test's — naming it after whichever test happened to be
    next would send the repair to the wrong place."""
    frames = workspace_frames(error.get("stack") or "", sources)
    message = _message(error)
    return TestOutcome(
        name=f"{rel} > (unhandled error)",
        file=rel,
        line=frames[0].line if frames else None,
        status="error",
        message=message,
        traceback=render_frames(frames) or message,
        frames=tuple(frames),
    )


def _outcome(rel: str, record: dict, sources: "_Sources") -> TestOutcome:
    """One page-side result as a report outcome."""
    name = f"{rel} > {record['name']}"
    duration = float(record.get("ms") or 0.0) / 1000.0
    error = record.get("error")
    if not error:
        return TestOutcome(
            name=name, file=rel, line=None, status="passed", duration=duration
        )
    frames = workspace_frames(error.get("stack") or "", sources)
    message = _message(error)
    return TestOutcome(
        name=name,
        file=rel,
        line=frames[0].line if frames else None,
        status="failed",
        duration=duration,
        message=message,
        traceback=render_frames(frames) or None,
        frames=tuple(frames),
    )


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------


def _served(ws: Any, rel: str) -> tuple[bytes, str] | None:
    """The bytes for a path under the synthetic root, or ``None`` when
    nothing there is served. ``app/api/`` is refused with everything
    else: it is backend source, and the app's own static dispatch
    refuses that subtree too.

    Reads the workspace WITHOUT taking its lock, and must: this runs on
    a worker thread of the browser's loop, and the run already holds the
    lock on the thread that started it (``run_js_tests``). Acquiring an
    RLock held by a different thread would simply hang."""
    clean = posixpath.normpath(rel)
    if clean in (".", "") or clean.startswith(".."):
        return None
    if clean == API_DIR or clean.startswith(API_DIR + "/"):
        return None
    if not any(clean == d or clean.startswith(d + "/") for d in SERVED_DIRS):
        return None
    path = _abs(ws, clean)
    fs = ws.files.fs
    if not fs.exists(path) or not fs.isfile(path):
        return None
    return fs.read(path), _content_type(clean)


async def _run_file(
    browser: Any,
    sema: "asyncio.Semaphore",
    ws: Any,
    rel: str,
    *,
    name: str | None,
    load_timeout_ms: int,
    run_timeout_ms: int,
) -> FileResult:
    """Run one test file on a fresh context of the shared browser."""
    loop = asyncio.get_running_loop()
    console: dict[str, int] = {}
    refused: dict[str, None] = {}
    misses: list[str] = []
    page_errors: list[dict] = []
    started = time.perf_counter()

    def _page_error(e: Any) -> None:
        if len(page_errors) < 20:
            page_errors.append(
                {
                    "name": getattr(e, "name", "") or "Error",
                    "message": getattr(e, "message", None) or str(e),
                    "stack": getattr(e, "stack", "") or "",
                }
            )

    def _console(message: Any) -> None:
        line = f"[{message.type}] {message.text}"
        if line in console:
            console[line] += 1
        elif len(console) < 50:
            console[line] = 1

    async def route_handler(route: Any, request: Any) -> None:
        parts = urlsplit(request.url)
        if parts.netloc != _HOST:
            # Hermetic: a unit test has no business off this origin, and
            # the policy already refused it — this is the second wall.
            if len(refused) < 20:
                refused.setdefault(
                    f"{request.url} -> blocked ({request.resource_type}): a "
                    "ws-vitest run reaches the files under test and nothing "
                    "else. Stub the boundary with vi.stubFetch."
                )
            await route.abort()
            return
        if parts.path != _PREFIX and not parts.path.startswith(_PREFIX + "/"):
            # An absolute path leaves the synthetic root — including the
            # /api/* an app's own frontend would call.
            await route.fulfill(
                status=404, body=_HERMETIC_BODY, content_type="application/json"
            )
            return
        # Percent-DECODED before anything looks it up: a space, a `#`
        # or a non-ASCII character rides the wire encoded, and matching
        # the encoded spelling against a filename reports a file that is
        # right there as missing. `_served` normalizes and confines what
        # comes out, so a decoded separator cannot escape the two trees.
        rest = unquote(parts.path[len(_PREFIX) + 1 :])
        if rest in ("", "index.html"):
            await route.fulfill(
                status=200,
                body=page_html(rel, {"name": name} if name else {}),
                content_type="text/html; charset=utf-8",
                headers={"content-security-policy": UNIT_CSP},
            )
            return
        if rest == HARNESS_PATH:
            await route.fulfill(
                status=200,
                body=HARNESS_JS.encode(),
                content_type="text/javascript; charset=utf-8",
            )
            return
        if rest == "api" or rest.startswith("api/"):
            await route.fulfill(
                status=404, body=_HERMETIC_BODY, content_type="application/json"
            )
            return
        found = await loop.run_in_executor(None, _served, ws, rest)
        if found is None:
            # Remembered, not just answered: a module import that 404s
            # surfaces in the page as "Failed to fetch dynamically
            # imported module" and names the IMPORTER, never the file
            # that was missing.
            if rest not in misses and len(misses) < 20:
                misses.append(rest)
            await route.fulfill(
                status=404, body=_NOT_FOUND_BODY, content_type="application/json"
            )
            return
        body, content_type = found
        await route.fulfill(status=200, body=body, content_type=content_type)

    async with sema:
        context = await browser.new_context()
        try:
            await context.add_init_script(
                "document.addEventListener('securitypolicyviolation', e => {"
                "  (window.__nt_csp = window.__nt_csp || []).push("
                "    [e.effectiveDirective || e.violatedDirective,"
                "     e.blockedURI || '(inline)']);"
                "});"
            )
            page = await context.new_page()
            page.on("console", _console)
            page.on("pageerror", _page_error)
            await context.route("**/*", route_handler)
            try:
                await page.goto(BASE_URL, timeout=load_timeout_ms)
            except Exception as e:
                return FileResult(
                    file=rel,
                    duration=time.perf_counter() - started,
                    console=tuple(console),
                    load_error=str(e),
                )
            try:
                # page.evaluate goes over CDP, so it is not subject to the
                # page's own policy; the deadline bounds the WHOLE await,
                # since a test that never settles would otherwise hang the
                # verb with no output.
                payload = await asyncio.wait_for(
                    page.evaluate(_AWAIT_RUN), run_timeout_ms / 1000
                )
            except (TimeoutError, asyncio.TimeoutError):
                payload = {
                    "results": [],
                    "collected": 0,
                    "error": {
                        "name": "Error",
                        "message": (
                            f"the run did not finish within {run_timeout_ms}ms — a "
                            "test returned a promise that never settled"
                        ),
                        "stack": "",
                    },
                }
            except Exception as e:
                payload = {
                    "results": [],
                    "collected": 0,
                    "error": {"name": "Error", "message": str(e), "stack": ""},
                }
            for directive, blocked in await _violations(page):
                if len(refused) < 20:
                    refused.setdefault(
                        f"{blocked} -> blocked by the ws-vitest policy "
                        f"({directive}). A unit run reaches the files under "
                        "test and nothing else."
                    )
            sources = _Sources(ws)
            error = payload.get("error")
            if error:
                frames = workspace_frames(error.get("stack") or "", sources)
                message = _message(error)
                if misses and "dynamically imported module" in message:
                    message += " — nothing is served at " + ", ".join(misses)
                return FileResult(
                    file=rel,
                    duration=time.perf_counter() - started,
                    console=_lines(console),
                    violations=tuple(refused),
                    collection_error=message,
                    frames=tuple(frames),
                )
            outcomes = [_outcome(rel, record, sources) for record in payload["results"]]
            # What no test was running to be charged with: a module-scope
            # rejection, a timer that outlived the test that set it.
            # Playwright's own pageerror is the backstop for anything the
            # page's listeners could not attribute, and is used only when
            # the harness accounted for nothing — otherwise the same
            # failure would be reported twice.
            unattributed = list(payload.get("stray") or ())
            if not unattributed and not any(o.status != "passed" for o in outcomes):
                unattributed = page_errors
            outcomes.extend(_unhandled(rel, record, sources) for record in unattributed)
            return FileResult(
                file=rel,
                outcomes=tuple(outcomes),
                duration=time.perf_counter() - started,
                collected=int(payload.get("collected") or 0),
                console=_lines(console),
                violations=tuple(refused),
            )
        finally:
            await context.close()


#: Await the harness's promise, giving the module entry a moment to park
#: it. A page whose harness never evaluated (a served file that would not
#: parse) has no promise at all, and says so rather than hanging.
_AWAIT_RUN = """async () => {
  for (let i = 0; i < 200 && !window.__nt_done; i++) {
    await new Promise((r) => setTimeout(r, 10));
  }
  if (!window.__nt_done) {
    return {results: [], collected: 0, error: {
      name: 'Error',
      message: 'the harness page did not start (nothing evaluated the entry module)',
      stack: '',
    }};
  }
  return await window.__nt_done;
}"""


async def _violations(page: Any) -> list[tuple[str, str]]:
    try:
        return list(await page.evaluate("window.__nt_csp || []") or [])
    except Exception:  # noqa: BLE001 — a diagnostic never breaks a run
        return []


def _lines(console: dict[str, int]) -> tuple[str, ...]:
    return tuple(line if n == 1 else f"{line} (x{n})" for line, n in console.items())


def require_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "ws-vitest requires the apps extra: pip install nontainer[apps] "
            "&& playwright install chromium"
        ) from e


def run_js_tests(
    ws: Any,
    files: Any,
    *,
    name: str | None = None,
    bail: int = 0,
    load_timeout_ms: int = 15_000,
    run_timeout_ms: int = 30_000,
) -> list[FileResult]:
    """Run each test file in the headless browser and report what
    happened, one :class:`FileResult` per file.

    ``name`` is the ``-t`` filter, applied in the page where the test
    names are. ``bail`` stops once that many files have failed — the
    files already run are returned, and the rest are not started; 0 is
    no limit.
    """
    from .browser import submit_job

    require_playwright()
    out: list[FileResult] = []
    failed = 0
    # The workspace's single-writer lock, held for the whole run on THIS
    # thread: the page is served from the tree as it stands, so nothing
    # may rewrite a module between the import that loads it and the test
    # that asserts on it. Held here rather than taken per read inside the
    # route handler, which runs on the browser loop's worker threads —
    # an RLock is re-entrant for its owner and a deadlock for anyone
    # else, and the verb is typed inside an already-locked terminal call.
    with ws.lock:
        for rel in files:
            try:
                result = submit_job(
                    lambda browser, sema, rel=rel: _run_file(
                        browser,
                        sema,
                        ws,
                        rel,
                        name=name,
                        load_timeout_ms=load_timeout_ms,
                        run_timeout_ms=run_timeout_ms,
                    )
                ).result()
            except Exception as e:  # noqa: BLE001 — a browser failure is a result
                result = FileResult(
                    file=rel, load_error=f"Playwright/Chromium unavailable: {e}"
                )
            out.append(result)
            if result.failed:
                failed += 1
                if bail and failed >= bail:
                    break
    return out
