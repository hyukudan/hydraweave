"""Exercise the served setup/profile JavaScript without requiring a browser or nodes."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave import engine_settings
from hydraweave.winproc import no_window


ROOT = Path(__file__).resolve().parents[1]


class SetupUIWorkflowTests(unittest.TestCase):
    def test_model_selection_reaches_profile_creation_with_a_paired_agent(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        presets = [engine_settings.preset_document(name)
                   for name in ("ds41-pair", "qwen-starter")]
        script = "const presets = " + json.dumps(presets) + r""";
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const handlers = {}, storage = new Map([
  ['hydraweave-setup-step', '2'], ['hydraweave-setup-model', 'old-selection']
]);
const localStorage = {getItem:key=>storage.get(key) ?? null, setItem:(key,value)=>storage.set(key,value)};
const worker = {id:'paired-worker',os:'linux',access:'agent',trust:'paired',selectable:true};
const setup = {configured:true,configuration:{state:'valid'},nodes:[worker],profiles:[],
  checklist:[{id:'profile',title:'Choose a model and its nodes',state:'next',href:'#/engine-settings'},
    {id:'components',title:'Prepare engine',state:'pending',href:'#/components'},
    {id:'launch',title:'Start the model',state:'pending',href:'#/engines'}]};
const settings = {version:'revision',presets,profiles:[],machines:[
  {id:'controller',os:'linux',access:'local',selectable:false}, worker,
  {id:'excluded',os:'linux',access:'agent',selectable:false}
]};
const live = [], posts = [], notices = [];
let dialog;
const H = {esc,state:{docs:{setup,'engine-settings':settings},errors:{},fetched:{},fresh:false},
  profiles:()=>live,machines:()=>[],rows:value=>Array.isArray(value)?value:[],
  mutationReason:()=>'',card:(title,body)=>`<section><h2>${esc(title)}</h2>${body}</section>`,
  button:(label,action,options={})=>`<button ${options.extra || ''}${options.disabled?' disabled':''}>${esc(label)}</button>`,
  statusPill:esc,tag:esc,empty:(title,body,extra='')=>title+body+extra,
  request:async path=>{assert.equal(path,'engine-settings');return settings;},
  render:()=>{},announce:message=>notices.push(message),prefs:()=>({refresh_s:5})};
const document = {
  addEventListener:(type,handler)=>(handlers[type] ||= []).push(handler),
  querySelector:()=>null,
  getElementById:id=>{assert.equal(id,'es-new-id');return {value:'starter-on-agent'};},
  querySelectorAll:selector=>{assert.equal(selector,'input[name="es-create-node"]:checked');return [{value:worker.id}];}
};
const UI = {open:value=>{dialog=value;},post:async(path,args)=>{
  posts.push([path,JSON.parse(JSON.stringify(args))]);
  return path==='engine-settings/validate'?{valid:true}:{saved:true};
}};
const context = {window:{Hydraweave:H,HydraweaveOpsUI:UI},document,localStorage,
  location:{hash:'#/control-center',origin:'http://portal.example:8800'},URLSearchParams};
async function click(pageAction,extra={}) {
  const button = {dataset:{pageAction,...extra},disabled:false,isConnected:true};
  const event = {target:{closest:selector=>pageAction.startsWith(selector.includes('cc-')?'cc-':'es-')?button:null}};
  for(const handler of handlers.click || []) await handler(event);
}
async function main() {
  vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/control-center.js','utf8'),context);
  const home = context.window.HydraweavePages['control-center'];
  assert.equal(presets[1].qualified,false,'starter has no speed qualification flag');
  let html = home.render();
  assert.match(html,/<button[^>]*data-page-action="cc-wizard-model"[^>]*data-model="qwen-starter"[^>]*>/);
  assert.doesNotMatch(html,/<button[^>]*data-model="qwen-starter"[^>]*disabled/);
  await click('cc-wizard-model',{model:'qwen-starter'});
  assert.equal(context.location.hash,'#/engine-settings?preset=qwen-starter');
  assert.equal(storage.get('hydraweave-setup-model'),'old-selection','selection is not claimed as a saved profile');
  assert.equal(posts.length,0);
  html = home.render();
  assert.match(html,/href="#\/engine-settings"/);
  assert.match(html,/href="#\/components"/);
  assert.match(html,/href="#\/engines"/);
  assert.doesNotMatch(html,/cc-copy-api|portal\.example.*\/v1|Open Chat/);
  await click('cc-wizard-leave');
  assert.match(home.render(),/Setup: 2 of 4 done/,'a stale localStorage model is not counted');
  setup.profiles.push({id:'saved-profile',state:'configured'});
  assert.match(home.render(),/Setup: 3 of 4 done/,'saved configuration is counted');
  live.push({id:'saved-profile',state:'running'});
  assert.match(home.render(),/Setup: 4 of 4 done/,'running evidence comes from live profiles');

  vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/engine-settings.js','utf8'),context);
  await click('es-create');
  assert.ok(dialog,'paired agents can reach the profile form');
  assert.match(dialog.body,/<option value="qwen-starter" selected>/);
  assert.match(dialog.body,/name="es-create-node" value="paired-worker" checked/);
  assert.doesNotMatch(dialog.body,/value="controller"|value="excluded"/);
  await dialog.submit();
  assert.deepEqual(posts,[
    ['engine-settings/validate',{version:'revision',id:'starter-on-agent',preset:'qwen-starter',nodes:['paired-worker'],op:'create'}],
    ['engine-settings/create',{version:'revision',id:'starter-on-agent',preset:'qwen-starter',nodes:['paired-worker']}]
  ]);
  assert.equal(context.location.hash,'#/engine-settings?profile=starter-on-agent');
  assert.match(notices.at(-1),/created/);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
