/* Component inventory, staged downloads and node installation. */
"use strict";
(() => {
  const H = () => window.Hydraweave;
  const UI = () => window.HydraweaveOpsUI;
  const data = () => H().state.docs.components || {};
  const esc = value => H().esc(value ?? "");
  const sha = value => typeof value === "string" ? value.slice(0,12) : "Not reported";
  const when = value => typeof value === "number" ? new Date(value * 1000).toLocaleString() : "Not checked yet";
  const rows = value => Array.isArray(value) ? value : [];
  let notice = "";
  const act = (label,name,attributes={},disabled="",primary=false) => H().button(label,"",{style:primary?"pri":"",disabled,
    extra:`data-page-action="cmp-${name}" ${Object.entries(attributes).map(([k,v])=>`data-${k}="${esc(v)}"`).join(" ")}`});
  function download(row, commit, label, upstream=false) {
    if (!commit) return "";
    const cached = rows(row.downloads).find(d=>d.commit === commit && ["ready","complete","downloaded","staged"].includes(d.state));
    return cached ? `<span class="pill p-ok">Source downloaded</span>` : act(label,"download",{component:row.id,commit,upstream:String(upstream)},H().mutationReason() || (upstream && row.upstream?.download_allowed === false ? "Check again before downloading this upstream revision" : "") || (row.download?.enabled === false ? row.download.why || "Download unavailable" : ""),!upstream);
  }
  function component(row) {
    const pins = rows(row.pins), upstream=row.upstream || {}, active=rows(row.active), downloaded=rows(row.downloads);
    const problem = upstream.error || (typeof row.why === "string" ? row.why : row.why?.check || row.why?.upstream);
    const newer = row.update_available === true, matches=pins.some(p=>p.commit === upstream.commit);
    const pinned = pins.length ? pins.map(p=>`<div class="cmp-pin"><div><span class="cc-eyebrow">Pinned for integration</span><code title="${esc(p.commit)}">${esc(p.commit ? sha(p.commit) : `Unpublished pin ${p.commit_prefix || ""}`)}</code><p class="hint">${rows(p.profiles).length ? "Profiles: " + esc(p.profiles.join(", ")) : esc(Array.isArray(p.source) ? p.source.join(", ") : p.source || "Workspace default")}</p></div><div>${download(row,p.commit,"Download pinned source")}</div></div>`).join("") : `<p class="hint">No integration pin is set for this component.</p>`;
    const latest = upstream.commit ? `<div class="cmp-upstream"><div><span class="cc-eyebrow">Latest upstream</span><code title="${esc(upstream.commit)}">${esc(sha(upstream.commit))}</code><p class="hint">${matches ? "Matches an integration pin" : "Available source; compatibility has not been established by this check"}</p></div>${matches ? "" : download(row,upstream.commit,"Download upstream source",true)}</div>` : `<p class="hint">Check for updates to compare the upstream version.</p>`;
    const installations = active.length ? `<div class="cmp-active"><span class="cc-eyebrow">Saved launch records</span>${active.map(a=>`<p><code>${esc(sha(a.commit))}</code> · ${esc(a.profile)} · ${esc(rows(a.machines).join(", "))} ${H().statusPill(a.state || "recorded")}</p>`).join("")}<p class="hint">Reported from saved launches. Check Nodes for current reachability.</p></div>` : `<p class="hint">No active version is recorded by a saved launch.</p>`;
    return `<section class="card cmp-card"><div class="row between"><h2>${esc(row.label || row.id)}</h2>${newer ? `<span class="pill p-info">Upstream update</span>` : upstream.commit && matches ? `<span class="pill p-ok">Pin matches upstream</span>` : ""}</div><p class="cmp-role">${esc(row.role || "")}</p>${pinned}${latest}${problem ? `<p class="field-error" role="status">${esc(problem)}</p>` : ""}<div class="cmp-card-foot"><span>Checked ${esc(when(upstream.checked_at))}</span>${act("Check","check",{component:row.id},H().mutationReason())}</div><details><summary>Downloads and installed evidence</summary>${installations}${downloaded.length ? H().table(["Source","State","Downloaded"],downloaded.map(d=>H().cells([`<code title="${esc(d.commit)}">${esc(sha(d.commit))}</code>`,H().statusPill(d.state || "unknown"),esc(when(d.downloaded_at))])).join(""),"Downloaded component sources") : `<p class="hint">No sources downloaded through this workspace.</p>`}<p class="hint">${esc(row.activation?.note || "Downloading stages source files. Building and activating an engine is a separate profile operation.")}</p><p class="mono">${esc(row.repo)}</p></details></section>`;
  }
  function profiles() {
    const list = rows(data().profiles);
    if (!list.length) return H().card("Install on compute nodes",`<p>Create a profile to choose the engine, compatible dependencies and target nodes.</p><a class="btn pri" href="#/engine-settings">Create a profile</a>`);
    return H().card("Install a profile on its nodes",`<p>First prepare the node tools. The installer then downloads the profile's pinned sources on the build node, builds a release and prepares its configured targets.</p><div class="cmp-profile-list">${list.map(p=>`<div class="cmp-profile"><div><strong>${esc(p.id)}</strong><p class="hint">${esc(p.preset)} · ${esc(rows(p.machines).join(", "))}</p></div><div class="row">${act("Prepare node tools","nodes",{profile:p.id},H().mutationReason())}${act("Review installation","plan",{profile:p.id},H().mutationReason())}</div></div>`).join("")}</div><p class="hint">Controller source downloads and node installations are separate. The selected profile determines the versions that will actually run.</p>`);
  }
  function render() {
    const d=data(), list=rows(d.components), upstream=list.filter(r=>r.update_available === true).length;
    return `<section class="cc-page-intro"><div><span class="cc-eyebrow">Software lifecycle</span><h2>Keep your stack in sync.</h2><p>Check what changed, download the source you need, then install a compatible profile on its nodes.</p></div>${act("Check all updates","check",{},H().mutationReason(),true)}</section><div class="cc-status-row"><span>${list.length} components</span><span>${upstream} upstream update${upstream === 1 ? "" : "s"}</span><span>Downloads: ${esc(d.storage?.scope || "controller")}</span></div>${notice ? `<p class="cc-notice" role="status">${esc(notice)}</p>` : ""}${d.storage?.error ? `<div class="banner warn" role="alert">${esc(d.storage.error)}</div>` : ""}${d.config_error ? `<div class="banner warn">${esc(d.config_error)} <a href="#/control-center">Open setup</a></div>` : ""}<div class="grid g2 cmp-grid">${list.map(component).join("")}</div>${profiles()}<details class="cc-storage"><summary>Source download location</summary><p class="mono">${esc(d.storage?.path || "Not reported")}</p><p class="hint">A completed download does not load a kernel module or replace an engine that is running.</p></details>`;
  }
  async function plan(profile) {
    const p=await H().request(`components/plan?profile=${encodeURIComponent(profile)}`);
    const blockers=rows(p.blockers);
    const steps=rows(p.steps);
    const machines=rows(p.machines), build=p.build_on || machines[0] || "";
    const formatted=steps.map(s=>typeof s === "string" ? esc(s) : `<b>${esc(s.title || s.label || s.action || s.id || s.step || "")}</b>${s.detail || s.note ? ` · ${esc(s.detail || s.note)}` : ""}`);
    UI().open({title:`Install ${profile}`,confirm:"Install on nodes",body:`<p>After preparing the node tools, build and install the versions selected by this profile. Progress and any errors appear above the page.</p>${blockers.length ? `<div class="banner warn">${blockers.map(b=>esc(typeof b === "string" ? b : b.reason || b.message)).join("<br>")}</div>` : ""}${steps.length ? `<ol class="cc-install-steps">${formatted.map(s=>`<li>${s}</li>`).join("")}</ol>` : ""}<label class="field" for="cmp-build-on">Build node<select class="inp" id="cmp-build-on">${machines.map(m=>{const id=typeof m === "string" ? m : m.id;return `<option value="${esc(id)}" ${id === build ? "selected" : ""}>${esc(id)}</option>`;}).join("")}</select></label><p class="hint">Targets: ${esc(machines.map(m=>typeof m === "string" ? m : m.id).join(", "))}</p>${p.note ? `<p class="hint">${esc(p.note)}</p>` : ""}${p.driver?.note ? `<p class="hint">${esc(p.driver.note)}</p>` : ""}`,submit:async()=>{
      if(blockers.length) throw new Error(blockers.map(b=>typeof b === "string" ? b : b.reason || b.message).join("; "));
      const build_on=document.getElementById("cmp-build-on").value;
      if(!build_on) throw new Error("This profile has no installable build node.");
      await H().mutate("install",{profile,build_on});
    }});
  }
  document.addEventListener("click",async event=>{
    const control=event.target.closest('[data-page-action^="cmp-"]');
    if(!control || control.disabled)return;
    try {
      const v=control.dataset;
      if(v.pageAction === "cmp-check") await H().mutate("components/check",v.component ? {component:v.component} : {});
      else if(v.pageAction === "cmp-plan") await plan(v.profile);
      else if(v.pageAction === "cmp-nodes") await H().mutate("ops/nodes-update",{profile:v.profile});
      else if(v.pageAction === "cmp-download") {
        const commit=v.commit,component=v.component;
        if(v.upstream === "true") {
          UI().open({title:"Download upstream source",confirm:"Download source",body:`<p>Stage <strong>${esc(component)}</strong> at commit <code>${esc(commit)}</code> on the controller.</p><p>This version has not been selected by a compatible integration profile. The existing profile pins remain in use until a compatible profile is available.</p>`,submit:()=>H().mutate("components/download",{component,commit})});
        } else await H().mutate("components/download",{component,commit});
      }
    } catch(error) { notice=error.message; H().announce(notice); H().render(); }
  });
  (window.HydraweavePages ||= {}).components = {title:"Components & updates",group:"Control center",order:30,
    icon:'<path d="M4 6h12v11H4zM3 3h14v3H3zM8 10h4"/><path d="M13 13l2 2 3-4"/>',
    endpoints:()=>["components"],render};
})();
