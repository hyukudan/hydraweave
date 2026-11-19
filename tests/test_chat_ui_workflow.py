"""Actual Chat JS: same-origin jobs, polling, draft retention and cancellation without duplicate handlers."""
from pathlib import Path
import shutil
import subprocess
import unittest

from hydraweave.winproc import no_window


ROOT = Path(__file__).resolve().parents[1]


class ChatUIWorkflowTests(unittest.TestCase):
    def test_chat_jobs_poll_and_cancel_without_browser_credentials_or_duplicate_submissions(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const posts = [], owned = [], handlers = {};
let html = '', page, pendingAcceptance, count = 0;
const input = {id:'chat-message',value:''};
const main = {addEventListener:(type,handler)=>(handlers[type] ||= []).push(handler),
  querySelector:selector=>selector==='#chat-message'?input:null};
const state = {docs:{chat:{profiles:[{id:'qwen',endpoint:'http://router.example:18600'}]}},lastJobs:[]};
const H = {esc,state,profiles:()=>[{id:'qwen',state:'running',processes:[{name:'engine',endpoint:'http://engine.example:18501'}]}],
  profileId:p=>p.id,empty:(title,body)=>title+body,csrf:()=> 'portal-csrf',announce:()=>{},
  own:id=>owned.push(id),render:()=>{html=page.render();page.after(main);},
  refresh:async()=>H.render(),request:async(action,options)=>{
    posts.push({action,headers:options.headers,args:JSON.parse(options.body)});
    assert.equal(options.method,'POST');
    assert.equal(options.headers['X-Hydraweave-CSRF'],'portal-csrf');
    assert.equal(options.headers.Authorization,undefined,'router keys never enter the browser');
    if(action==='chat/send') {
      count++;
      if(count===2) return new Promise(resolve=>pendingAcceptance=resolve);
      if(count===3) throw Error('conversation exceeds its limit');
      return {id:'job-1',state:'running',progress:'starting'};
    }
    assert.equal(action,'cancel');
    state.lastJobs=[{id:'job-2',state:'cancelled',error:'cancelled by user'}];
    return {state:'succeeded'};
  }};
const context = {window:{Hydraweave:H},location:{hash:'#/chat?model=qwen'},URLSearchParams,
  fetch:()=>{throw Error('Chat must never fetch a router from the browser');},navigator:{}};
vm.runInNewContext(fs.readFileSync('hydraweave/web/static/pages/chat.js','utf8'),context);
page=context.window.HydraweavePages.chat;
function type(value) {input.value=value;for(const handler of handlers.input)handler({target:input});}
async function click(id) {const target={id,disabled:false};for(const handler of handlers.click)await handler({target:{closest:()=>target}});}
async function mainTest() {
  H.render();H.render();H.render();
  for(const kind of ['click','input','change']) assert.equal(handlers[kind].length,1,'one '+kind+' handler after rerenders');
  type('first <message>');H.render();H.render();
  assert.match(html,/first &lt;message&gt;/,'draft survives polling');
  await click('chat-send');
  await click('chat-send');
  assert.equal(posts.length,1,'a second click while running cannot submit another generation');
  assert.deepEqual(owned,['job-1']);
  assert.deepEqual(posts[0].args,{profile:'qwen',messages:[{role:'user',content:'first <message>'}],max_tokens:512});
  assert.match(html,/id="chat-send" disabled/);
  assert.doesNotMatch(html,/id="chat-stop" disabled/);
  state.lastJobs=[{id:'job-1',state:'running',progress:'Generating a response'}];H.render();
  assert.match(html,/Generating a response/);
  state.lastJobs=[{id:'job-1',state:'succeeded',result:{profile:'qwen',message:{role:'assistant',content:'answer <safe>'}}}];
  H.render();H.render();
  assert.equal((html.match(/answer &lt;safe&gt;/g)||[]).length,1,'completed response appears exactly once');
  assert.doesNotMatch(html,/answer <safe>|engine\.example/);
  assert.match(html,/id="chat-stop" disabled/);
  assert.doesNotMatch(html,/id="chat-send" disabled/);

  type('second message');
  const sending=click('chat-send');
  await Promise.resolve();
  assert.equal(count,2);
  await click('chat-stop');
  assert.match(html,/Cancelling/);
  pendingAcceptance({id:'job-2',state:'running',progress:'starting'});
  await sending;
  assert.deepEqual(owned,['job-1','job-2']);
  assert.deepEqual(posts.map(p=>p.action),['chat/send','chat/send','cancel']);
  assert.deepEqual(posts[2].args,{job:'job-2'});
  assert.deepEqual(posts[1].args.messages,[{role:'user',content:'first <message>'},
    {role:'assistant',content:'answer <safe>'},{role:'user',content:'second message'}]);
  assert.match(html,/Request cancelled/);
  assert.match(html,/id="chat-stop" disabled/);
  assert.doesNotMatch(html,/id="chat-send" disabled/);

  type('retryable draft');await click('chat-send');
  assert.match(html,/conversation exceeds its limit/);
  assert.match(html,/retryable draft<\/textarea>/,'refused submission keeps its draft');
  assert.doesNotMatch(html,/id="chat-send" disabled/);
  assert.equal(handlers.click.length,1);
}
mainTest().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=15, **no_window())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
