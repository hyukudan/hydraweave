"""Run the actual node-update page handler with concurrent admitted jobs."""
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window


class NodeUpdateUITests(unittest.TestCase):
    def test_per_node_updates_renew_show_progress_and_cancel_without_global_slot(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const handlers={},posts=[],owned=[],notices=[];let html='',page,releaseFirst;
const state={fresh:true,errors:{},fetched:{},job:null,lastJobs:[],docs:{
 setup:{configured:true,configuration:{state:'valid'},nodes:[],profiles:[],checklist:[]},
 discovery:{nodes:['node1','node2'].map(machine=>({id:machine,machine,state:'adopted',version_mismatch:true}))},
 'engine-settings':{presets:[]}
}};
const reply=machine=>({id:'update-'+machine,action:'discovery/update',state:'running',arguments:{machine},progress:'staging',renew:'POST jobs/renew'});
const H={state,esc:v=>String(v??'').replaceAll('<','&lt;'),csrf:()=> 'csrf',card:(title,body)=>title+body,
 button:(label,unused,o)=>`<button ${o.extra}${o.disabled?' disabled':''}>${label}</button>`,
 profiles:()=>[],machines:()=>[],rows:v=>Array.isArray(v)?v:[],prefs:()=>({refresh_s:5}),statusPill:v=>v,
 mutationReason:()=>state.job?'Another action is running':'',own:id=>owned.push(id),announce:text=>notices.push(text),
 mutate:()=>assert.fail('node updates must not reserve the shell global job'),
 render:()=>{html=page.render();},refresh:async()=>H.render(),request:async(endpoint,options)=>{
 const args=JSON.parse(options.body);posts.push({endpoint,args});assert.equal(options.headers['X-Hydraweave-CSRF'],'csrf');
 if(endpoint==='cancel')return {id:'cancel-request'};
 assert.equal(endpoint,'discovery/update');assert.equal(args.origin,'http://portal.example:18700');
 if(args.machine==='node1')return new Promise(resolve=>{releaseFirst=()=>resolve(reply('node1'));});
 return reply(args.machine);
}};
const context={window:{Hydraweave:H,location:{origin:'http://portal.example:18700'}},
 document:{addEventListener:(kind,fn)=>(handlers[kind]||=[]).push(fn),querySelector:()=>null},
 localStorage:{getItem:()=>null,setItem:()=>{}},location:{origin:'http://portal.example:18700'}};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/control-center.js','utf8'),context);
page=context.window.HydraweavePages['control-center'];
async function click(action,data={}){
 const button={dataset:{pageAction:action,...data},disabled:false,isConnected:true};
 for(const fn of handlers.click||[])await fn({target:{closest:()=>button}});
}
function updateButton(machine){return html.match(new RegExp('<button[^>]*data-page-action="cc-update-node"[^>]*data-machine="'+machine+'"[^>]*>'))?.[0];}
(async()=>{
 H.render();assert.doesNotMatch(updateButton('node1'),/disabled/);
 const first=click('cc-update-node',{machine:'node1'});await Promise.resolve();
 assert.match(updateButton('node1'),/disabled/);assert.doesNotMatch(updateButton('node2'),/disabled/);
 await click('cc-update-node',{machine:'node1'});assert.equal(posts.length,1,'same-node request blocked before ACK');
 await click('cc-update-node',{machine:'node2'});assert.equal(posts.length,2,'another node starts while first request awaits ACK');
 assert.deepEqual(owned,['update-node2']);assert.equal(state.job,null);
 releaseFirst();await first;assert.deepEqual(owned,['update-node2','update-node1']);
 assert.match(updateButton('node1'),/disabled/);assert.match(updateButton('node2'),/disabled/);
 state.lastJobs=[{...reply('node1'),progress:'authenticated health check'}, {...reply('node2'),state:'succeeded',progress:'finished'}];
 H.render();assert.match(html,/authenticated health check/);assert.match(html,/data-job="update-node1"/);
 assert.match(html,/node completes its activation or rollback independently/);
 assert.match(updateButton('node1'),/disabled/);assert.doesNotMatch(updateButton('node2'),/disabled/);
 await click('cc-cancel-update',{job:'update-node1'});
 assert.deepEqual(posts.at(-1),{endpoint:'cancel',args:{job:'update-node1'}});
 assert.equal(state.job,null);assert.equal(owned.length,2,'cancel request is not treated as an owned update');
 state.job={action:'up',state:'running'};H.render();assert.match(updateButton('node2'),/disabled/);
 const count=posts.length;await click('cc-update-node',{machine:'node2'});assert.equal(posts.length,count,'existing core action guard retained');
 assert.equal(handlers.click.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, "-e", script], cwd=root, capture_output=True, text=True,
                                encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
