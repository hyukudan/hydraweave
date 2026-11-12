/* DOM-to-HTTP control-center walkthrough. Test-only JSDOM; no GPU or SSH calls.
 * This verifies UI behavior and real configuration writes, not rendering or native browser focus.
 * Run with a supported Node version and a test-only jsdom installation (see tests/ui/README.md).
 */
"use strict";
const {spawn} = require("node:child_process");
const assert = require("node:assert/strict");
const {existsSync} = require("node:fs");
const path = require("node:path");
const {JSDOM, VirtualConsole} = require("jsdom");

async function main() {
const repo = path.resolve(__dirname, "../..");
const venv = path.join(repo, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.HYDRAWEAVE_TEST_PYTHON || (existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3");
const pythonPath = [repo, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
const processServer = spawn(python, [path.join(__dirname, "control_center_fixture.py")], {
  cwd: repo, env: {...process.env, PYTHONPATH: pythonPath}, stdio: ["ignore", "pipe", "pipe"]
});
let stdout="",stderr="",dom;
processServer.stdout.on("data",data=>stdout+=String(data));
processServer.stderr.on("data",data=>stderr+=String(data));
async function until(test,label,ms=20000){
  const start=Date.now();
  while(Date.now()-start<ms){if(await test())return;await new Promise(r=>setTimeout(r,50));}
  throw Error("Timeout: "+label+"\n"+(dom?.window.document.querySelector("main")?.textContent.slice(0,4000)||"")+"\nDialog: "+(dom?.window.document.querySelector("#ops-dialog-error")?.textContent||"")+"\n"+stderr.slice(-1200));
}
const errors=[];
try{
  await until(()=>stdout.includes("URL="),"fixture server");
  const origin=stdout.match(/URL=(.+)/)[1].trim();
  const vc=new VirtualConsole();
  vc.on("jsdomError",e=>errors.push(e.stack||String(e)));
  vc.on("error",(...e)=>errors.push(e.join(" ")));
  dom=await JSDOM.fromURL(origin+"/",{
    resources:"usable",runScripts:"dangerously",pretendToBeVisual:true,virtualConsole:vc,
    beforeParse(w){
      w.scrollTo=()=>{};
      w.fetch=(input,init={})=>{
        const headers=new Headers(init.headers||{});
        if(init.method && init.method!=="GET")headers.set("Origin",origin);
        return fetch(new URL(input,origin),{...init,headers});
      };
      w.Headers=Headers;w.Request=Request;w.Response=Response;
      w.AbortController=AbortController;w.AbortSignal=AbortSignal;
      w.scrollTo=()=>{};
      w.matchMedia=q=>({media:q,matches:false,addEventListener(){},removeEventListener(){},addListener(){},removeListener(){}});
      w.HTMLElement.prototype.scrollIntoView=function(){};
      w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute("open","");};
      w.HTMLDialogElement.prototype.close=function(){this.removeAttribute("open");this.dispatchEvent(new w.Event("close"));};
    }
  });
  const w=dom.window,d=w.document;
  const el=s=>{const value=d.querySelector(s);assert.ok(value,"Missing "+s);return value;};
  const click=s=>{const node=el(s);assert.ok(!node.disabled,"Disabled "+s+": "+node.title);node.click();};
  const fill=(s,value)=>{const node=el(s);node.value=String(value);node.dispatchEvent(new w.Event(node.tagName==="SELECT"?"change":"input",{bubbles:true}));};
  const submit=()=>el("#ops-dialog form").dispatchEvent(new w.Event("submit",{bubbles:true,cancelable:true}));
  const closed=async()=>until(()=>!d.querySelector("#ops-dialog[open]"),"dialog close: "+d.querySelector("#ops-dialog-error")?.textContent);
  const go=async route=>{w.location.hash="#/"+route;await until(()=>d.body.dataset.page===route.split("?")[0] && d.querySelector("main").getAttribute("aria-busy")==="false","route "+route);await w.Hydraweave.refresh();};
  const api=path=>w.Hydraweave.request(path);
  await until(()=>w.Hydraweave && d.querySelector(".cc-wizard"),"first-run wizard");
  const primaryLinks=[...d.querySelectorAll("#navigation .nav-links a")];
  assert.deepEqual(primaryLinks.map(a=>a.textContent.trim()),["Home","Models","Chat","Nodes","Updates","Settings"]);
  click('#navigation [data-shell="advanced"]');
  assert.ok(d.querySelectorAll("#navigation .nav-links a").length>6,"Advanced reveals feature pages");
  for(const group of ["Monitoring","Benchmarks","Lab tools"])assert.ok(d.querySelector(`#navigation .nav-group[data-group="${group}"]`),"Missing Advanced group "+group);
  assert.doesNotMatch(d.body.textContent,/TM-R\d|later phase|Phase \d/);
  assert.equal(d.querySelectorAll(".disabled-control").length,0,"no disabled later placeholders on Home");
  click('#navigation [data-shell="advanced"]');
  for (const [input, cause] of [["unreachable node", "The node cannot be reached"], ["invalid token", "did not accept its access credentials"], ["address already in use", "using this port"], ["missing model file", "required file is missing"], ["out of memory", "not enough memory"], ["GPU slot busy", "GPU slot is being used"], ["engine binary missing", "engine program is missing"], ["Portal data unavailable", "cannot read its settings"]])
    assert.match(w.Hydraweave.explainError(input).cause, new RegExp(cause, "i"));
  const unknownError=w.Hydraweave.explainError("fixture raw detail");
  assert.equal(unknownError.cause,"Something went wrong");assert.equal(unknownError.hasDetails,true);
  assert.equal(d.querySelectorAll("header #global-status").length,1);assert.equal(d.querySelector("#global-status").getAttribute("href"),"#/troubleshoot");
  assert.equal((await api("setup")).configuration.state,"missing");
  assert.equal(d.querySelector("#page-title").textContent,"Home");
  assert.match(d.querySelector("main").textContent,/This computer.*Nodes.*Model.*Start/s);
  assert.doesNotMatch(d.querySelector("main").textContent,/Qwen3\.8|DeepSeek V4\.1|MiniMax M3\.1|GLM-5\.3|RTX PC|USB4 · 2 cables/);
  click('[data-page-action="cc-initialize"]');
  await until(()=>w.Hydraweave.state.docs.setup?.configured===true && w.Hydraweave.state.fresh,"initialized workspace");
  assert.ok(d.querySelector(".cc-wizard"),"wizard remains active while checklist is incomplete");
  click('[data-page-action="cc-wizard-leave"]');
  assert.match(d.querySelector("main").textContent,/Setup: 1 of 4 done/);
  click('[data-page-action="cc-wizard-resume"]');
  click('[data-page-action="cc-wizard-next"]');
  console.log("PASS missing workspace wizard and resumable partial-progress card");
  click('[data-page-action="cc-add-node"]');
  await until(()=>d.querySelector("#ops-dialog[open]") && el("#ops-dialog-title").textContent==="Add node","add node chooser");
  assert.match(el("#ops-dialog").textContent,/Add manually over SSH/);
  click('#ops-dialog [data-page-action="cc-add-manual"]');
  await until(()=>d.querySelector("#cc-node-id"),"node form");
  fill("#cc-node-id","halo-ui");
  fill("#cc-node-address","192.168.44.31");
  fill("#cc-node-ssh","halo-ui");
  fill("#cc-node-root","/home/test/hydraweave");
  assert.equal(el("#cc-node-lab").checked,false);
  assert.ok(el("#cc-node-lab-fields").hidden);
  fill("#cc-node-gpus","gpu0");
  submit();
  await closed();
  await until(()=>w.Hydraweave.state.docs.setup?.nodes.some(n=>n.id==="halo-ui"),"saved node");
  let setup=await api("setup");
  assert.equal(setup.nodes.find(n=>n.id==="halo-ui").trust,"unverified");
  assert.equal(setup.nodes.find(n=>n.id==="halo-ui").lab_mode,false);
  click('[data-page-action="cc-wizard-leave"]');
  if(setup.nodes.some(n=>n.lab_mode===true))assert.match(d.querySelector("main").textContent,/Review GPU slots/);else assert.doesNotMatch(d.querySelector("main").textContent,/GPU slots/);
  assert.match(d.querySelector("main").textContent,/Verify host key/);
  console.log("PASS visual node form → real config write; draft survives polling; SSH remains unverified");
  // Before node tools have answered, identities stay frozen. A new peer alias
  // can still connect two newly declared nodes without editing either identity.
  let nodeConfig = await api("ops/config");
  await w.HydraweaveOpsUI.post("ops/config/save", {
    version: nodeConfig.version, op: "add", machine: {
      id: "halo-peer", os: "linux", access: "ssh", http_address: "192.168.44.32",
      ssh_target: "halo-peer", install_root: "/home/test/peer-hydraweave",
      lab_mode: true, lab_dir: "/home/test/gpu-lab", devices: []
    }
  });
  await w.Hydraweave.refresh({afterWrite:true});
  nodeConfig = await api("ops/config");
  const originalNode = nodeConfig.machines.find(n=>n.id==="halo-ui").entry;
  assert.ok(nodeConfig.machines.find(n=>n.id==="halo-ui").add_peer_aliases);
  click('[data-page-action="cc-edit-node"][data-machine="halo-ui"]');
  await until(()=>d.querySelector("#ops-dialog[open]") &&
    d.querySelector("#ops-dialog-confirm")?.textContent==="Save peer aliases","frozen node peer form");
  assert.equal(el("#ops-dialog-confirm").textContent,"Save peer aliases");
  assert.ok([...d.querySelectorAll("#ops-dialog input,#ops-dialog select,#ops-dialog textarea")]
    .every(input=>input.id==="cc-node-peers" ? !input.disabled : input.disabled));
  fill("#cc-node-peers","halo-peer=peer-second");
  submit();await closed();
  const withPeer = (await api("ops/config")).machines.find(n=>n.id==="halo-ui").entry;
  assert.deepEqual(withPeer,{...originalNode,peer_ssh:{"halo-peer":"peer-second"}});
  click('[data-page-action="cc-edit-node"][data-machine="halo-ui"]');
  await until(()=>d.querySelector("#ops-dialog[open]"),"frozen peer replacement form");
  fill("#cc-node-peers","halo-peer=changed-alias");submit();
  await until(()=>/cannot be changed or removed/.test(el("#ops-dialog-error").textContent),"existing peer alias refusal");
  assert.deepEqual((await api("ops/config")).machines.find(n=>n.id==="halo-ui").entry,withPeer);
  click("#ops-dialog [data-ops-close]");
  console.log("PASS frozen node allows only new peer aliases; replacement is refused and identity is preserved");
  await go("engine-settings");
  click('[data-page-action="es-create"]');
  await until(()=>d.querySelector("#es-new-id"),"profile form");
  fill("#es-new-id","ui-qwen");
  fill("#es-new-preset","qwen-replicas");
  assert.ok(el('input[name="es-create-node"][value="halo-ui"]').checked);
  submit();await closed();
  await until(()=>w.Hydraweave.state.docs["engine-settings"]?.profiles.some(p=>p.id==="ui-qwen"),"saved profile");
  let settings=await api("engine-settings");
  let profile=settings.profiles.find(p=>p.id==="ui-qwen");
  assert.deepEqual(profile.nodes,["halo-ui"]);
  assert.ok(profile.assets.some(a=>a.verification==="missing"));
  console.log("PASS profile creation → assigned Linux SSH node and explicitly missing assets");
  const control=profile.controls.find(c=>c.editable && c.section==="overrides" && c.type==="integer");
  assert.ok(control,"An editable integer control");
  const value=control.min ?? 256;
  const selector='[data-es-profile="ui-qwen"][data-es-key="'+control.section+':'+control.key+'"]';
  fill(selector,value);
  await w.Hydraweave.refresh();
  assert.equal(el(selector).value,String(value));
  assert.match(d.querySelector("main").textContent,/Unsaved changes are kept/);
  click('[data-page-action="es-save"]');
  await until(()=>d.querySelector("#ops-dialog[open]") && /Review ui-qwen/.test(el("#ops-dialog").textContent),"settings review");
  submit();await closed();
  settings=await api("engine-settings");profile=settings.profiles.find(p=>p.id==="ui-qwen");
  assert.equal(profile.configuration.overrides[control.key],value);
  console.log("PASS knob draft survives polling → review → real versioned save");
  fill(selector,"");
  click('[data-page-action="es-save"]');
  await until(()=>d.querySelector("#ops-dialog[open]"),"Auto review");
  submit();await closed();
  profile=(await api("engine-settings")).profiles.find(p=>p.id==="ui-qwen");
  assert.ok(!Object.hasOwn(profile.configuration.overrides||{},control.key));
  console.log("PASS Auto clears the requested override");
  // A separate-tab write invalidates the draft's revision without erasing it.
  fill(selector,value);
  const beforeExternalWrite = await api("setup");
  await w.HydraweaveOpsUI.post("setup/router/save", {
    version: beforeExternalWrite.configuration.version, machine: "local", port: 18990
  });
  await w.Hydraweave.refresh({afterWrite:true});
  assert.equal(el(selector).value,String(value));
  assert.ok(el('[data-page-action="es-save"]').disabled);
  assert.match(d.querySelector("main").textContent,/configuration changed in another tab/);
  click('[data-page-action="es-discard"]');
  await until(()=>/No unsaved changes/.test(d.querySelector("main").textContent),"discard stale draft");
  console.log("PASS external config change preserves draft and requires review before save");
  await go("components");
  await until(()=>d.querySelectorAll(".cmp-card").length===4,"component inventory");
  assert.match(d.querySelector("main").textContent,/Source download location/);
  assert.match(d.querySelector("main").textContent,/Prepare node tools/);
  assert.match(d.querySelector("main").textContent,/No sources downloaded/);
  const components=await api("components");
  assert.equal(components.storage.scope,"controller");
  console.log("PASS four-component catalog → separate staged-source and node-install controls");
  await go("control-center");
  click('[data-page-action="cc-router"]');
  await until(()=>d.querySelector("#cc-router-machine"),"router form");
  fill("#cc-router-machine","halo-ui");fill("#cc-router-port","18991");
  submit();await closed();
  setup=await api("setup");
  assert.equal(setup.router.machine,"halo-ui");
  assert.match(setup.router.listen,/:18991$/);
  assert.equal(setup.router.active,null);
  console.log("PASS saved router placement/port without claiming live activation");
  assert.deepEqual(errors,[],"JavaScript/runtime errors");
  console.log("PASS DOM-to-real-API walkthrough; no hardware access, layout, native focus, or screenshot claims");
} finally {
  if(dom)dom.window.close();
  processServer.kill("SIGTERM");
}


}
main().catch(error => { console.error(error); process.exitCode = 1; });
