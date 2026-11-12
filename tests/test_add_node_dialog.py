"""Add node starts from nodes found on the LAN and adopts them with a pairing code."""
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window


class AddNodeDialogTests(unittest.TestCase):
    def test_discovery_first_add_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {docs:{discovery:{nodes:[{id:'found-a',hostname:'<x>',address:'10.0.0.5',gpu_arch:'gfx1151',state:'discovered, not adopted'},
 {id:'found-b',hostname:'Worker',state:'adopted'}]}}};
let handler, opened, posts = [];
const H = {state, esc, refresh:async()=>{}, announce(){}, render(){}};
const UI = {open:o=>{opened=o;}, post:async(a,args)=>{posts.push([a,args]); return {adopted:args.id};}};
const context = {window:{Hydraweave:H, HydraweaveOpsUI:UI}, document:{addEventListener(t,f){ if (t==='click') handler=f; }}, Date};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/control-center.js','utf8'), context);
const click = (action, data={}) => handler({target:{closest:()=>({dataset:{pageAction:action, ...data}})}});
const form = (id, code) => ({querySelector:q => q.includes('radio') || q.includes('cc-adopt-id') ? (id ? {value:id} : null) : {value:code}});
(async () => {
  await click('cc-add-node');
  assert.equal(opened.title, 'Add node'); assert.equal(opened.confirm, 'Adopt');
  assert(opened.body.includes('value="found-a" checked')); assert(!opened.body.includes('found-b'));
  assert(opened.body.includes('&lt;x&gt;') && !opened.body.includes('<x>'));
  assert(opened.body.includes('cc-add-manual') && opened.body.includes('data-ops-close'));
  await assert.rejects(opened.submit(form('found-a', ' ')), /pairing code/);
  await opened.submit(form('found-a', ' 123456 '));
  assert.equal(JSON.stringify(posts), JSON.stringify([['discovery/adopt', {id:'found-a', code:'123456'}]]));
  state.docs.discovery.nodes[0].state = 'adopted';
  await click('cc-add-node');
  assert.equal(opened.confirm, 'Check again'); assert(opened.body.includes('No new nodes found'));
  assert(opened.body.includes('Add manually over SSH'));
  assert.equal(await opened.submit(form(null, '')), true);
  assert.equal(posts.length, 1);
  console.log('PASS discovery-first add node');
})().catch(e => { console.error(e); process.exit(1); });
"""
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
