"""Home must distinguish verified current GPU turns from stale or incomplete evidence."""
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window


class HomeDesignTests(unittest.TestCase):
    def test_current_turn_and_serving_evidence(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const now = Date.now();
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {fresh:true, errors:{}, fetched:{setup:now, profiles:now, devices:now, topology:now, turns:now},
 docs:{setup:{configured:true, configuration:{state:'valid'}, nodes:[{id:'node-a',access:'local'}, {id:'node-b',access:'ssh'}]},
 devices:{machines:[{id:'node-a',health:'online'}, {id:'node-b',health:'online'}]},
 profiles:{profiles:[{id:'qwen',label:'Qwen3.8',state:'stopped'}]}, turns:{nodes:[]}, topology:{links:[]}}};
let calls = 0;
const H = {state, esc, rows:v => Array.isArray(v) ? v : [], prefs:()=>({refresh_s:2}),
 machines:()=>state.docs.devices.machines, profiles:()=>state.docs.profiles.profiles || [], profileId:p=>p.id,
 pages:{turns:{}}, mutationReason:()=>'', statusPill:s=>esc(s),
 card:(title,body)=>`<section><h2>${esc(title)}</h2>${body}</section>`,
 button:(label,action,opt={})=>`<button ${opt.extra || ''}>${esc(label)}</button>`,
 request:()=>{calls++;}, mutate:()=>{calls++;}};
const context = {window:{Hydraweave:H}, document:{addEventListener(){}}, Date};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/control-center.js','utf8'),context);
const page = context.window.HydraweavePages['control-center'], render = ()=>page.render();
let html = render();
assert(html.includes('No engine serving'));
assert(!html.includes('cc-turn-held'));
for (const text of ['Qwen3.8','DeepSeek V4.1','MiniMax M3.1','GLM-5.3','MiniMax-H3-Image',
 'Needs a preset','USB4','2 cables / 4 streams','Queue is read only']) assert(html.includes(text),text);
for (const action of ['cc-add-node','cc-edit-node','cc-trust-node','cc-router']) assert(html.includes(action),action);
assert(html.includes('#/engines?profile=qwen'));
assert.deepEqual(Array.from(page.endpoints()),['setup','discovery','turns']);
state.docs.setup.nodes[1].trust = 'paired';
state.docs.setup.profiles = [{id:'qwen',machines:['node-b']}];
state.docs.discovery = {nodes:[{id:'found-a',hostname:'<untrusted>',state:'discovered, not adopted'},
 {id:'found-b',hostname:'Worker',machine:'node-b',state:'adopted'}],pairs:[['found-a','found-b']]};
html = render();
for (const text of ['Agent paired','discovered, not adopted','USB4 pair available','cc-adopt','cc-provision'])
 assert(html.includes(text),text);
assert(!html.includes('<untrusted>'));
assert(html.includes('&lt;untrusted&gt;'));
state.docs.profiles.profiles[0].state = 'running';
assert(render().includes('1 engine serving'));
state.docs.profiles.profiles = null;
assert(render().includes('Engine status unavailable'));
assert(!render().includes('No engine serving'));
state.docs.profiles.profiles = [{id:'qwen',state:'starting'}];
assert(render().includes('Check engine state'));
const holder = {parsed:true,alive:true,verified:true,past_cap:false,label:'Qwen3.8'};
const queue = {machine:'node-a',state:'ok',age_s:2,holder};
state.docs.turns.nodes = [queue];
assert.equal((render().match(/class="cc-turn cc-turn-held"/g)||[]).length,1);
queue.holder.label = '<img src=x onerror=alert(1)>';
assert(!render().includes('<img src=x'));
assert(render().includes('&lt;img'));
for (const [key,value] of [['alive',false],['verified',false],['parsed',false],['past_cap',true],
 ['pair_state','lock attempt (unconfirmed)']]) {
 const previous = holder[key]; holder[key] = value;
 assert(!render().includes('cc-turn-held'),key); holder[key] = previous;
}
holder.pair_state = 'running (confirmed by the first node)';
assert(render().includes('cc-turn-held'));
queue.age_s=91; assert(!render().includes('cc-turn-held')); queue.age_s=2;
queue.state='stale'; assert(!render().includes('cc-turn-held')); queue.state='ok';
state.fetched.turns=now-20000; assert(!render().includes('cc-turn-held')); state.fetched.turns=now;
state.errors.turns='unavailable'; assert(!render().includes('cc-turn-held')); delete state.errors.turns;
state.fresh=false; assert(!render().includes('cc-turn-held')); state.fresh=true;
queue.holder=null; assert(render().includes('No GPU turn held'));
state.docs.devices.machines.push({id:'rtxpc',os:'windows',accelerators:[{name:'RTX PRO 6000',excluded:true}]});
assert(!render().includes('#/machine?id=rtxpc'));
state.docs.devices.machines[2].accelerators[0].excluded=false;
assert(render().includes('#/machine?id=rtxpc'));
state.docs.setup={configuration:{state:'missing'}};
assert(render().includes('cc-initialize'));
state.docs.setup={configuration:{state:'invalid',error:'Review configuration'}};
assert(render().includes('role="alert"'));
assert(render().includes('Review configuration'));
assert.equal(calls,0,'Rendering must not request work or mutate a queue');
console.log('PASS Home serving, turn evidence, optional GPU exclusion, escaping, setup controls');
"""
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
