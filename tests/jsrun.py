"""Run the dashboard's browser scripts under node, against a stub DOM, so their
pure logic can be tested from unittest. Skips when node isn't installed.

    out = run_js(["static/app.js", "static/habits.js"], "PAYLOAD = {...}; out(habitMeasures(w))")

Scripts share one global scope, as they do on the page, so a later snippet can
read and assign their top-level `let`/`const` bindings. The page's own load
(`fetch`) never resolves, so nothing renders until the snippet calls it.
`env` (a dict, merged over os.environ) is for the node process, e.g. {"TZ": "America/Los_Angeles"}.
`out(value)` returns `value` to the test as JSON. Every element read by id or
selector is remembered, so a snippet can check what was rendered:
`out(el('teamHabits').innerHTML)`; every chart made is in `charts_made`.
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PRELUDE = r"""
const vm = require('vm');
const fs = require('fs');
const [files, snippet] = JSON.parse(fs.readFileSync(0, 'utf8'));
function stub() {
  const store = {};
  const fn = function () { return stub(); };
  return new Proxy(fn, {
    get(t, k) {
      if (k in store) return store[k];
      if (k === Symbol.toPrimitive) return () => '';
      if (k === 'then' || typeof k === 'symbol') return undefined;
      if (k === 'querySelectorAll') return () => [];
      if (k === 'querySelector') return () => null;
      return (store[k] = stub());
    },
    set(t, k, v) { store[k] = v; return true; },
  });
}
const elements = {};
const el = key => elements[key] || (elements[key] = stub());
const document = stub();
document.getElementById = el;
document.querySelector = el;
document.querySelectorAll = () => [];
document.createElement = () => stub();
let result;
const charts_made = [];
class Chart {
  constructor(canvas, cfg) { Object.assign(this, cfg); charts_made.push(cfg); }
  update() {} destroy() {}
}
const ctx = vm.createContext({
  document, el, charts_made, Chart, console,
  window: stub(), localStorage: stub(), location: stub(), navigator: stub(),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  fetch: () => new Promise(() => {}),
  setTimeout, clearTimeout, URL, URLSearchParams,
  out: v => { result = v; },
});
for (const f of files) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx, { filename: f });
vm.runInContext(snippet, ctx, { filename: 'snippet.js' });
process.stdout.write(JSON.stringify(result === undefined ? null : result));
"""


def run_js(files, snippet, env=None):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed")
    paths = [os.path.join(REPO, f) for f in files]
    # Node reads and writes UTF-8; without saying so, Python decodes its output
    # in the locale's encoding (cp1252 on Windows) and "—" arrives mangled.
    proc = subprocess.run([node, "-e", _PRELUDE], input=json.dumps([paths, snippet]),
                          capture_output=True, text=True, encoding="utf-8", timeout=60,
                          env=None if env is None else {**os.environ, **env})
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)
