"""Execute real page handlers against the portal shell's actual state shape."""
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window

ROOT = Path(__file__).resolve().parents[1]


class ModelAssetUITests(unittest.TestCase):
    def test_asset_plan_confirmation_stable_selection_and_shell_job_cancel(self):
        self.run_js(r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const handlers={},posts=[],owned=[];let html='',page;
const a={profile:'qwen',machine:'halo-1',asset:'model',downloadable:true,configured:{path:'/models/model.gguf'},source:{repo:'owner/model',revision:'a'.repeat(40)}};
const b={...a,machine:'halo-2'};
const state={docs:{assets:{assets:[a,b]}},lastJobs:[]};
const H={state,esc:v=>String(v??'').replaceAll('<','&lt;'),csrf:()=> 'csrf',card:(title,body)=>title+body,
 button:(label,unused,o)=>`<button ${o.extra}${o.disabled?' disabled':''}>${label}</button>`,own:id=>owned.push(id),announce:()=>{},
 render:()=>{html=page.render();},refresh:async()=>H.render(),request:async(action,options)=>{
 const args=JSON.parse(options.body);posts.push({action,args});assert.equal(options.headers['X-Hydraweave-CSRF'],'csrf');
 if(action==='assets/plan')return {confirm:'abc123abc123',files:[],disk:[{ok:true}],configuration:'frozen'};
 if(action==='assets/download')return {id:'download-1'};
 if(action==='cancel')return {};
 return {prepared:true};
}};
const context={window:{Hydraweave:H},document:{addEventListener:(kind,fn)=>(handlers[kind]||=[]).push(fn)}};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/assets.js','utf8'),context);page=context.window.HydraweavePages.assets;
function fire(kind,target){for(const fn of handlers[kind]||[])fn({target});}
async function click(action){fire('click',{closest:s=>s==='[data-asset-action]'?{dataset:{assetAction:action},disabled:false}:null});await new Promise(setImmediate);}
(async()=>{
fire('change',{id:'asset-selection',value:JSON.stringify(['qwen','halo-1','model'])});
await click('plan');assert.equal(posts[0].args.machine,'halo-1');assert.match(html,/Confirm download/);
state.docs.assets.assets=[b,a];H.render();await click('confirm');
assert.equal(posts[1].action,'assets/download');assert.equal(posts[1].args.machine,'halo-1');
assert.equal(posts[1].args.confirm,'abc123abc123');assert.deepEqual(owned,['download-1']);
state.lastJobs=[{id:'download-1',action:'assets/download',state:'running',progress:'file payload 42 bytes'}];H.render();
assert.match(html,/file payload 42 bytes/);assert.match(html,/data-asset-cancel="download-1"/);
fire('click',{closest:s=>s==='[data-asset-cancel]'?{dataset:{assetCancel:'download-1'}}:null});await new Promise(setImmediate);
assert.deepEqual(posts.at(-1),{action:'cancel',args:{job:'download-1'}});
await click('plan');fire('input',{id:'asset-path',value:'/models/other.gguf'});const count=posts.length;await click('confirm');
assert.equal(posts.length,count,'editing a path invalidates reviewed confirmation');
assert.equal(handlers.click.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_library_uses_owned_jobs_direct_copy_plan_and_live_cancel(self):
        self.run_js(r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const handlers={},posts=[],owned=[];let html='',page;
const root={path:'/models',source:'config',units:[]};
const state={docs:{library:{copy:{available:true},verify_job:{available:true},verify:{},jobs:[],scanning:[],why:{},ledger:{},machines:[
{id:'halo-1',os:'linux',access:'ssh',verify_available:true,copy_destinations:['halo-2'],roots:[root],inventory:{roots:[root]}},
{id:'halo-2',os:'linux',access:'ssh',verify_available:true,copy_destinations:[],roots:[root],inventory:{roots:[root]}}]}},lastJobs:[]};
const H={state,esc:v=>String(v??''),csrf:()=> 'csrf',card:(t,b)=>t+b,table:(t,b)=>b,cells:row=>row.join(''),
tag:v=>String(v),display:v=>String(v??''),unavailable:v=>String(v||''),statusPill:v=>v,kpi:(t,b)=>t+b,
button:(label,unused,o)=>`<button ${o.extra}${o.disabled?' disabled':''}>${label}</button>`,own:id=>owned.push(id),announce:()=>{},
render:()=>{html=page.render();},refresh:async()=>H.render(),request:async(action,options)=>{
const args=JSON.parse(options.body);posts.push({action,args});
if(action==='library/copy-plan')return {confirm:'a'.repeat(16),files:[],bytes:7,transport:'direct SSH'};
return {id:action==='library/copy'?'copy-1':'verify-1'};
}};
const context={window:{Hydraweave:H},document:{addEventListener:(k,f)=>(handlers[k]||=[]).push(f),
getElementById:id=>id==='library-copy-destination'?{value:JSON.stringify(['halo-2','/models'])}:null}};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/library.js','utf8'),context);page=context.window.HydraweavePages.library;
async function click(action,data={}){for(const fn of handlers.click)fn({target:{closest:()=>({disabled:false,dataset:{pageAction:action,...data}})}});await new Promise(setImmediate);}
(async()=>{
await click('library-verify',{machine:'halo-1',root:'/models',name:'model'});
assert.deepEqual(posts[0],{action:'library/verify',args:{machine:'halo-1',model_root:'/models',name:'model'}});
assert.deepEqual(owned,['verify-1']);
await click('library-copy',{machine:'halo-1',root:'/models',name:'model'});await click('library-copy-plan');await click('library-copy-confirm');
assert.equal(posts[2].action,'library/copy');assert.equal(posts[2].args.destination,'halo-2');assert.equal(posts[2].args.confirm,'a'.repeat(16));
state.lastJobs=[{id:'copy-1',action:'library/copy',state:'running',progress:'transferred 7 bytes'}];H.render();
assert.match(html,/transferred 7 bytes/);assert.match(html,/data-job="copy-1"/);
await click('library-cancel',{job:'copy-1'});assert.deepEqual(posts.at(-1),{action:'cancel',args:{job:'copy-1'}});
assert.equal(handlers.click.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def run_js(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
