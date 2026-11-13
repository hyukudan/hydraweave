"""Nodes trend tiles and Metrics history in Node (no browser): a headline is a CURRENT value or says there is none,
a repeated deep link selects its series again, and the Nodes charts are bound after every render."""
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const listeners = {}, documentListeners = {};
const state = {docs: {}, errors: {}};
const H = {state, esc, info: () => '', refresh() {}, card: (t, b) => b, empty: (t, b) => t + b, display: (v, why) => esc(why)};
const stored = {};
const context = {window: {Hydraweave: H}, location: {hash: '#/devices'}, URLSearchParams, Intl,
  localStorage: {getItem: k => stored[k] ?? null, setItem: (k, v) => { stored[k] = v; }},
  document: {addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); }, querySelector: () => null},
  addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); }};
const load = name => vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/' + name, 'utf8'), context);
const go = hash => { context.location.hash = hash; for (const fn of listeners.hashchange || []) fn(); };
load('timeseries.js');
const TS = context.window.HydraweaveTS;
"""

HEADLINES = r"""
load('node-tiles.js');
const tiles = context.window.HydraweaveNodeTiles, END = 1_800_000_000, START = END - 1800, G = 2 ** 30;
const doc = lines => ({lines, step: 30, tier: 'raw', interval_s: 30, start: START, end: END});
const line = (machine, extra, points) => ({labels: {machine, ...extra}, points: points.map(([t, v]) => [t, v, null, null, null, null, null, null, 30])});
const put = (name, lines) => { state.docs[TS.endpoint(name, {range: 1800})] = doc(lines); };
const tile = (html, id) => html.split('data-nt-tile="').find(part => part.startsWith(id + '"'));
const headline = part => part.split('nt-val">')[1].split('<div class="nt-chart">')[0];

// One node stopped reporting 28 minutes ago at 100 %; the other reports 0 % now.
put('node_gpu_busy_percent', [line('n1', {device: 'card0'}, [[END - 1680, 100]]), line('n2', {device: 'card0'}, [[END - 60, 0], [END - 30, 0]])]);
put('node_gpu_temp_celsius', [line('n1', {sensor: 'edge'}, [[END - 1680, 91]]), line('n2', {sensor: 'edge'}, [[END - 30, 40]])]);
put('node_gpu_mem_used_bytes', [line('n1', {domain: 'vram'}, [[END - 1680, 8 * G]]), line('n2', {domain: 'vram'}, [[END - 30, G]])]);
put('node_gpu_mem_total_bytes', [line('n1', {domain: 'vram'}, [[END - 1680, 16 * G]]), line('n2', {domain: 'vram'}, [[END - 30, 4 * G]])]);
put('node_mem_total_bytes', [line('n1', {}, [[END - 1680, 64 * G]]), line('n2', {}, [[END - 30, 32 * G]])]);
put('node_mem_available_bytes', [line('n1', {}, [[END - 1680, G]]), line('n2', {}, [[END - 30, 16 * G]])]);
let html = tiles.render();
let busy = headline(tile(html, 'busy'));
assert(busy.startsWith('0 %'), busy);
assert(!busy.includes('100') && !busy.includes('highest now'), busy);
assert(busy.includes('no current sample: n1 · card0'), busy);
const temperature = headline(tile(html, 'temperature'));
assert(temperature.startsWith('40 °C') && !temperature.includes('91') && temperature.includes('no current sample: n1'), temperature);
const gpu = headline(tile(html, 'gpu-memory'));
assert(gpu.includes(TS.fmt(G, 'bytes') + ' / ' + TS.fmt(4 * G, 'bytes')) && gpu.includes('no current sample: n1'), gpu);
const memory = headline(tile(html, 'memory'));
assert(memory.includes(TS.fmt(16 * G, 'bytes') + ' / ' + TS.fmt(32 * G, 'bytes')) && memory.includes('no current sample: n1'), memory);
// The old samples stay on the chart as history.
assert(tile(html, 'busy').includes('n1 · card0') && tile(html, 'busy').includes('<svg'));

// A stopped sampler: every line ends well before the range end. No headline value at all; the chart remains.
put('node_gpu_busy_percent', [line('n1', {device: 'card0'}, [[END - 600, 100]]), line('n2', {device: 'card0'}, [[END - 600, 55]])]);
html = tiles.render();
busy = headline(tile(html, 'busy'));
assert(busy.includes('no current sample') && !/\d+ %/.test(busy.split('<small>')[0]), busy);
assert(tile(html, 'busy').includes('<svg'));

// Both current: the highest of the current values, and nothing is called missing.
put('node_gpu_busy_percent', [line('n1', {device: 'card0'}, [[END - 30, 70]]), line('n2', {device: 'card0'}, [[END - 60, 20]])]);
busy = headline(tile(tiles.render(), 'busy'));
assert(busy.startsWith('70 %') && busy.includes('highest now') && !busy.includes('no current sample'), busy);
// The cutoff follows the cadence recorded with the sample: three spacings, not a fixed age.
assert.equal(tiles.current([[END - 90, 5, null, 30]], {step: 30, end: END}), 5);
assert.equal(tiles.current([[END - 91, 5, null, 30]], {step: 30, end: END}), undefined);
assert.equal(tiles.current([[END - 400, 5, null, 300]], {step: 30, end: END}), 5);
"""

DEEP_LINK = r"""
state.docs.history = {series: ['node_cpu_busy_percent', 'node_gpu_mem_used_bytes']};
load('history.js');
const page = context.window.HydraweavePages.history, selected = () => page.endpoints()[1];
const GPU = TS.endpoint('node_gpu_mem_used_bytes', {range: 3600}), CPU = TS.endpoint('node_cpu_busy_percent', {range: 3600});
const pick = value => { for (const fn of documentListeners.change) fn({target: {dataset: {pageAction: 'history-series'}, value}}); };
go('#/history?series=node_gpu_mem_used_bytes');
assert.equal(selected(), GPU);
// The picker rules for the rest of this visit, although the link is still in the address.
pick('node_cpu_busy_percent');
assert.equal(selected(), CPU);
assert.equal(selected(), CPU);
// Leave, and come back through the same link: it selects its series again.
go('#/devices');
go('#/history?series=node_gpu_mem_used_bytes');
assert.equal(selected(), GPU);
// Leaving and returning without a link keeps the last choice.
pick('node_cpu_busy_percent');
go('#/devices');
go('#/history');
assert.equal(selected(), CPU);
"""


def run(script):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node is unavailable")
    result = subprocess.run([node, "-e", PRELUDE + script], cwd=ROOT, text=True, capture_output=True, timeout=30,
                            encoding="utf-8", **no_window())
    assert result.returncode == 0, result.stderr or result.stdout


class NodeTilesTests(unittest.TestCase):
    def test_headline_is_current_or_says_so(self):
        run(HEADLINES)

    def test_repeated_deep_link_selects_its_series_again(self):
        run(DEEP_LINK)

    def test_nodes_charts_are_bound_after_every_render(self):
        source = (ROOT / "hydraweave/web/static/app.js").read_text(encoding="utf-8")
        rendered = source.index('$("main").innerHTML = html;')
        after = source.index("if (feature?.after)", rendered)
        bound = source.index('window.HydraweaveNodeTiles?.bind?.($("main"))', rendered)
        self.assertLess(after, bound)
        self.assertLess(bound, source.index("deepLink(page);", after))
        self.assertIn('if (page === "devices")', source[after:bound])


if __name__ == "__main__":
    unittest.main()
