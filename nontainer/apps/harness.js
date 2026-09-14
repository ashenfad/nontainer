/* ws-vitest harness: vitest's shape, supplied by nontainer.
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
