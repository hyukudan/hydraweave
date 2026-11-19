/* Pinned model assets: prepare, inspect a plan, confirm, follow and cancel. */
"use strict";
(() => {
  const H=()=>window.Hydraweave, view={selected:null,path:"",plan:null,busy:false,pending:0};
  const doc=()=>H().state.docs.assets;
  const key=row=>JSON.stringify([row.profile,row.machine,row.asset]);
  const selection=()=>(doc()?.assets || []).find(row=>key(row)===view.selected);
  const esc=v=>H().esc(v ?? "");
  const action=(label,name,disabled="")=>H().button(label,"",{disabled,extra:`data-asset-action="${name}"`});
  async function post(endpoint,args,done) {
    if(view.busy && endpoint!=="cancel") return;
    view.pending++;view.busy=true;H().render();
    try {
      const reply=await H().request(endpoint,{method:"POST",headers:{"Content-Type":"application/json","X-Hydraweave-CSRF":H().csrf() || ""},body:JSON.stringify(args)});
      if(reply.id || reply.job) H().own(reply.id || reply.job);
      if(done) done(reply);
    } catch(error) {H().announce(error.message);}
    finally {view.pending--;view.busy=view.pending>0;H().render();H().refresh();}
  }
  function args() {const row=selection();return row?{profile:row.profile,machine:row.machine,asset:row.asset,path:view.path}:null;}
  function render() {
    const rows=doc()?.assets || [], selected=selection(), blocked=view.busy?"A request is running":"";
    let body=`<p>Choose the asset and destination, prepare its dedicated directory, then review and confirm the pinned download. Registration happens after the node verifies the final file.</p><label for="asset-selection">Profile, node and asset</label><select class="inp" id="asset-selection"><option value="">Choose an asset</option>${rows.map(r=>`<option value="${esc(key(r))}"${key(r)===view.selected?" selected":""}>${esc(r.profile+" · "+(r.machine || "")+" · "+(r.asset || r.error))}</option>`).join("")}</select>`;
    if(selected) {
      body+=`<p>${esc(selected.source?.repo || "")} · ${esc(selected.source?.revision || "")}</p>`;
      if(!selected.downloadable) body+=`<p>${esc(selected.why || selected.error)}</p>`;
      else {
        body+=`<label for="asset-path">Final absolute model path on ${esc(selected.machine)}</label><input class="inp mono" id="asset-path" value="${esc(view.path)}" placeholder="/data/models/model.gguf"><div class="row">${action("Prepare dedicated directory","prepare",blocked)}${action("Review download plan","plan",blocked)}</div><p class="hint">Preparation only creates a marker in an empty directory under a configured model root. It never adopts existing unmarked contents.</p>`;
        if(view.plan) body+=`<h3>Review the download</h3><pre>${esc(JSON.stringify({files:view.plan.files,disk:view.plan.disk,merge:view.plan.merge},null,2))}</pre>${action("Confirm download and register","confirm",blocked || (view.plan.disk?.some(d=>!d.ok)?"Insufficient disk space":""))}`;
      }
    }
    const jobs=(H().state.lastJobs || []).filter(j=>j.action==="assets/download");
    body+=jobs.map(j=>`<p>${esc(j.state)} · ${esc(j.progress || j.error || "")}${j.state==="running"?` <button class="btn" data-asset-cancel="${esc(j.id || j.job)}">Cancel download</button>`:""}</p>`).join("");
    return H().card("Download model files",body)+`<a class="btn" href="#/library">Model library</a>`;
  }
  (window.HydraweavePages ||= {}).assets={title:"Model downloads",group:"Operations",order:41,icon:'<path d="M10 2v11m-4-4l4 4 4-4M3 15v3h14v-3"/>',endpoints:()=>["assets"],ready:()=>!!doc(),render};
  document.addEventListener("change",event=>{
    if(event.target.id!=="asset-selection") return;
    view.selected=event.target.value || null;view.plan=null;
    const selected=selection();
    view.path=selected?.configured?.path || "";H().render();
  });
  document.addEventListener("input",event=>{if(event.target.id==="asset-path") {view.path=event.target.value;view.plan=null;}});
  document.addEventListener("click",event=>{
    const cancel=event.target.closest("[data-asset-cancel]");
    if(cancel) {post("cancel",{job:cancel.dataset.assetCancel});return;}
    const button=event.target.closest("[data-asset-action]");
    if(!button || button.disabled || view.busy || !args()) return;
    if(button.dataset.assetAction==="prepare") post("assets/prepare",args(),()=>H().announce("Download directory prepared"));
    if(button.dataset.assetAction==="plan") {const reviewed=args();post("assets/plan",reviewed,r=>{if(JSON.stringify(args())===JSON.stringify(reviewed))view.plan=r;});}
    if(button.dataset.assetAction==="confirm" && view.plan) post("assets/download",{...args(),confirm:view.plan.confirm},()=>{view.plan=null;H().announce("Download started; registration follows verification");});
  });
})();
