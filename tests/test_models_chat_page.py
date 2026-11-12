"""Models page reaches Chat: the chat page renders safely and shows API snippets."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "hydraweave/web/static/pages"


class ModelsChatPageTests(unittest.TestCase):
    def test_manifest_keeps_pages_and_adds_chat(self):
        manifest = json.loads((PAGES / "manifest.json").read_text(encoding="utf-8"))
        names = manifest if isinstance(manifest, list) else next(v for v in manifest.values() if isinstance(v, list))
        for name in ("chat.js", "value.js"):
            self.assertIn(name, names)

    def test_chat_render(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let list = [];
const H = {esc, profiles:()=>list, profileId:p=>p.id,
 empty:(title,body,extra)=>`<div class="empty"><h2>${esc(title)}</h2><p>${esc(body)}</p>${extra||''}</div>`};
const context = {window:{Hydraweave:H}, location:{hash:'#/chat'}, URLSearchParams};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/chat.js','utf8'), context);
const page = context.window.HydraweavePages.chat;
assert.equal(page.title, 'Chat');
let html = page.render();
assert(html.includes('No model is running'));
assert(html.includes('#/models'));
list = [{id:'stopped', state:'stopped'},
 {id:'m1', label:'<b>One</b>', state:'running', processes:[{endpoint:'http://127.0.0.1:9000/'}]},
 {id:'m2', label:'Two', state:'running', processes:[{endpoint:'http://127.0.0.1:9001'}]}];
html = page.render();
assert(!html.includes('value="stopped"'));
assert(html.includes('&lt;b&gt;One&lt;/b&gt;') && !html.includes('<b>One</b>'));
assert(html.includes('<option value="m1" selected>'));
assert(html.includes('http://127.0.0.1:9000/v1/chat/completions'));
assert(html.includes('base_url=&quot;http://127.0.0.1:9000/v1&quot;'));
assert(html.includes('&quot;model&quot;:&quot;m1&quot;'));
assert(html.includes('id="chat-stop" disabled'));
context.location.hash = '#/chat?model=m2';
html = page.render();
assert(html.includes('<option value="m2" selected>'));
assert(html.includes('http://127.0.0.1:9001/v1/chat/completions'));
console.log('PASS chat page');
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
