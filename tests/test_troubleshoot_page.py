"""Errors explain their cause with one fix link, and the Troubleshoot page renders safely."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "hydraweave/web/static"


class TroubleshootPageTests(unittest.TestCase):
    def test_manifest_lists_troubleshoot(self):
        manifest = json.loads((STATIC / "pages/manifest.json").read_text(encoding="utf-8"))
        names = manifest if isinstance(manifest, list) else next(v for v in manifest.values() if isinstance(v, list))
        for name in ("troubleshoot.js", "value.js"):
            self.assertIn(name, names)

    def run_node(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explain_error_mapping(self):
        self.run_node(r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const src = fs.readFileSync('hydraweave/web/static/app.js', 'utf8');
const start = src.indexOf('const errorTable');
const end = src.indexOf('\n  }\n', src.indexOf('function explainError')) + 4;
const {explainError, fixHref} = new Function(src.slice(start, end) + '; return {explainError, fixHref};')();
const known = explainError('connect: connection refused');
assert.equal(known.hasDetails, false);
assert.equal(fixHref(known), '#/troubleshoot');
assert.equal(fixHref(explainError('bind: address already in use')), '#/settings');
assert.equal(fixHref(explainError('CUDA out of memory')), '#/models');
assert.equal(fixHref(explainError('GPU slot already held')), '#/turns');
const unknown = explainError('weird <failure>');
assert.equal(unknown.cause, 'Something went wrong');
assert.equal(unknown.hasDetails, true);
assert.equal(unknown.detail, 'weird <failure>');
assert.equal(fixHref({fixAction: 'refresh'}), '#/troubleshoot');
assert.equal(fixHref(null), '#/troubleshoot');
console.log('PASS explainError');
""")

    def test_troubleshoot_render(self):
        self.run_node(r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const H = {esc, machines: () => [], render() {},
  explainError: v => ({cause: 'Something went wrong', fixLabel: 'Troubleshoot', fixAction: 'troubleshoot', detail: String(v), hasDetails: true}),
  fixHref: () => '#/troubleshoot'};
const context = {window: {Hydraweave: H}};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/troubleshoot.js', 'utf8'), context);
const page = context.window.HydraweavePages.troubleshoot;
assert.equal(page.title, 'Troubleshoot');
assert.equal(page.ready(), true);
const html = page.render();
assert(html.includes('Run all checks'));
assert(html.includes('Copy report'));
assert(html.includes('Run checks to see the health'));
console.log('PASS troubleshoot page');
""")


if __name__ == "__main__":
    unittest.main()
