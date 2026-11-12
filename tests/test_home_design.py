"""Home is the setup wizard until setup is done, then a plain overview of nodes and models."""
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window


class HomeDesignTests(unittest.TestCase):
    def test_wizard_and_home_overview(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const now = Date.now();
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {fresh:true, errors:{}, fetched:{setup:now, profiles:now, discovery:now, 'engine-settings':now, topology:now},
 docs:{setup:{configured:true, checklist:[], configuration:{state:'valid'}, nodes:[{id:'node-a',access:'local'}, {id:'node-b',access:'ssh'}]},
 devices:{machines:[{id:'node-a',health:'online'}, {id:'node-b',health:'online'}]},
 'engine-settings':{presets:[{id:'p1',label:'Starter'}]},
 profiles:{profiles:[{id:'p1',label:'Starter',state:'stopped'}]}, topology:{links:[]}}};
let calls = 0;
const H = {state, esc, rows:v => Array.isArray(v) ? v : [], prefs:()=>({refresh_s:2}),
 machines:()=>state.docs.devices.machines, profiles:()=>state.docs.profiles.profiles || [], profileId:p=>p.id,
 pages:{}, mutationReason:()=>'', statusPill:s=>esc(s),
 card:(title,body)=>`<section><h2>${esc(title)}</h2>${body}</section>`,
 button:(label,action,opt={})=>`<button ${opt.extra || ''}>${esc(label)}</button>`,
 request:()=>{calls++;}, mutate:()=>{calls++;}};
const context = {window:{Hydraweave:H}, document:{addEventListener(){}}, Date};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/control-center.js','utf8'),context);
const page = context.window.HydraweavePages['control-center'], render = ()=>page.render();
let html = render();
assert(html.includes('No engine serving'));
assert(!html.includes('Setup · Step'));
for (const text of ['Starter','Available preset','#/engine-settings']) assert(html.includes(text),text);
for (const action of ['cc-add-node','cc-edit-node','cc-trust-node','cc-router']) assert(html.includes(action),action);
assert.deepEqual(Array.from(page.endpoints()),['setup','discovery','engine-settings','topology']);
state.docs.setup.nodes[1].trust = 'paired';
state.docs.setup.profiles = [{id:'p1',machines:['node-b']}];
state.docs.discovery = {nodes:[{id:'found-a',hostname:'<untrusted>',state:'discovered, not adopted'},
 {id:'found-b',hostname:'Worker',machine:'node-b',state:'adopted'}],pairs:[['found-a','found-b']]};
html = render();
for (const text of ['Agent paired','discovered, not adopted','Connection available: found-a + found-b','cc-adopt','cc-provision'])
 assert(html.includes(text),text);
assert(!html.includes('<untrusted>'));
assert(html.includes('&lt;untrusted&gt;'));
state.docs.profiles.profiles[0].state = 'running';
assert(render().includes('1 engine serving'));
state.docs.profiles.profiles = null;
assert(render().includes('Engine status unavailable'));
assert(!render().includes('No engine serving'));
state.docs.profiles.profiles = [{id:'p1',state:'starting'}];
assert(render().includes('Check engine state'));
state.docs['engine-settings'] = {presets:[]};
assert(render().includes('No model presets are available yet.'));
state.docs.setup.checklist = [{id:'model',title:'Model',state:'pending'}];
assert(render().includes('Setup · Step 1 of 4'));
state.docs.setup = {configuration:{state:'missing'}};
html = render();
assert(html.includes('Setup · Step 1 of 4'));
assert(html.includes('cc-initialize'));
state.docs.setup = {configuration:{state:'invalid',error:'<b>Review configuration</b>'}};
html = render();
assert(html.includes('role="alert"'));
assert(html.includes('&lt;b&gt;Review configuration'));
assert.equal(calls,0,'Rendering must not request work or mutate');
console.log('PASS Home wizard, serving status, discovery, presets, escaping, setup controls');
"""
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
