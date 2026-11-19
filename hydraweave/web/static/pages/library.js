/* Model library (F36): model units per machine and verification against their manifests. */
"use strict";
(() => {
  const H = () => window.Hydraweave;
  const view = {busy: false, pending: 0, accept: false, copy: null, plan: null};
  const doc = () => H().state.docs.library;
  const when = t => typeof t === "number" ? new Date(t * 1000).toLocaleString() : null;
  const size = (value, why) => H().display(value, why, "bytes");
  const verifiable = status => !["none", "unknown"].includes(status);
  const statusClass = status => ({complete: "ok", partial: "warn", missing: "err", conflict: "err", none: "off", unknown: "off"})[status] || "off";
  const pill = (text, cls) => `<span class="pill p-${cls}">${H().esc(text)}</span>`;
  const act = (label, action, data = {}, {disabled = "", style = "sm"} = {}) => H().button(label, "", {disabled, style,
    extra: `data-page-action="${action}" ${Object.entries(data).map(([k, v]) => `data-${k}="${H().esc(v)}"`).join(" ")}`});
  const runningJobs = () => (doc()?.jobs || []).filter(j => j.state === "running");

  function verifyCell(machine, root, name) {
    const result = doc()?.verify?.[`${machine}|${root}|${name}`];
    if (!result) return H().unavailable("not verified here yet");
    if (result.state !== "succeeded") return `${pill("failed", "err")} <span class="hint">${H().esc(result.reason || "")}</span>`;
    const counts = result.result?.counts || {};
    const ok = result.result?.ok;
    return `${pill(ok ? "verified" : "mismatch", ok ? "ok" : "err")} <span class="hint">${H().esc(`${counts.ok ?? 0}/${result.result?.entries ?? "?"} files · ${when(result.at)}`)}</span>`;
  }

  function unitRows(machine, root) {
    const {esc, cells, display, unavailable} = H();
    // Copies between machines are not part of this release: the control is shown disabled with the reason.
    const copyWhy = doc()?.copy?.available ? "" : doc()?.copy?.why || "copies between machines are not available";
    // Model verification is held in this release too (nothing starts it): disabled with the reason.
    const verifyWhy = doc()?.verify_job?.available ? "" : doc()?.verify_job?.why || "model verification is not available";
    return (root.units || []).map(unit => {
      const manifest = unit.manifest || {};
      const status = manifest.status || "unknown";
      const busy = view.busy ? "Another library request is being sent" : "";
      const rootWhy = machine.roots.some(r=>r.path===root.path) ? "" : "This scanned root is no longer configured; rescan the machine";
      const verifyBlocker = verifyWhy || busy || rootWhy || (!machine.verify_available ? "Managed verification requires a Linux node or the local controller" : "") || (verifiable(status) ? "" : status === "none" ? "No manifest entries to verify against" : "The scan reached a bound in this unit");
      const engine = unit.engine || {};
      const engineText = engine.family ? `${engine.family}${engine.presets?.length ? ` · ${engine.presets.join(", ")}` : ` · ${engine.why || "no preset"}`}` : "";
      return cells([
        `<span class="mono lib-unit">${esc(unit.name)}</span>`,
        `<span class="hide-n">${esc(unit.kind || "?")}</span>`,
        unit.format ? `${esc(unit.format)}${unit.quant ? ` <span class="hint">${esc(unit.quant)}</span>` : ""}` : unavailable("scan again to classify"),
        engineText ? `${esc(engineText)} <span class="hint">${esc(engine.basis || "")}</span>` : unavailable(engine.why || "not mapped"),
        size(unit.size_bytes ?? unit.bytes, manifest.why || "not measured"),
        `<span class="hide-n">${display(unit.files, manifest.why || "not counted")}</span>`,
        `<span class="hide-n">${unit.newest_mtime ? esc(when(unit.newest_mtime)) : H().unavailable(manifest.why || "no files")}</span>`,
        `${pill(status, statusClass(status))}${manifest.unlisted ? ` <span class="hint">${esc(manifest.unlisted)} not listed</span>` : ""}${manifest.missing ? ` <span class="hint">${esc(manifest.missing)} missing</span>` : ""}`,
        `<span class="hide-n">${verifyCell(machine.id, root.path, unit.name)}</span>`,
        `<div class="row lib-actions">${act("Verify", "library-verify", {machine: machine.id, root: root.path, name: unit.name}, {disabled: verifyBlocker})}${act("Copy to…", "library-copy", {machine: machine.id, root: root.path, name: unit.name}, {disabled: copyWhy || busy || rootWhy || (status !== "complete" ? "A complete manifest is required" : "") || (!machine.copy_destinations?.length ? "Configure a pinned peer_ssh destination first" : "")})}</div>`]);
    }).join("");
  }

  function machineCard(machine) {
    const {esc, card, table, tag, unavailable} = H();
    const inventory = machine.inventory;
    const scanning = (doc()?.scanning || []).includes(machine.id);
    const subtitle = inventory?.scanned_at ? `scanned ${when(inventory.scanned_at)}` : "";
    const head = `<div class="row">${tag(machine.os)}${tag(machine.access)}${machine.lab_mode ? tag("lab mode") : ""}${scanning ? H().statusPill("scanning") : ""}${act("Scan", "library-scan", {machine: machine.id}, {disabled: scanning ? "A scan of this machine is running" : !machine.roots.length ? "No model roots configured" : ""})}</div>`;
    let body;
    if (!machine.roots.length) body = `<p>${unavailable(machine.why?.roots)}</p>`;
    else if (!inventory) body = `<p>${unavailable(machine.why?.inventory || "not scanned yet")}</p>`;
    else if (inventory.error) body = `<p>${unavailable(inventory.error)}</p>`;
    else body = (inventory.roots || []).map(root => {
      const notes = [root.error ? `unavailable · ${root.error}` : "", root.truncated?.length ? `scan bounds reached: ${root.truncated.join(", ")} (affected units show unknown)` : "",
        root.other_files ? `${root.other_files} other files at the top level` : ""].filter(Boolean);
      return `<h3 class="lib-root"><span class="mono">${esc(root.path)}</span> ${tag(root.source || "config")} <span class="hint">free ${size(root.free_bytes, "not reported")}</span></h3>${notes.map(n => `<p class="hint">${esc(n)}</p>`).join("")}${root.error ? "" : table(["Unit", ["Kind", "hide-n"], "Format / quant", "Engine", "Size", ["Files", "hide-n"], ["Newest file", "hide-n"], "Manifest", ["Last verify", "hide-n"], "Actions"], unitRows(machine, root), `Model units in ${root.path}`)}`;
    }).join("");
    return card(machine.id, head + body, subtitle);
  }

  function jobsCard() {
    const {esc, card, table, cells} = H();
    const jobs = doc()?.jobs || [];
    const body = jobs.map(job => {
      const what = job.kind === "verify" ? `${job.name} on ${job.machine}` : esc(job.kind);
      const node = job.node || {};
      // A pause is shown only while the job is still running; a finished or unresolved job shows its own state
      // and reason (a pause reason left in its last node record is history).
      const paused = job.state === "running" ? node.paused?.reason : null;
      const state = paused ? "paused" : job.state;
      return cells([esc(job.kind), esc(what), pill(state, ({succeeded: "ok", running: "info", paused: "warn", unresolved: "warn", failed: "err"})[state] || "off"), esc(paused ? `paused: ${paused}` : job.reason || job.progress || ""), esc(when(job.started) || "")]);
    }).join("");
    return card("Library jobs", jobs.length ? table(["Kind", "What", "State", "Progress", "Started"], body, "Library jobs") : `<p class="t2">No library jobs recorded.</p>`,
      "owned jobs, verified results and direct-transfer byte counts");
  }

  function copyCard() {
    if (!view.copy) return "";
    const {esc, card} = H(), source = (doc()?.machines || []).find(m=>m.id===view.copy.machine);
    const destinations = (doc()?.machines || []).filter(m=>(source?.copy_destinations || []).includes(m.id));
    const options = destinations.flatMap(m=>(m.roots || []).map(r=>({machine:m.id,root:r.path})));
    const selected = view.copy.destination ? JSON.stringify([view.copy.destination,view.copy.destination_root]) : "";
    return card("Copy " + view.copy.name, `<p>Model bytes travel directly between the selected nodes. Existing files and retained staging files are reused only after SHA-256 verification.</p><label for="library-copy-destination">Destination machine and model root</label><select class="inp" id="library-copy-destination"><option value="">Choose a destination</option>${options.map(o=>`<option value="${esc(JSON.stringify([o.machine,o.root]))}"${selected===JSON.stringify([o.machine,o.root])?" selected":""}>${esc(o.machine + " · " + o.root)}</option>`).join("")}</select>${view.plan ? `<p>${size(view.plan.bytes)} · ${esc(view.plan.files?.length)} files · ${esc(view.plan.transport)}</p><details><summary>Review file hashes</summary><pre>${esc(JSON.stringify(view.plan.files,null,2))}</pre></details>${act("Confirm direct copy", "library-copy-confirm", {}, {disabled:view.busy?"A request is running":"",style:"pri"})}` : act("Review copy plan", "library-copy-plan", {}, {disabled:view.busy?"A request is running":""})} ${act("Close", "library-copy-close")}`);
  }

  function activeCard() {
    const jobs=(H().state.lastJobs || []).filter(j=>j.action?.startsWith("library/") && j.state==="running");
    return jobs.length ? H().card("Active library work",jobs.map(j=>`<p>${H().esc(j.action)} · ${H().esc(j.progress || "Running")} ${act("Cancel", "library-cancel", {job:j.id || j.job})}</p>`).join("")) : "";
  }

  function rootsCard() {
    const {esc, card} = H();
    const machines = doc()?.machines || [];
    const body = machines.map(m => {
      const config = m.roots.filter(r => r.source === "config");
      const prefs = m.roots.filter(r => r.source === "prefs").map(r => r.path);
      return `<div class="lib-roots"><h3>${esc(m.id)}</h3><p class="hint">From the configuration: ${config.length ? config.map(r => `<span class="mono">${esc(r.path)}</span>`).join(", ") : "none"}</p>
        <div class="field"><label for="library-roots-${esc(m.id)}">Extra roots on ${esc(m.id)} (one absolute directory per line)</label><textarea class="inp mono" rows="2" id="library-roots-${esc(m.id)}">${esc(prefs.join("\n"))}</textarea></div>
        ${act("Save roots", "library-roots", {machine: m.id}, {disabled: view.busy ? "A request is being sent" : ""})}</div>`;
    }).join("");
    return card("Model roots", `<div class="grid g2">${body}</div>`, "extra roots per machine, kept in the portal preferences");
  }

  function ledgerCard() {
    const {esc, card, unavailable} = H();
    const ledger = doc()?.ledger || {};
    let body;
    if (!ledger.configured) body = `<p>${unavailable(ledger.why || "not configured")}</p>`;
    else if (!ledger.baseline) body = `<p>${unavailable(ledger.why || "no accepted baseline")}</p>`;
    else body = `<p class="t2">Accepted ${esc(when(ledger.baseline.accepted_at))}${ledger.baseline.advanced_at ? ` · last checked growth ${esc(when(ledger.baseline.advanced_at))}` : ""} · ${esc(ledger.baseline.size)} bytes covered.</p>`;
    const button = !ledger.configured ? "" : view.accept
      ? `<div class="row">${act("Confirm: no RTX PRO big job is hidden by a rotation", "library-accept-confirm", {}, {style: "dan"})}${act("Keep the current baseline", "library-accept-cancel", {}, {style: ""})}</div>`
      : act("Accept the current ledger", "library-accept", {}, {style: "", disabled: view.busy ? "A request is being sent" : ""});
    return card("RTX big-job guard", body + `<p class="hint">Verification on the control machine pauses while the coordination ledger has an open PC-BIG start; a replaced or rewritten ledger blocks it until it is accepted again.</p>` + button);
  }

  function kpis() {
    const machines = doc()?.machines || [];
    const scanned = machines.filter(m => m.inventory && !m.inventory.error);
    const units = scanned.flatMap(m => (m.inventory.roots || []).flatMap(r => r.units || []));
    const known = units.filter(u => typeof u.bytes === "number");
    const total = known.reduce((sum, u) => sum + u.bytes, 0);
    const complete = units.filter(u => u.manifest?.status === "complete").length;
    const {kpi, esc, unavailable} = H();
    return `<div class="grid g4 kpis">${kpi("Machines scanned", `${scanned.length}<small>/ ${machines.length}</small>`, "inventory from the last scan")}${kpi("Model units", scanned.length ? esc(units.length) : unavailable("not scanned"), "directories and model files")}${kpi("Total size", known.length ? size(total) : unavailable("not scanned"), units.length > known.length ? `${units.length - known.length} ${units.length - known.length === 1 ? "unit" : "units"} not measured (scan bound)` : "measured by the node")}${kpi("Complete manifests", scanned.length ? `${complete}<small>/ ${units.length}</small>` : unavailable("not scanned"), "*.sha256 lists every file")}</div>`;
  }

  (window.HydraweavePages ||= {}).library = {
    title: "Model library",
    group: "Operations",
    order: 40,
    icon: '<path d="M3 4h4v12H3z"/><path d="M8 4h4v12H8z"/><path d="M13.5 4.5l3.5 1-3 11-3.5-1z"/>',
    endpoints: () => ["library"],
    ready: () => !!doc(),
    actions: () => act("Scan all", "library-scan", {}, {style: "", disabled: (doc()?.scanning || []).length ? "A scan is running" : view.busy ? "A request is being sent" : ""}),
    render() {
      const library = doc();
      const {esc} = H();
      const store = library.why?.inventory ? `<p class="hint">${esc(library.why.inventory)}</p>` : "";
      return `<div class="grid">${kpis()}${store}<a class="btn" href="#/assets">Download pinned model files</a>${copyCard()}${activeCard()}${(library.machines || []).map(machineCard).join("")}${jobsCard()}<div class="grid g2">${rootsCard()}${ledgerCard()}</div></div>`;
    },
  };

  async function post(endpoint, body, done) {
    if(view.busy && endpoint!=="cancel") return;
    view.pending++; view.busy = true; H().render();
    try {
      const result = await H().request(endpoint, {method: "POST", headers: {"Content-Type": "application/json", "X-Hydraweave-CSRF": H().csrf() || ""}, body: JSON.stringify(body)});
      if (result.id || result.job) H().own(result.id || result.job);
      if (done) done(result);
    } catch (error) {
      H().announce(error.message);
    } finally {
      view.pending--; view.busy = view.pending>0; H().render(); H().refresh();
    }
  }

  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action^='library-']");
    if (!control || control.disabled) return;
    const d = control.dataset;
    switch (d.pageAction) {
      case "library-verify": post("library/verify", {machine:d.machine,model_root:d.root,name:d.name},()=>H().announce("Verification started")); break;
      case "library-copy": view.copy={machine:d.machine,model_root:d.root,name:d.name}; view.plan=null; H().render(); break;
      case "library-copy-close": view.copy=null; view.plan=null; H().render(); break;
      case "library-copy-plan": {
        const value=document.getElementById("library-copy-destination")?.value;
        if(!value) {H().announce("Choose a destination");break;}
        const [destination,destination_root]=JSON.parse(value);
        view.copy={...view.copy,destination,destination_root};
        const reviewed={...view.copy};
        post("library/copy-plan",reviewed,r=>{if(JSON.stringify(view.copy)===JSON.stringify(reviewed))view.plan=r;}); break;
      }
      case "library-copy-confirm": if(view.copy&&view.plan) post("library/copy",{...view.copy,confirm:view.plan.confirm},()=>{view.copy=null;view.plan=null;H().announce("Direct copy started");}); break;
      case "library-cancel": post("cancel",{job:d.job},()=>H().announce("Cancellation requested")); break;
      case "library-scan": post("library/scan", d.machine ? {machine: d.machine} : {}, () => H().announce("Scan started")); break;
      case "library-roots": {
        const text = document.getElementById(`library-roots-${d.machine}`)?.value || "";
        post("library/roots", {machine: d.machine, roots: text.split("\n").map(s => s.trim()).filter(Boolean)}, () => H().announce(`Roots of ${d.machine} saved`));
        break;
      }
      case "library-accept": view.accept = true; H().render(); break;
      case "library-accept-cancel": view.accept = false; H().render(); break;
      case "library-accept-confirm": view.accept = false; post("library/ledger-accept", {}, () => H().announce("Ledger accepted as the new baseline")); break;
    }
  });
  document.addEventListener("change",event=>{if(event.target.id==="library-copy-destination") {view.plan=null; const value=event.target.value;if(value) {const [destination,destination_root]=JSON.parse(value);Object.assign(view.copy,{destination,destination_root});} H().render();}});
})();
