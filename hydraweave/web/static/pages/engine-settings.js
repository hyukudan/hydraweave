/* Revision-aware profile editor backed by qualified, pinned engine controls. */
"use strict";
(() => {
  const H=()=>window.Hydraweave, UI=()=>window.HydraweaveOpsUI;
  const esc=value=>H().esc(value ?? "");
  const doc=()=>H().state.docs["engine-settings"] || {};
  const drafts=new Map();
  let selected="",notice="",creation=null;
  const profile=()=> {
    const query=new URLSearchParams(location.hash.split("?")[1] || "").get("profile");
    const list=doc().profiles || [];
    return list.find(p=>p.id === (query || selected)) || list[0] || null;
  };
  const act=(label,name,disabled="",primary=false,data={})=>H().button(label,"",{style:primary?"pri":"",disabled,
    extra:`data-page-action="es-${name}" ${Object.entries(data).map(([k,v])=>`data-${k}="${esc(v)}"`).join(" ")}`});
  const keyOf=c=>`${c.section}:${c.key}`;
  function changed(p) { return drafts.get(p.id); }
  function controlValue(p,c) {
    const draft=changed(p), key=keyOf(c);
    return draft && Object.hasOwn(draft.values,key) ? draft.values[key] : c.value == null ? "" : String(c.value);
  }
  function editor(p,c,index) {
    const value=controlValue(p,c), id=`es-control-${index}`, lock=p.blocked || (c.editable === false ? c.why || "Fixed by this release" : "");
    const props=`id="${id}" data-es-profile="${esc(p.id)}" data-es-key="${esc(keyOf(c))}" ${lock ? `disabled title="${esc(lock)}"` : ""} aria-describedby="${id}-help"`;
    const input=c.type === "select" ? `<select class="inp" ${props}><option value="" ${value === "" ? "selected" : ""}>Auto · ${c.default == null ? "engine default" : esc(c.default)}</option>${(c.choices || []).map(o=>`<option value="${esc(o.value)}" ${value === String(o.value) ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>` : `<input class="inp" type="number" step="1" ${props} ${c.min == null ? "" : `min="${c.min}"`} ${c.max == null ? "" : `max="${c.max}"`} value="${esc(value)}" placeholder="${esc(c.default == null ? "Auto · engine default" : `Auto · ${c.default}`)}">`;
    const effective=c.effective == null ? "Engine default" : String(c.effective);
    return `<div class="es-control"><label class="field" for="${id}"><span class="es-control-label">${esc(c.label)}${c.editable === false ? `<span class="tag">Fixed by release</span>` : ""}</span>${input}</label><p class="hint" id="${id}-help">${esc(c.description || c.why || "")}</p><div class="es-provenance"><span>Saved effective value: <strong>${esc(effective)}</strong></span><span>Source: ${esc(c.source || "not reported")}</span></div>${c.requires && Object.keys(c.requires).length ? `<p class="hint">Requires ${esc(Object.entries(c.requires).map(([k,v])=>`${k}: ${v.join(", ")}`).join("; "))}</p>` : ""}${c.conflicts && Object.keys(c.conflicts).length ? `<p class="hint">Conflicts with ${esc(Object.entries(c.conflicts).map(([k,v])=>`${k}: ${v.join(", ")}`).join("; "))}</p>` : ""}</div>`;
  }
  function assets(p) {
    const all=p.assets || [], missing=all.filter(a=>!a.configured).length;
    return H().card("Model files",`<p class="hint">Register the file identity and its absolute path on each node. Declared metadata is checked by the installer and engine.</p>${missing ? `<p class="cc-notice">${missing} required file${missing === 1 ? "" : "s"} still need paths and verified metadata.</p>` : ""}<div class="es-assets">${all.map(a=>`<div class="es-asset"><div><strong>${esc(a.key)}</strong><span class="tag">${esc(a.machine)}</span><p class="mono">${esc(a.value?.path || "No file assigned")}</p></div><div class="row">${H().statusPill(a.configured ? "declared" : "missing")}${act(a.configured?"Edit":"Assign file","asset",p.blocked || H().mutationReason(),false,{machine:a.machine,key:a.key,profile:p.id})}</div></div>`).join("")}</div><a class="btn" href="#/library">Model library</a>`,`${all.length-missing} of ${all.length} files declared`);
  }
  function preview(p) {
    const view=p.preview || {};
    const error=view.error || (view.errors || []).map(e=>e.message).join("; ");
    return `<details class="card es-preview"><summary>Launch preview & capability evidence</summary><p class="hint">This is the saved launch configuration. It is not a report from a running engine.</p>${error ? `<p class="cc-notice">${esc(error)}</p>` : `<p class="hint">${view.rendered === false ? "Final launch commands are completed at startup using the installed release and managed API keys." : "The pinned renderer can produce the launch commands. Installation and readiness checks still run at launch."}</p>`}${(view.generated_at_launch || []).map(item => `<p class="hint">${esc(typeof item === "string" ? item : item.message)}</p>`).join("")}${(view.processes || []).map(proc=>`<details><summary>${esc(proc.machine_id || proc.machine || "")} · ${esc(proc.role || "engine")}</summary><pre class="raw">${esc(JSON.stringify({argv:proc.argv,env:proc.env},null,2))}</pre></details>`).join("")}<p class="hint">${esc(p.capabilities?.why || "")}</p><div class="row">${(p.capabilities?.declared || []).map(x=>H().tag(x)).join("")}</div></details>`;
  }
  function render() {
    const d=doc(), p=profile(), list=d.profiles || [];
    if (!H().state.docs["engine-settings"] && H().state.errors["engine-settings"]) return H().empty("Set up your workspace first", H().state.errors["engine-settings"], '<a class="btn pri" href="#/control-center">Open Home</a>');
    const heading=`<section class="cc-page-intro"><div><span class="cc-eyebrow">Inference configuration</span><h2>Configure your engine.</h2><p>Choose a profile, assign model files and adjust the settings supported by its pinned engine.</p></div>${act("Create profile","create",H().mutationReason(),true)}</section>`;
    if(!p)return heading + H().empty("Create your first inference profile","Choose a supported model preset and the Linux nodes that will run it. You can complete the model files afterwards.",act("Create profile","create",H().mutationReason(),true));
    selected=p.id;
    const draft=changed(p), stale=draft && draft.version !== d.version, blocked=p.blocked || H().mutationReason();
    const controls=p.controls || [], basic=controls.map((c,i)=>[c,i]).filter(([c])=>c.section!=="advanced"), advanced=controls.map((c,i)=>[c,i]).filter(([c])=>c.section==="advanced");
    const header=H().card("Profile",`<div class="es-profile-bar"><label class="field" for="es-profile">Active editor<select class="inp" id="es-profile">${list.map(r=>`<option value="${esc(r.id)}" ${r.id===p.id?"selected":""}>${esc(r.id)}</option>`).join("")}</select></label><div class="es-profile-meta"><strong>${esc(p.preset)}</strong><span>${esc((p.nodes || []).join(" + "))}</span><span>Engine <code>${esc(String(p.engine_sha || "").slice(0,12))}</code></span></div><a class="btn" href="#/engines?profile=${encodeURIComponent(p.id)}">Launch & status</a></div>`);
    const feedback=`${notice ? `<p class="cc-notice" role="status">${esc(notice)}</p>` : ""}${p.blocked ? `<p class="banner warn">${esc(p.blocked)}</p>` : ""}${stale ? `<p class="banner warn">The saved configuration changed in another tab. Your edits are preserved. Discard and reload before saving.</p>` : ""}`;
    const save=H().card("Apply settings",`<div class="row">${act("Review & save","save",blocked || (stale?"Reload the changed configuration":!draft?"No settings changed":""),true)}${act("Discard edits","discard",!draft?"No unsaved edits":"")}<span class="hint">${draft ? "Unsaved changes are kept while status refreshes." : "No unsaved changes."}</span></div><p class="hint">Saved settings apply at the next start. Auto removes your override and uses the pinned profile or engine default.</p>`);
    const settings=H().card("Capacity & serving",`<div class="es-control-grid">${basic.map(([c,i])=>editor(p,c,i)).join("")}</div>`);
    const extra=advanced.length ? `<details class="card es-advanced"><summary>Advanced optimizations · ${advanced.length} supported controls</summary><p class="hint">These values have a qualification record for this engine revision. Their requirements are checked together before saving.</p><div class="es-control-grid">${advanced.map(([c,i])=>editor(p,c,i)).join("")}</div></details>` : `<div class="cc-lifecycle"><strong>Optimizations follow this release</strong><p>This pinned profile does not expose additional qualified optimization switches. Settings added by newer engines become editable when a compatible integration profile supports them.</p></div>`;
    return heading+header+feedback+settings+extra+save+assets(p)+preview(p);
  }
  function payload(p) {
    const draft=changed(p), changes={};
    for(const c of p.controls || []) {
      const k=keyOf(c);
      if(!draft || !Object.hasOwn(draft.values,k))continue;
      const raw=draft.values[k];
      let value=null;
      if(raw !== "") {
        if(c.type==="integer") {
          value=Number(raw);
          if(!Number.isSafeInteger(value))throw new Error(`${c.label}: enter a whole number or leave it empty for Auto.`);
        } else {
          const option=(c.choices || []).find(o=>String(o.value) === raw);
          if(!option)throw new Error(`${c.label}: select one of the supported values.`);
          value=option.value;
        }
      }
      if(c.section==="profile")changes[c.key]=value;
      else (changes[c.section] ||= {})[c.key]=value;
    }
    return {version:draft?.version || doc().version,profile:p.id,changes};
  }
  function reviewBody(review) {
    const change=(review.changes || []).map(c=>H().cells([`<code>${esc(c.path)}</code>`,`<code>${esc(JSON.stringify(c.before))}</code>`,`<code>${esc(JSON.stringify(c.after))}</code>`])).join("");
    return `${H().table(["Setting","Before","After"],change,"Configuration changes")}<p class="hint">These changes apply at the next launch. No running process will be restarted.</p>${review.preview?.error ? `<p class="cc-notice">Additional setup still needed: ${esc(review.preview.error)}</p>` : ""}`;
  }
  async function save() {
    const p=profile(), args=payload(p), review=await UI().post("engine-settings/validate",args);
    if(!review.valid)throw new Error(review.error || "Settings are not valid.");
    if(review.blocked)throw new Error(review.blocked);
    UI().open({title:`Review ${p.id}`,confirm:"Save for next start",body:reviewBody(review),submit:async()=>{
      const result=await UI().post("engine-settings/save",args);
      if(result.saved !== true)throw new Error(result.error || "The server did not confirm a save.");
      drafts.delete(p.id);notice=`${p.id} saved. The settings apply on its next start.`;H().announce(notice);
    }});
  }
  function nodeFields() {
    const selectedPreset=(creation?.presets || []).find(p=>p.id===creation.preset), options=creation?.machines || [];
    const choices=selected=>options.map(m=>`<option value="${esc(m.id)}" ${m.id===selected?"selected":""}>${esc(m.id)}</option>`).join("");
    const required=selectedPreset?.required_assets || [];
    const body=selectedPreset?.topology==="replicas" ? `<fieldset class="es-create-nodes"><legend>Nodes for this profile</legend>${options.map(m=>`<label class="check"><input type="checkbox" name="es-create-node" value="${esc(m.id)}" ${(creation.nodes || []).includes(m.id)?"checked":""}>${esc(m.id)}</label>`).join("")}</fieldset>` : `<div class="two"><label class="field" for="es-rank0">First worker · rank 0<select class="inp" id="es-rank0">${choices(creation.rank0 || options[0]?.id)}</select></label><label class="field" for="es-rank1">Second worker · rank 1<select class="inp" id="es-rank1">${choices(creation.rank1 || options[1]?.id)}</select></label></div><p class="hint">A pair needs two different Linux nodes and a working USB4 link.</p>`;
    return body+`<p class="hint">Required assets: ${esc(required.join(", "))}. File paths and verified identities can be assigned after creating the profile.</p>`;
  }
  async function create() {
    const fresh=await H().request("engine-settings");
    const machines=(fresh.machines || []).filter(m=>m.selectable !== false && m.os==="linux" && m.access==="ssh");
    if(!machines.length)throw new Error("Add a Linux SSH worker from Home before creating an inference profile.");
    creation={version:fresh.version,presets:fresh.presets || [],machines,preset:fresh.presets?.[0]?.id,nodes:[machines[0].id]};
    UI().open({title:"Create inference profile",confirm:"Create profile",body:`<label class="field" for="es-new-id">Profile name<input class="inp" id="es-new-id" placeholder="my-qwen" spellcheck="false" autocomplete="off"></label><label class="field" for="es-new-preset">Model preset<select class="inp" id="es-new-preset">${creation.presets.map(p=>`<option value="${esc(p.id)}">${esc(p.label || p.id)}</option>`).join("")}</select></label><div id="es-new-nodes">${nodeFields()}</div>`,submit:async()=>{
      const id=document.getElementById("es-new-id").value.trim(), preset=creation.presets.find(p=>p.id===creation.preset);
      const args={version:creation.version,id,preset:creation.preset};
      if(!id)throw new Error("Give this profile a name.");
      if(preset.topology==="replicas") {
        args.nodes=[...document.querySelectorAll('input[name="es-create-node"]:checked')].map(e=>e.value);
        if(!args.nodes.length)throw new Error("Choose at least one worker.");
      } else {
        args.rank0=document.getElementById("es-rank0").value;args.rank1=document.getElementById("es-rank1").value;
        if(args.rank0===args.rank1)throw new Error("Choose two different nodes for the pair.");
      }
      const review=await UI().post("engine-settings/validate",{...args,op:"create"});
      if(!review.valid)throw new Error(review.error || "Profile is not valid.");
      const result=await UI().post("engine-settings/create",args);
      if(result.saved !== true)throw new Error(result.error || "Profile was not saved.");
      selected=id;notice=`${id} created. Assign its model files and install the selected components next.`;H().announce(notice);
      location.hash=`#/engine-settings?profile=${encodeURIComponent(id)}`;
    }});
  }
  async function assetEditor(profileId,machine,key) {
    const fresh=await H().request("engine-settings"), p=(fresh.profiles || []).find(p=>p.id===profileId), row=p?.assets?.find(a=>a.machine===machine&&a.key===key);
    if(!p || !row)throw new Error("That model file is no longer required by this profile.");
    const value=row.value || {};
    UI().open({title:`${key} · ${machine}`,confirm:"Save file identity",body:`<p>Use the actual file's path and metadata on <strong>${esc(machine)}</strong>. Saving metadata alone does not download or verify the file.</p><label class="field" for="es-asset-path">Absolute file path on node<input class="inp" id="es-asset-path" value="${esc(value.path)}" placeholder="/home/user/models/model.gguf" spellcheck="false"></label><label class="field" for="es-asset-bytes">File size in bytes<input class="inp" type="number" min="0" step="1" id="es-asset-bytes" value="${esc(value.bytes ?? "")}"></label><label class="field" for="es-asset-sha">SHA-256<input class="inp mono" id="es-asset-sha" value="${esc(value.sha256)}" spellcheck="false" autocomplete="off" maxlength="64"></label><details><summary>Controller copy (optional)</summary><label class="field" for="es-asset-control">Absolute path on controller<input class="inp" id="es-asset-control" value="${esc(value.control_path)}" spellcheck="false"></label></details><p class="hint">File identities may be shared by profiles using the same asset on this node.</p>`,submit:async()=>{
      const path=document.getElementById("es-asset-path").value.trim(),size=document.getElementById("es-asset-bytes").value,sha256=document.getElementById("es-asset-sha").value.trim().toLowerCase();
      const bytes=Number(size), control=document.getElementById("es-asset-control").value.trim();
      if(!path || size==="" || !Number.isSafeInteger(bytes) || bytes<0 || !/^[0-9a-f]{64}$/.test(sha256))throw new Error("Enter an absolute path, the exact size in bytes and the complete 64-character SHA-256.");
      const asset={path,bytes,sha256,...(control?{control_path:control}:{})},args={version:fresh.version,profile:profileId,changes:{assets:{[machine]:{[key]:asset}}}};
      const checked=await UI().post("engine-settings/validate",args);
      if(!checked.valid)throw new Error(checked.error || "The file metadata is not valid.");
      const result=await UI().post("engine-settings/save",args);
      if(result.saved !== true)throw new Error(result.error || "File identity was not saved.");
      const draft=drafts.get(profileId);
      if(draft?.version === fresh.version) draft.version=result.version;
      notice=`${key} saved for ${machine}. Its file contents will be verified during preparation.`;H().announce(notice);
    }});
  }
  document.addEventListener("input",event=>{
    const input=event.target;
    if(!input.dataset?.esKey)return;
    const p=(doc().profiles || []).find(p=>p.id===input.dataset.esProfile);
    if(!p)return;
    let draft=drafts.get(p.id);
    if(!draft){draft={version:doc().version,values:{}};drafts.set(p.id,draft);}
    draft.values[input.dataset.esKey]=input.value;
    H().render();
  });
  document.addEventListener("change",event=>{
    const input=event.target;
    if(input.id==="es-profile"){selected=input.value;location.hash=`#/engine-settings?profile=${encodeURIComponent(selected)}`;}
    if(input.id==="es-new-preset" && creation){creation.preset=input.value;document.getElementById("es-new-nodes").innerHTML=nodeFields();}
  });
  document.addEventListener("click",async event=>{
    const c=event.target.closest('[data-page-action^="es-"]');
    if(!c || c.disabled)return;
    try {
      if(c.dataset.pageAction==="es-create")await create();
      else if(c.dataset.pageAction==="es-save")await save();
      else if(c.dataset.pageAction==="es-discard"){const p=profile();drafts.delete(p.id);notice="Unsaved changes discarded.";await H().refresh();}
      else if(c.dataset.pageAction==="es-asset")await assetEditor(c.dataset.profile,c.dataset.machine,c.dataset.key);
    }catch(error){notice=error.message;H().announce(notice);H().render();}
  });
  (window.HydraweavePages ||= {})["engine-settings"]={title:"Engine configuration",group:"Control center",order:40,
    icon:'<path d="M3 5h14M3 10h14M3 15h14"/><circle cx="7" cy="5" r="2" fill="var(--surface)"/><circle cx="13" cy="10" r="2" fill="var(--surface)"/><circle cx="8" cy="15" r="2" fill="var(--surface)"/>',
    endpoints:()=>["engine-settings"],ready:()=>!!H().state.docs["engine-settings"] || !!H().state.errors["engine-settings"],render};
})();
