/* Guided control plane, node onboarding and explicit router placement. */
"use strict";
(() => {
  const H = () => window.Hydraweave;
  const UI = () => window.HydraweaveOpsUI;
  const doc = () => H().state.docs.setup || {};
  const esc = value => H().esc(value ?? "");
  let notice = "";
  let wizardStep = 0;
  let wizardLeft = false;
  try { wizardStep = Math.max(0, Math.min(3, Number(localStorage.getItem("hydraweave-setup-step")) || 0)); wizardLeft = localStorage.getItem("hydraweave-setup-left") === "true"; } catch (_) { /* storage may be disabled */ }
  const setWizardStep = value => { wizardStep = Math.max(0, Math.min(3, value)); try { localStorage.setItem("hydraweave-setup-step", String(wizardStep)); } catch (_) { /* storage may be disabled */ } };
  const action = (label, name, data = {}, disabled = "", primary = false) => H().button(label, "", {
    style: primary ? "pri" : "", disabled,
    extra: `data-page-action="cc-${name}" ${Object.entries(data).map(([k,v]) => `data-${k}="${esc(v)}"`).join(" ")}`,
  });
  const link = (label, href, primary = false) => `<a class="btn${primary ? " pri" : ""}" href="${esc(href)}">${esc(label)}</a>`;
  const mark = kind => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${({engine:"M7 7h10v10H7z M4 9h3m-3 6h3m10-6h3m-3 6h3M9 4v3m6-3v3M9 17v3m6-3v3",turns:"m3 8 9-5 9 5-9 5z M3 12l9 5 9-5M3 16l9 5 9-5",node:"M4 4h16v6H4z M4 14h16v6H4z M7 7h1m3 0h6M7 17h1m3 0h6",pc:"M3 4h18v13H3z M9 21h6m-3-4v4"})[kind]}"></path></svg>`;

  // Presentation of existing read documents only. No launch or queue mutation lives on Home.
  const fresh = name => H().state.fresh && !H().state.errors[name] && H().state.fetched[name]
    && Date.now() - H().state.fetched[name] <= Math.max(10000, H().prefs().refresh_s * 3000);
  const presets = () => Array.isArray(H().state.docs["engine-settings"]?.presets) ? H().state.docs["engine-settings"].presets : [];
  function servingStatus() {
    if (!fresh("profiles") || !Array.isArray(H().state.docs.profiles?.profiles)) return "Engine status unavailable";
    const profiles = H().profiles();
    const serving = profiles.filter(p => p.state === "running");
    if (serving.length) return `${serving.length} ${serving.length === 1 ? "engine" : "engines"} serving`;
    if (profiles.some(p => !["stopped", "idle", "not installed", "unavailable", "disabled"].includes(p.state))) return "Check engine state";
    return "No engine serving";
  }
  function weave() {
    const observed = H().machines(), configured = doc().nodes || [];
    const slots = configured.map(n => ({id:n.id,label:n.id,detail:n.access === "local" ? "This computer" : (n.os || "Machine")}));
    const links = H().rows(H().state.docs.topology?.links);
    const linksHtml = links.map(l => `<li>${esc(l.from)} → ${esc(l.to)} · ${esc(l.kind || "Link")} · ${esc(l.health || "status unavailable")}</li>`).join("");
    if (!slots.length && !links.length) return H().card("Machines & connections", `<p>No machines or links are configured yet.</p>${action("Add a machine", "add-node", {}, H().mutationReason(), true)}`);
    return `<section class="card cc-weave" aria-labelledby="cc-weave-title"><div class="cc-section-head"><div><span class="cc-eyebrow">Machines & connections</span><h2 id="cc-weave-title">The weave</h2></div><a class="cc-text-link" href="#/topology">Topology & links <span aria-hidden="true">↗</span></a></div>
      <div class="cc-weave-map"><div class="cc-thread-links" aria-hidden="true"></div>
      <ul class="cc-threads">${slots.map(s => {
        const machine = observed.find(m => m.id === s.id), config = configured.find(n => n.id === s.id);
        const health = fresh("devices") && machine ? machine.health || "Status unavailable" : "Status unavailable";
        return `<li class="cc-thread"><div class="cc-machine-head"><span class="cc-machine-icon" aria-hidden="true">${mark("node")}</span><a class="cc-machine-name" href="#/machine?id=${encodeURIComponent(s.id)}">${esc(s.label)}</a><span class="cc-machine-detail">${esc(s.detail)}</span><span class="cc-machine-health">${esc(health)}</span></div></li>`;
      }).join("")}</ul>${links.length ? `<ul class="cc-reported-links">${linksHtml}</ul>` : ""}</div></section>`;
  }
  function engineCatalog() {
    const rows = presets();
    return H().card("Models", rows.length ? `<ul class="cc-engine-list">${rows.map(p => `<li><a href="#/engine-settings"><strong>${esc(p.label || p.id)}</strong><span>${esc(p.family || "Preset")}</span><span>${esc(p.qualified === false ? p.notes || "Not qualified" : "Available preset")}</span></a></li>`).join("")}</ul>` : `<p>No model presets are available yet.</p>${link("Open engine settings", "#/engine-settings", true)}`);
  }

  function controller() {
    const d = doc(), c = d.controller || {}, router = d.router || {};
    return `<div class="cc-roles"><div><span class="cc-eyebrow">Control plane</span><strong>${esc(c.hostname || "This computer")}</strong><span>${esc(c.os || "")} · local panel</span></div><div><span class="cc-eyebrow">Request router</span><strong>${esc(router.machine === "local" ? "This computer" : router.machine || "Not configured")}</strong><span>Configured placement · ${esc(router.listen || "endpoint not configured")}</span></div><div><span class="cc-eyebrow">Compute nodes</span><strong>${(d.nodes || []).filter(n => n.access !== "local").length} linked</strong><span>Workers are controlled over SSH</span></div></div>`;
  }
  function steps() {
    const rows = doc().checklist || [];
    return `<ol class="cc-checklist">${rows.map((s,i) => `<li class="cc-step cc-step-${esc(s.state)}"><span class="cc-step-number" aria-hidden="true">${s.state === "done" ? "✓" : i + 1}</span><div><div class="row"><strong>${esc(s.title)}</strong><span class="cc-step-state">${esc(({done:"Complete",next:"Next step",pending:"Pending",blocked:"Needs attention"})[s.state] || s.state)}</span></div><p>${esc(s.detail || "")}</p></div><div class="cc-step-action">${s.state === "done" ? "" : s.action === "setup/initialize" ? action("Set up workspace", "initialize", {}, "", true) : s.id === "machines" || s.id === "nodes" ? action("Add node", "add-node", {}, H().mutationReason(), true) : s.href ? link("Open", s.id === "trust" ? "#/ops?tab=config" : s.href, s.state === "next") : ""}</div></li>`).join("")}</ol>`;
  }
  function nodes() {
    const d = doc(), observed = H().machines();
    const remote = (d.nodes || []).filter(n => n.access !== "local");
    if (!remote.length) return `<div class="cc-empty-inline"><strong>No compute nodes yet</strong><p>Add a machine to run a model across this workspace.</p>${action("Add a machine", "add-node", {}, H().mutationReason(), true)}</div>`;
    return `<div class="cc-node-list">${remote.map(n => {
      const live = observed.find(m => m.id === n.id) || {}, trust = ["pinned", "paired"].includes(n.trust);
      return `<article class="cc-node"><div><a class="cc-node-name" href="#/machine?id=${encodeURIComponent(n.id)}">${esc(n.id)}</a><div class="hint">${esc(n.http_address || n.address || n.ssh_target || n.access)}</div></div><div>${H().statusPill(live.health || "unknown")}<div class="hint">${n.trust === "paired" ? "Agent paired" : trust ? "SSH identity verified" : "Verify SSH identity"}</div></div><div class="cc-node-actions">${trust ? action("Check node", "test-node", {machine:n.id}, H().mutationReason()) : action("Verify host key", "trust-node", {machine:n.id}, H().mutationReason())}${action("Edit", "edit-node", {machine:n.id}, H().mutationReason())}</div></article>`;
    }).join("")}</div>`;
  }
  function discovered() {
    const d = H().state.docs.discovery || {}, rows = d.nodes || [];
    const pairText = (d.pairs || []).map(p => `<p class="hint">Connection available: ${p.map(esc).join(" + ")}</p>`).join("");
    const sha256 = (typeof document.querySelector === "function" && document.querySelector('meta[name="hydraweave-package-sha256"]')?.content) || "";
    const bootstrap = sha256 ? `<p>On each worker: <code>curl -fsSL ${esc(typeof location === "undefined" ? "" : location.origin)}/node/bootstrap.sh | HYDRAWEAVE_PKG_SHA256=${esc(sha256)} sh</code></p><p class="hint">Package SHA-256: <code>${esc(sha256)}</code></p>` : "<p>Open the live portal to get the verified node bootstrap command.</p>";
    return H().card("Discovered on your LAN", `${bootstrap}${d.error ? `<p role="alert">${esc(d.error)}</p>` : ""}${pairText}${rows.map(n => {
      const profiles = (doc().profiles || []).filter(p => (p.machines || p.nodes || []).includes(n.machine));
      return `<article class="cc-node"><div><strong>${esc(n.hostname || n.id)}</strong><p class="hint">${esc(n.state)} · ${esc(n.gpu_arch)} · ${esc(n.address)} · agent ${esc(n.agent_version || "unknown")}${n.version_status === "outdated" ? ` <span class="pill p-warn" title="Agent version differs from this portal; rerun the bootstrap one-liner on the node">Out of date</span>` : ""}</p></div>${n.state === "adopted" && (n.version_status === "outdated" || n.version_mismatch) ? action("Update node", "update-node", {machine:n.machine}, H().mutationReason()) : ""}${n.state !== "adopted" ? action("Adopt", "adopt", {id:n.id}) : profiles.map(p => action(`Install ${p.profile || p.name || p.id}`, "provision", {machine:n.machine,profile:p.profile || p.name || p.id})).join("")}</article>`;
    }).join("") || "<p>Listening for nodes. Discovery can take a few seconds.</p>"}<p class="hint">Provisioning progress appears in Jobs.${(d.nodes || []).some(n => n.lab_mode === true) ? " Engines use configured GPU slots when launched." : ""}</p>`);
  }
  function render() {
    const d = doc(), cfg = d.configuration || {}, ready = d.configured === true || cfg.state === "valid";
    const checklist = d.checklist || [], completed = checklist.filter(s => s.state === "done").length;
    const setupDone = [cfg.state === "valid", (d.nodes || []).some(n => n.access !== "local"), (() => { try { return !!localStorage.getItem("hydraweave-setup-model"); } catch (_) { return false; } })() || (d.profiles || []).some(p => p.state === "running"),
      (d.profiles || []).some(p => p.state === "running")].filter(Boolean).length;
    if ((cfg.state === "missing" || (checklist.length > 0 && completed < checklist.length)) && !wizardLeft) {
      const names = ["This computer", "Nodes", "Model", "Start"];
      const discovery = H().state.docs.discovery || {};
      let content = wizardStep === 0 ? (cfg.state === "missing" ? `<p>Create the workspace on this computer to continue.</p>${action("Create workspace", "initialize", {}, "", true)}` : `<p>Workspace created. Run the local check, then continue.</p>${steps()}${action("Check again", "wizard-check")}${action("Continue", "wizard-next", {}, "", true)}`) : "";
      if (wizardStep === 1) { const found = (discovery.nodes || []).filter(n => n.state !== "adopted"); content = (found.length ? `<p>Found on your network: ${found.map(n => `${esc(n.hostname || n.id)} ${action("Add", "adopt", {id:n.id})}`).join(" · ")}</p>` : `<p>No additional machines found.</p>`) + action("Add a machine", "add-node") + action("I only have this computer", "wizard-next", {}, "", true); }
      if (wizardStep === 2) { const rows = presets(); content = rows.length ? `<ul class="cc-engine-list">${rows.map(p => { const unavailable = p.qualified === false; return `<li><button type="button" class="${unavailable ? "cc-model-unavailable" : ""}" ${unavailable ? "disabled" : `data-page-action="cc-wizard-model" data-model="${esc(p.id)}"`}><strong>${esc(p.label || p.id)}</strong><span>${esc(unavailable ? (p.notes || "Not qualified for this setup") : "Available preset")}</span></button></li>`; }).join("")}</ul>` : `<p>No model presets are available.</p>${link("Open engine settings", "#/engine-settings", true)}`; content += action("Continue", "wizard-next", {}, "", true); }
      if (wizardStep === 3) content = `<ol>${(d.start_status?.steps || checklist).map(s => `<li>${esc(s.title || s.name || s.id)} · ${esc(s.state || "Pending")}</li>`).join("")}</ol>${link("Open Chat", "#/chat", true)} <button type="button" data-page-action="cc-copy-api">Copy API address</button>`;
      return `<section class="cc-welcome"><div><span class="cc-eyebrow">Setup · Step ${wizardStep + 1} of 4</span><h2>Welcome. Let’s get a model running.</h2></div></section><section class="card cc-wizard"><nav aria-label="Setup steps">${names.map((n,i)=>`<span${i===wizardStep?' aria-current="step"':''}>${i+1}. ${n}</span>`).join(" · ")}</nav><h2>${names[wizardStep]}</h2>${content}${wizardStep ? action("Back", "wizard-back") : ""}${action("Leave setup", "wizard-leave")}</section>`;
    }
    const header = `<section class="cc-welcome"><div><span class="cc-eyebrow">Home / Control center</span><h2>Your machines and models.</h2><p>Machines and connections reported by this workspace.</p></div>${link("Open engines", "#/engines", true)}</section>`;
    const count = (d.nodes || []).length;
    const lab = (d.nodes || []).some(n => n.lab_mode === true);
    const summary = `<div class="cc-summary" aria-label="Cluster summary"><div><span class="cc-summary-icon" aria-hidden="true">${mark("engine")}</span><div><span class="cc-eyebrow">Serving</span><strong>${esc(servingStatus())}</strong></div></div>${lab ? `<div><span class="cc-summary-icon" aria-hidden="true">${mark("turns")}</span><div><span class="cc-eyebrow">GPU slots</span><strong>Status available in slot details</strong></div></div>` : ""}<div><span class="cc-summary-icon" aria-hidden="true">${mark("node")}</span><div><span class="cc-eyebrow">Workspace</span><strong>${count} ${count === 1 ? "machine" : "machines"} configured</strong></div></div></div>`;
    const invalid = cfg.state === "invalid" ? `<div class="banner warn" role="alert"><div><strong>The saved configuration needs attention</strong><p>${esc(cfg.error || "Validation failed")}</p><span class="mono">${esc(cfg.path)}</span></div></div>` : "";
    const initial = cfg.state === "missing" ? H().card("Start with this computer", `<p>Create the controller workspace, then add your compute nodes. No engine is started during setup.</p>${action("Create workspace", "initialize", {}, "", true)}<p class="hint">Configuration: <span class="mono">${esc(cfg.path)}</span></p>`) : "";
    const progress = ready ? `<div class="cc-main-grid">${H().card("Getting ready", steps(), "A guided path from installation to inference")}${H().card("Quick access", `<div class="cc-shortcuts"><a href="#/components"><strong>Components & updates</strong><span>Check versions and prepare downloads</span></a><a href="#/engine-settings"><strong>Engine configuration</strong><span>Profiles, model files and supported options</span></a><a href="#/recipes"><strong>Run a model</strong><span>Choose and deploy an inference recipe</span></a><a href="#/overview"><strong>Live monitoring</strong><span>Node health, memory and throughput</span></a></div>`)}</div>` : "";
    const lifecycle = d.lifecycle || {};
    const next = `<section class="card cc-next" aria-labelledby="cc-next-title"><span class="cc-eyebrow">Keep things moving</span><h2 id="cc-next-title">Next steps</h2>${lab ? `<a class="cc-next-link" href="#/turns"><strong>Review GPU slots</strong><span>See current slot status</span></a>` : ""}<a class="cc-next-link" href="#/components"><strong>Check components</strong><span>Review versions and node tools</span></a><button type="button" class="cc-next-link" data-page-action="cc-manage-nodes"><strong>Manage nodes</strong><span>Connections and SSH identities</span></button></section>`;
    const resume = (cfg.state === "missing" || (checklist.length > 0 && completed < checklist.length)) ? H().card(`Setup: ${setupDone} of 4 done`, `<p>Continue the setup wizard where you left off.</p>${action("Continue", "wizard-resume", {}, "", true)}`) : "";
    return header + invalid + resume + initial + summary + weave() + discovered() + `<div class="cc-home-grid">${engineCatalog()}${next}</div>`
      + `<div class="cc-setup-heading" id="cc-node-management" tabindex="-1"><span class="cc-eyebrow">Workspace management</span><h2>Keep your cluster ready.</h2></div>` + controller() + (notice ? `<p class="cc-notice" role="status">${esc(notice)}</p>` : "") + progress
      + (ready ? H().card("Your nodes", nodes() + `<div class="cc-card-footer">${action("Add node", "add-node", {}, H().mutationReason())}${link("All machines", "#/devices")}${link("Topology & links", "#/topology")}</div>`, "Connected explicitly; status comes from each machine") : "")
      + H().card("Where does Hydraweave run?", `<p>The panel runs on <strong>${esc(d.controller?.hostname || "this computer")}</strong>. The request router can run here or on a configured Linux node. The engine's rank 0 is a compute role and does not have to host the panel.</p><p class="hint">${esc(typeof d.recommendation === "string" ? d.recommendation : d.recommendation?.detail || d.recommendation?.note || "For regular use, choose an always-on controller. A laptop can connect to it through an SSH tunnel.")}</p>${lifecycle.note ? `<div class="cc-lifecycle"><strong>${esc(lifecycle.mode || "Process lifecycle")}</strong><p>${esc(lifecycle.note)}</p></div>` : ""}${ready ? action("Choose router location", "router", {}, H().mutationReason()) : ""}`);
  }

  function addNodeDialog(selected = null) {
    const found = ((H().state.docs.discovery || {}).nodes || []).filter(n => n.state !== "adopted");
    const manual = `<p class="hint">Not listed? Run the node bootstrap command on the worker and check again, or connect to it over SSH.</p><button type="button" class="btn" data-page-action="cc-add-manual" data-ops-close>Add manually over SSH</button>`;
    if (!found.length) return {title:"Add node", confirm:"Check again", body:`<p>No new nodes found on your network yet.</p>${manual}`, submit:null};
    const pick = found.some(n => n.id === selected) ? selected : found[0].id;
    const rows = found.map(n => `<label class="cc-found"><input type="radio" name="cc-adopt-id" value="${esc(n.id)}"${n.id === pick ? " checked" : ""}> <strong>${esc(n.hostname || n.id)}</strong> <span class="hint">${[n.address, n.gpu_arch].filter(Boolean).map(esc).join(" · ")}</span></label>`).join("");
    return {title:"Add node", confirm:"Adopt", body:`<p>Found on your network. Choose the node and enter the pairing code shown on its console.</p><fieldset class="field"><legend>Node</legend>${rows}</fieldset><label class="field" for="cc-pair-code">Pairing code<input class="inp" id="cc-pair-code" inputmode="numeric" autocomplete="off"></label>${manual}`, submit:"adopt"};
  }
  function openAddNode(selected = null) {
    const d = addNodeDialog(selected);
    UI().open({title:d.title, confirm:d.confirm, body:d.body, submit:async form => {
      if (!d.submit) { await H().refresh(); openAddNode(); return true; }
      const id = form.querySelector('input[name="cc-adopt-id"]:checked')?.value, code = form.querySelector("#cc-pair-code")?.value.trim();
      if (!code) throw new Error("Enter the pairing code shown on the node console.");
      const result = await UI().post("discovery/adopt", {id, code});
      notice = `Adopted ${result.adopted}${result.provisioning_error ? ` · ${result.provisioning_error}` : ""}`;
    }});
  }
  async function openMachine(id = null) {
    const cfg = await H().request("ops/config");
    if (!Array.isArray(cfg.machines) || !cfg.version) throw new Error("Create a valid workspace first on Home.");
    const row = id ? cfg.machines.find(m => m.id === id) : null;
    if (id && !row) throw new Error("This machine is no longer configured. Refresh and try again.");
    const e = row?.entry || {os:"linux", access:"ssh", ssh_port:22, lab_mode:false};
    const frozen = row?.frozen || "", lock = frozen ? `disabled title="${esc(frozen)}"` : "";
    const appendPeersOnly = !!frozen && row?.add_peer_aliases === true;
    const input = (key,label,value,placeholder="",type="text") => `<label class="field" for="cc-node-${key}">${esc(label)}<input class="inp" id="cc-node-${key}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" spellcheck="false" ${lock}></label>`;
    const peers = Object.entries(e.peer_ssh || {}).map(([mid,alias]) => `${mid}=${alias}`).join("\n");
    UI().open({title:id ? `Edit ${id}` : "Add compute node", confirm:appendPeersOnly ? "Save peer aliases" : frozen ? "Close" : id ? "Save node" : "Add node", body:
      `<p class="hint">Use a private network address and an SSH account that already has access to the machine. You will verify its host key after adding it.</p>${frozen ? `<p class="banner warn">${esc(frozen)}</p>` : ""}${appendPeersOnly ? '<p class="hint">You can add aliases for newly configured peer nodes. Existing aliases cannot be changed or removed. Saving records the configuration; it does not test SSH or change running processes.</p>' : ""}<div class="two">${input("id","Node name",e.id || "","cluster2")}${input("address","Private IP address",e.http_address || "","192.168.1.20")}</div><div class="two">${input("ssh","SSH account or configured alias",e.ssh_target || "","user@192.168.1.20")}${input("root","Install directory on this node",e.install_root || "","/home/user/hydraweave-node")}</div>
      <div class="cc-lifecycle"><label class="check"><input type="checkbox" id="cc-node-lab" ${e.lab_mode ? "checked" : ""} ${lock}>Use the existing lab slot manager</label><div id="cc-node-lab-fields" ${e.lab_mode ? "" : "hidden"}><p>Start profile uses GPU slots from this node’s lab directory. It must already contain the slot manager scripts.</p>${input("labdir","Lab directory on node (required for Start profile)",e.lab_dir || "","/home/user/gpu-lab")}</div><p>Without lab mode, you can register the node for monitoring.</p></div>
      <label class="field" for="cc-node-gpus">GPU identifiers (one per line, optional)<textarea class="inp" id="cc-node-gpus" rows="2" spellcheck="false" placeholder="gpu0" ${lock}>${esc((e.devices || []).map(g => g.id).join("\n"))}</textarea><span class="hint">Register only the devices you want Hydraweave to use. Hardware checks report their actual capabilities.</span></label>
      <details ${appendPeersOnly ? "open" : ""}><summary>Advanced connection settings</summary><div class="cc-form-stack"><div class="two"><label class="field" for="cc-node-os">System<select class="inp" id="cc-node-os" ${lock}>${["linux","windows"].map(v => `<option value="${v}" ${e.os === v ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label><label class="field" for="cc-node-access">Connection<select class="inp" id="cc-node-access" ${lock}>${["ssh","local"].map(v => `<option value="${v}" ${e.access === v ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label></div>${input("port","SSH port",e.ssh_port ?? 22,"22","number")}<label class="field" for="cc-node-peers">Peer SSH aliases (one node=alias per line)<textarea class="inp" id="cc-node-peers" rows="2" spellcheck="false" placeholder="cluster2=cluster2" ${appendPeersOnly ? "" : lock}>${esc(peers)}</textarea><span class="hint">Used for direct transfers between already configured nodes.</span></label></div></details>`,
      submit: async () => {
        if (frozen && !appendPeersOnly) return;
        const value = key => document.getElementById(`cc-node-${key}`).value.trim();
        const peers = {};
        for (const line of value("peers").split(/\r?\n/).filter(s => s.trim())) {
          const match = line.match(/^\s*([^=\s]+)\s*=\s*([^=\s]+)\s*$/);
          if (!match || Object.hasOwn(peers,match[1])) throw new Error("Use one unique node=alias pair per line.");
          peers[match[1]] = match[2];
        }
        let machine;
        if (appendPeersOnly) {
          const existing = e.peer_ssh || {};
          if (Object.entries(existing).some(([mid,alias]) => peers[mid] !== alias)) throw new Error("Existing peer aliases cannot be changed or removed while this node is frozen. Add new destinations only.");
          if (!Object.keys(peers).some(mid => !Object.hasOwn(existing,mid))) throw new Error("Add at least one new peer alias before saving.");
          // Preserve the original entry byte-for-value except for added aliases;
          // disabled form defaults must not become identity changes.
          machine = {...e,peer_ssh:peers};
        } else {
          machine = {...e,id:value("id"),os:value("os"),access:value("access"),http_address:value("address"),install_root:value("root"),ssh_port:Number(value("port")),lab_mode:document.getElementById("cc-node-lab").checked,peer_ssh:peers};
          if (!machine.id || !machine.http_address || !machine.install_root) throw new Error("Enter a node name, private IP address and absolute installation directory.");
          if (machine.access === "ssh" && !value("ssh")) throw new Error("Enter the SSH account or alias for this node.");
          if (machine.lab_mode && !value("labdir")) throw new Error("Enter the existing lab directory that contains gpu-turn.sh, or turn off lab mode to register the node without profile launch support.");
          if (value("ssh")) machine.ssh_target = value("ssh"); else delete machine.ssh_target;
          if (value("labdir")) machine.lab_dir = value("labdir"); else delete machine.lab_dir;
          const ids = value("gpus").split(/\r?\n/).map(s => s.trim()).filter(Boolean);
          if (new Set(ids).size !== ids.length) throw new Error("Each GPU identifier must be unique.");
          machine.devices = ids.map(gid => ({...(e.devices || []).find(g => g.id === gid),id:gid}));
        }
        const payload = id ? {op:"edit",id,machine,version:cfg.version} : {op:"add",machine,version:cfg.version};
        const checked = await UI().post("ops/config/validate",payload);
        if (!checked.valid) throw new Error(checked.error || "Node configuration is not valid.");
        await UI().post("ops/config/save",payload);
        notice = appendPeersOnly ? `Peer aliases saved for ${machine.id}. SSH connectivity has not been verified.` : `Node ${machine.id} saved. Verify its SSH host key and run a node check next.`;
        H().announce(notice);
      },
    });
  }
  window.HydraweaveNodesUI = {open:openMachine};
  if (typeof document.addEventListener === "function") document.addEventListener("change", event => { if (event.target?.id === "cc-node-lab" && typeof document.getElementById === "function") { const fields = document.getElementById("cc-node-lab-fields"); if (fields) fields.hidden = !event.target.checked; } });

  async function trustNode(mid) {
    const scan = await UI().post("ops/hostkey/scan",{machine:mid});
    UI().open({title:`Verify ${mid}`,confirm:"It matches · trust this node",body:`<p>Compare this fingerprint with the one shown on the node. Select the matching fingerprint to confirm.</p><ul class="cc-fingerprints">${(scan.candidates || []).map((c,i) => `<li><label class="check"><input type="radio" name="cc-fingerprint" value="${i}"><b>${esc(c.algorithm)}</b> <code>${esc(c.fingerprint)}</code></label></li>`).join("")}</ul><details><summary>Details</summary><p>The fingerprint is shown read-only for comparison.</p></details>`,submit:async () => {
      const index = Number(document.querySelector('input[name="cc-fingerprint"]:checked')?.value);
      const fingerprint = scan.candidates?.[index]?.fingerprint;
      if (!fingerprint) throw new Error("Select the fingerprint that matches the node.");
      await H().mutate("hosts/pin",{machine:mid,fingerprint});
    }});
  }
  async function routerDialog() {
    const setup = await H().request("setup"), cfg = setup.configuration || {}, router = setup.router || {};
    const candidates = (setup.nodes || []).filter(n => n.access === "ssh" && n.os === "linux");
    const port = String(router.listen || "").split(":").at(-1);
    UI().open({title:"Router placement",confirm:"Save for next start",body:`<p>The router receives API requests and forwards them to compute workers. The panel stays on this controller.</p><label class="field" for="cc-router-machine">Run the router on<select class="inp" id="cc-router-machine"><option value="local" ${router.machine === "local" ? "selected" : ""}>This computer</option>${candidates.map(n => `<option value="${esc(n.id)}" ${router.machine === n.id ? "selected" : ""}>${esc(n.id)} · Linux node</option>`).join("")}</select></label><label class="field" for="cc-router-port">Port<input class="inp" id="cc-router-port" type="number" min="1" max="65535" value="${/^\d+$/.test(port) ? esc(port) : "18701"}"></label><p class="hint">This changes the saved launch configuration. Running profiles must be stopped before it can be saved.</p>`,submit:async () => {
      const args = {version:cfg.version,machine:document.getElementById("cc-router-machine").value,port:Number(document.getElementById("cc-router-port").value)};
      const check = await UI().post("setup/router/validate",args);
      if (check.valid === false) throw new Error(check.error || "Router placement is not valid.");
      await UI().post("setup/router/save",args);
      notice = "Router location saved. It applies on the next profile start."; H().announce(notice);
    }});
  }
  document.addEventListener("click",async event => {
    const button = event.target.closest('[data-page-action^="cc-"]');
    if (!button || button.disabled) return;
    try {
      const a = button.dataset.pageAction, mid = button.dataset.machine;
      if (a === "cc-manage-nodes") { const target = document.getElementById("cc-node-management"); target?.scrollIntoView({block:"start"}); target?.focus({preventScroll:true}); }
      else if (a === "cc-initialize") { button.disabled = true; await UI().post("setup/initialize",{}); setWizardStep(0); notice="Workspace created."; H().announce(notice); await H().refresh(); }
      else if (a === "cc-wizard-next") { setWizardStep(wizardStep + 1); H().render(); }
      else if (a === "cc-wizard-back") { setWizardStep(wizardStep - 1); H().render(); }
      else if (a === "cc-wizard-check") { await H().refresh(); }
      else if (a === "cc-wizard-model") { try { localStorage.setItem("hydraweave-setup-model", button.dataset.model || ""); } catch (_) {} setWizardStep(3); H().render(); }
      else if (a === "cc-wizard-leave") { wizardLeft = true; try { localStorage.setItem("hydraweave-setup-left", "true"); } catch (_) {} H().render(); }
      else if (a === "cc-wizard-resume") { wizardLeft = false; try { localStorage.setItem("hydraweave-setup-left", "false"); } catch (_) {} H().render(); }
      else if (a === "cc-copy-api") { const address = `${location.origin}/v1`; if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(address); }
      else if (a === "cc-add-node") openAddNode();
      else if (a === "cc-add-manual") await openMachine();
      else if (a === "cc-adopt") openAddNode(button.dataset.id);
      else if (a === "cc-update-node") await H().mutate("discovery/update", {machine:mid,origin:window.location.origin});
      else if (a === "cc-provision") await H().mutate("discovery/provision", {machine:mid,profile:button.dataset.profile});
      else if (a === "cc-edit-node") await openMachine(mid);
      else if (a === "cc-trust-node") await trustNode(mid);
      else if (a === "cc-test-node") await H().mutate("ops/machine-test",{machine:mid});
      else if (a === "cc-router") await routerDialog();
    } catch (error) { notice=error.message; H().announce(notice); H().render(); }
    finally { if (button.isConnected) button.disabled=false; }
  });
  (window.HydraweavePages ||= {})["control-center"] = {
    title:"Home",group:"Control center",order:1,home:true,
    icon:'<path d="M3 9l7-6 7 6v8h-5v-5H8v5H3z"/>',
    endpoints:() => ["setup", "discovery", "engine-settings", "topology"],
    ready:() => !!H().state.docs.setup,
    error:() => H().state.errors.setup || "",render,
  };
})();
