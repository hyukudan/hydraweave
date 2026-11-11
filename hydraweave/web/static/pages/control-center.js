/* Guided control plane, node onboarding and explicit router placement. */
"use strict";
(() => {
  const H = () => window.Hydraweave;
  const UI = () => window.HydraweaveOpsUI;
  const doc = () => H().state.docs.setup || {};
  const esc = value => H().esc(value ?? "");
  let notice = "";
  const action = (label, name, data = {}, disabled = "", primary = false) => H().button(label, "", {
    style: primary ? "pri" : "", disabled,
    extra: `data-page-action="cc-${name}" ${Object.entries(data).map(([k,v]) => `data-${k}="${esc(v)}"`).join(" ")}`,
  });
  const link = (label, href, primary = false) => `<a class="btn${primary ? " pri" : ""}" href="${esc(href)}">${esc(label)}</a>`;
  const mark = kind => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${({engine:"M7 7h10v10H7z M4 9h3m-3 6h3m10-6h3m-3 6h3M9 4v3m6-3v3M9 17v3m6-3v3",turns:"m3 8 9-5 9 5-9 5z M3 12l9 5 9-5M3 16l9 5 9-5",node:"M4 4h16v6H4z M4 14h16v6H4z M7 7h1m3 0h6M7 17h1m3 0h6",pc:"M3 4h18v13H3z M9 21h6m-3-4v4"})[kind]}"></path></svg>`;

  // Presentation of existing read documents only. No launch or queue mutation lives on Home.
  const fresh = name => H().state.fresh && !H().state.errors[name] && H().state.fetched[name]
    && Date.now() - H().state.fetched[name] <= Math.max(10000, H().prefs().refresh_s * 3000);
  const catalog = [
    ["Qwen3.8", "Two replicas", /qwen/i],
    ["DeepSeek V4.1", "Pair", /deepseek/i],
    ["MiniMax M3.1", "Pair", /minimax.*m3|m3.*minimax/i],
    ["GLM-5.3", "Pair", /glm/i],
    ["MiniMax-H3-Image", "Image", /minimax.*h3|h3.*image/i],
  ];
  function servingStatus() {
    if (!fresh("profiles") || !Array.isArray(H().state.docs.profiles?.profiles)) return "Engine status unavailable";
    const profiles = H().profiles();
    const serving = profiles.filter(p => p.state === "running");
    if (serving.length) return `${serving.length} ${serving.length === 1 ? "engine" : "engines"} serving`;
    if (profiles.some(p => !["stopped", "idle", "not installed", "unavailable", "disabled"].includes(p.state))) return "Check engine state";
    return "No engine serving";
  }
  function turnState(machine) {
    const turns = H().state.docs.turns || {}, node = (turns.nodes || []).find(n => n.machine === machine?.id);
    if (!fresh("turns") || !node || node.state !== "ok" || !Number.isFinite(node.age_s) || node.age_s > 90) return {text:"Turn status unavailable"};
    const holder = node.holder;
    if (!holder) return {text:"No GPU turn held"};
    const confirmed = holder.parsed && holder.alive === true && holder.verified === true && !holder.past_cap
      && (!holder.pair_state || holder.pair_state.startsWith("running (confirmed"));
    return confirmed ? {held:true,text:"GPU turn held now",label:holder.label} : {text:"Turn holder needs review"};
  }
  function weave() {
    const observed = H().machines(), configured = doc().nodes || [];
    const pc = observed.find(m => m.os === "windows" && H().rows(m.accelerators).some(g => !g.excluded && /RTX PRO 6000/i.test(g.name || "")));
    // The two Strix Halo threads are the first two configured Linux nodes (any names); the controller's own one first.
    const linux = configured.filter(n => (n.os || observed.find(m => m.id === n.id)?.declared_os) !== "windows");
    const controllerId = doc().controller?.machine || doc().controller?.id;
    linux.sort((a, b) => (b.id === controllerId || b.access === "local") - (a.id === controllerId || a.access === "local"));
    const nodeSlot = (node, index) => ({id:node?.id, label:node?.id || `Node ${index + 1}`,
      detail:(node && (node.id === controllerId || node.access === "local") ? "Controller · " : "") + "AMD Strix Halo"});
    const slots = [nodeSlot(linux[0], 0), nodeSlot(linux[1], 1),
      {id:pc?.id,label:"RTX PC",detail:"Optional · Windows · RTX PRO 6000"}];
    const pairIds = slots.slice(0, 2).map(s => s.id).filter(Boolean);
    const links = H().rows(H().state.docs.topology?.links);
    const usb = links.filter(l => l.kind === "USB4" && pairIds.includes(l.from) && pairIds.includes(l.to));
    const usbNote = !fresh("topology") || !usb.length ? "Link status unavailable" : usb.every(l => ["up","ok"].includes(l.health)) ? "Reported up" : "Link needs review";
    const lan = links.filter(l => l.kind === "Ethernet" && slots.some(s => s.id === l.from));
    return `<section class="card cc-weave" aria-labelledby="cc-weave-title"><div class="cc-section-head"><div><span class="cc-eyebrow">Machines & connections</span><h2 id="cc-weave-title">The weave</h2></div><a class="cc-text-link" href="#/topology">Topology & links <span aria-hidden="true">↗</span></a></div>
      <div class="cc-weave-map"><div class="cc-thread-links" aria-hidden="true"><div class="cc-usb-thread"></div><div class="cc-lan-thread"></div></div>
      <ul class="cc-threads">${slots.map(s => {
        const machine = observed.find(m => m.id === s.id), config = configured.find(n => n.id === s.id);
        const turn = turnState(machine), known = !!(machine || config);
        const health = fresh("devices") && machine ? machine.health || "Status unavailable" : known ? "Status unavailable" : s.label === "RTX PC" ? "Optional · not configured" : "Not configured";
        return `<li class="cc-thread${turn.held ? " cc-thread-held" : ""}"><div class="cc-machine-head"><span class="cc-machine-icon" aria-hidden="true">${mark(s.label === "RTX PC" ? "pc" : "node")}</span>${known ? `<a class="cc-machine-name" href="#/machine?id=${encodeURIComponent(s.id)}">${esc(s.label)}</a>` : `<strong class="cc-machine-name">${esc(s.label)}</strong>`}<span class="cc-machine-detail">${esc(s.detail)}</span><span class="cc-machine-health">${esc(health)}</span></div><div class="cc-machine-thread" aria-hidden="true"></div><div class="cc-turn${turn.held ? " cc-turn-held" : ""}"><strong>${esc(turn.text)}</strong>${turn.label ? `<span>${esc(turn.label)}</span>` : ""}</div></li>`;
      }).join("")}</ul><div class="cc-usb-label"><strong>USB4 <span>· 2 cables / 4 streams</span></strong><span>${esc(usbNote)} · configured link</span></div><div class="cc-lan-label">LAN <span>· ${fresh("topology") && lan.length ? "Interface observations available" : "Status unavailable"}</span></div></div>
      <div class="cc-weave-caption"><span><i class="cc-key-usb" aria-hidden="true"></i>USB4 between Strix Halo nodes</span><span><i class="cc-key-turn" aria-hidden="true"></i>Amber marks a verified GPU turn held now</span><a href="#/turns">Read the GPU queue <span aria-hidden="true">↗</span></a></div></section>`;
  }
  function engineCatalog() {
    return `<section class="card cc-catalog" aria-labelledby="cc-catalog-title"><div class="cc-section-head"><div><span class="cc-eyebrow">Inference</span><h2 id="cc-catalog-title">Engines</h2></div><a class="cc-text-link" href="#/engines">Browse engines <span aria-hidden="true">↗</span></a></div><ul class="cc-engine-list">${catalog.map(([name,mode,match]) => {
      const profile = H().profiles().find(p => match.test(`${p.family || ""} ${p.label || ""} ${H().profileId(p)}`));
      const href = profile ? `#/engines?profile=${encodeURIComponent(H().profileId(profile))}` : "#/engine-settings";
      return `<li><a href="${esc(href)}"><strong>${esc(name)}</strong><span>${esc(mode)}</span><span class="cc-engine-state">${profile ? esc(fresh("profiles") ? profile.state || "State unavailable" : "State unavailable") : name === "GLM-5.3" ? "Needs a preset" : "Not configured"}</span><span aria-hidden="true">↗</span></a></li>`;
    }).join("")}</ul><p class="cc-footnote">Catalog options · configured profiles open their current engine details.</p></section>`;
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
    if (!remote.length) return `<div class="cc-empty-inline"><strong>Add your first compute node</strong><p>Connect a Strix Halo machine using its address and SSH account. Its engine runs on that node.</p>${action("Add compute node", "add-node", {}, H().mutationReason(), true)}</div>`;
    return `<div class="cc-node-list">${remote.map(n => {
      const live = observed.find(m => m.id === n.id) || {}, trust = ["pinned", "paired"].includes(n.trust);
      return `<article class="cc-node"><div><a class="cc-node-name" href="#/machine?id=${encodeURIComponent(n.id)}">${esc(n.id)}</a><div class="hint">${esc(n.http_address || n.address || n.ssh_target || n.access)}</div></div><div>${H().statusPill(live.health || "unknown")}<div class="hint">${n.trust === "paired" ? "Agent paired" : trust ? "SSH identity verified" : "Verify SSH identity"}</div></div><div class="cc-node-actions">${trust ? action("Check node", "test-node", {machine:n.id}, H().mutationReason()) : action("Verify host key", "trust-node", {machine:n.id}, H().mutationReason())}${action("Edit", "edit-node", {machine:n.id}, H().mutationReason())}</div></article>`;
    }).join("")}</div>`;
  }
  function discovered() {
    const d = H().state.docs.discovery || {}, rows = d.nodes || [];
    const pairText = (d.pairs || []).map(p => `<p class="hint">USB4 pair available: ${p.map(esc).join(" + ")}</p>`).join("");
    return H().card("Discovered on your LAN", `<p>On each worker: <code>curl -fsSL http://&lt;controller&gt;:18700/node/bootstrap.sh | sh</code></p>${d.error ? `<p role="alert">${esc(d.error)}</p>` : ""}${pairText}${rows.map(n => {
      const profiles = (doc().profiles || []).filter(p => (p.machines || p.nodes || []).includes(n.machine));
      return `<article class="cc-node"><div><strong>${esc(n.hostname || n.id)}</strong><p class="hint">${esc(n.state)} · ${esc(n.gpu_arch)} · ${esc(n.address)}</p></div>${n.state !== "adopted" ? action("Adopt", "adopt", {id:n.id}) : profiles.map(p => action(`Install ${p.profile || p.name || p.id}`, "provision", {machine:n.machine,profile:p.profile || p.name || p.id})).join("")}</article>`;
    }).join("") || "<p>Listening for nodes. Discovery can take a few seconds.</p>"}<p class="hint">Provisioning progress appears in Jobs. Engines use the existing GPU queue when launched.</p>`);
  }
  function render() {
    const d = doc(), cfg = d.configuration || {}, ready = d.configured === true || cfg.state === "valid";
    const header = `<section class="cc-welcome"><div><span class="cc-eyebrow">Home / Control center</span><h2>Your cluster, woven together.</h2><p>Two Strix Halo nodes. An optional RTX PC. One place to see how they connect.</p></div>${link("Open engines", "#/engines", true)}</section>`;
    const count = (d.nodes || []).length;
    const summary = `<div class="cc-summary" aria-label="Cluster summary"><div><span class="cc-summary-icon" aria-hidden="true">${mark("engine")}</span><div><span class="cc-eyebrow">Serving</span><strong>${esc(servingStatus())}</strong></div></div><div><span class="cc-summary-icon" aria-hidden="true">${mark("turns")}</span><div><span class="cc-eyebrow">GPU turns</span><strong>Queue is read only</strong></div></div><div><span class="cc-summary-icon" aria-hidden="true">${mark("node")}</span><div><span class="cc-eyebrow">Workspace</span><strong>${count} ${count === 1 ? "machine" : "machines"} configured</strong></div></div></div>`;
    const invalid = cfg.state === "invalid" ? `<div class="banner warn" role="alert"><div><strong>The saved configuration needs attention</strong><p>${esc(cfg.error || "Validation failed")}</p><span class="mono">${esc(cfg.path)}</span></div></div>` : "";
    const initial = cfg.state === "missing" ? H().card("Start with this computer", `<p>Create the controller workspace, then add your compute nodes. No engine is started during setup.</p>${action("Create workspace", "initialize", {}, "", true)}<p class="hint">Configuration: <span class="mono">${esc(cfg.path)}</span></p>`) : "";
    const progress = ready ? `<div class="cc-main-grid">${H().card("Getting ready", steps(), "A guided path from installation to inference")}${H().card("Quick access", `<div class="cc-shortcuts"><a href="#/components"><strong>Components & updates</strong><span>Check versions and prepare downloads</span></a><a href="#/engine-settings"><strong>Engine configuration</strong><span>Profiles, model files and supported options</span></a><a href="#/recipes"><strong>Run a model</strong><span>Choose and deploy an inference recipe</span></a><a href="#/overview"><strong>Live monitoring</strong><span>Node health, memory and throughput</span></a></div>`)}</div>` : "";
    const lifecycle = d.lifecycle || {};
    const next = `<section class="card cc-next" aria-labelledby="cc-next-title"><span class="cc-eyebrow">Keep things moving</span><h2 id="cc-next-title">Next steps</h2><a class="cc-next-link" href="#/turns"><strong>Review GPU turns</strong><span>See holders and waiting tickets <b aria-hidden="true">↗</b></span></a><a class="cc-next-link" href="#/components"><strong>Check components</strong><span>Review versions and node tools <b aria-hidden="true">↗</b></span></a><button type="button" class="cc-next-link" data-page-action="cc-manage-nodes"><strong>Manage nodes</strong><span>Connections and SSH identities <b aria-hidden="true">↓</b></span></button><p class="cc-queue-note">Engines start only inside a GPU ticket queue. Hydraweave reads the queue; it never writes it.</p></section>`;
    return header + invalid + initial + summary + weave() + discovered() + `<div class="cc-home-grid">${engineCatalog()}${next}</div>`
      + `<div class="cc-setup-heading" id="cc-node-management" tabindex="-1"><span class="cc-eyebrow">Workspace management</span><h2>Keep your cluster ready.</h2></div>` + controller() + (notice ? `<p class="cc-notice" role="status">${esc(notice)}</p>` : "") + progress
      + (ready ? H().card("Your nodes", nodes() + `<div class="cc-card-footer">${action("Add node", "add-node", {}, H().mutationReason())}${link("All machines", "#/devices")}${link("Topology & links", "#/topology")}</div>`, "Connected explicitly; status comes from each machine") : "")
      + H().card("Where does Hydraweave run?", `<p>The panel runs on <strong>${esc(d.controller?.hostname || "this computer")}</strong>. The request router can run here or on a configured Linux node. The engine's rank 0 is a compute role and does not have to host the panel.</p><p class="hint">${esc(typeof d.recommendation === "string" ? d.recommendation : d.recommendation?.detail || d.recommendation?.note || "For regular use, choose an always-on controller. A laptop can connect to it through an SSH tunnel.")}</p>${lifecycle.note ? `<div class="cc-lifecycle"><strong>${esc(lifecycle.mode || "Process lifecycle")}</strong><p>${esc(lifecycle.note)}</p></div>` : ""}${ready ? action("Choose router location", "router", {}, H().mutationReason()) : ""}`);
  }

  async function openMachine(id = null) {
    const cfg = await H().request("ops/config");
    if (!Array.isArray(cfg.machines) || !cfg.version) throw new Error("Create a valid workspace first on Home.");
    const row = id ? cfg.machines.find(m => m.id === id) : null;
    if (id && !row) throw new Error("This machine is no longer configured. Refresh and try again.");
    const e = row?.entry || {os:"linux", access:"ssh", ssh_port:22, lab_mode:true};
    const frozen = row?.frozen || "", lock = frozen ? `disabled title="${esc(frozen)}"` : "";
    const appendPeersOnly = !!frozen && row?.add_peer_aliases === true;
    const input = (key,label,value,placeholder="",type="text") => `<label class="field" for="cc-node-${key}">${esc(label)}<input class="inp" id="cc-node-${key}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" spellcheck="false" ${lock}></label>`;
    const peers = Object.entries(e.peer_ssh || {}).map(([mid,alias]) => `${mid}=${alias}`).join("\n");
    UI().open({title:id ? `Edit ${id}` : "Add compute node", confirm:appendPeersOnly ? "Save peer aliases" : frozen ? "Close" : id ? "Save node" : "Add node", body:
      `<p class="hint">Use a private network address and an SSH account that already has access to the machine. You will verify its host key after adding it.</p>${frozen ? `<p class="banner warn">${esc(frozen)}</p>` : ""}${appendPeersOnly ? '<p class="hint">You can add aliases for newly configured peer nodes. Existing aliases cannot be changed or removed. Saving records the configuration; it does not test SSH or change running processes.</p>' : ""}<div class="two">${input("id","Node name",e.id || "","cluster2")}${input("address","Private IP address",e.http_address || "","192.168.1.20")}</div><div class="two">${input("ssh","SSH account or configured alias",e.ssh_target || "","user@192.168.1.20")}${input("root","Install directory on this node",e.install_root || "","/home/user/hydraweave-node")}</div>
      <div class="cc-lifecycle"><label class="check"><input type="checkbox" id="cc-node-lab" ${e.lab_mode ? "checked" : ""} ${lock}>Use the existing lab turn manager</label><p>Start profile uses GPU leases from this node’s lab directory. It must already contain <code>gpu-turn.sh</code>; paired profiles also need <code>gpu-turn-pair.sh</code>. Preparing node tools does not install these scripts.</p>${input("labdir","Lab directory on node (required for Start profile)",e.lab_dir || "","/home/user/gpu-lab")}<p>Without lab mode, you can register the node for monitoring and use the separate Qwen service workflow.</p></div>
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

  async function trustNode(mid) {
    const scan = await UI().post("ops/hostkey/scan",{machine:mid});
    UI().open({title:`Verify ${mid}`,confirm:"Trust this host key",body:`<p>Compare this fingerprint with the one shown on the node or provided by your administrator.</p><ul class="cc-fingerprints">${(scan.candidates || []).map(c => `<li><b>${esc(c.algorithm)}</b><code>${esc(c.fingerprint)}</code></li>`).join("")}</ul><label class="field" for="cc-fingerprint">Fingerprint you verified<input class="inp" id="cc-fingerprint" spellcheck="false" autocomplete="off" placeholder="SHA256:…"></label>`,submit:async () => {
      const fingerprint = document.getElementById("cc-fingerprint").value.trim();
      if (!(scan.candidates || []).some(c => c.fingerprint === fingerprint)) throw new Error("Enter one of the scanned fingerprints after verifying it on the node.");
      await H().mutate("hosts/pin",{machine:mid,fingerprint});
    }});
  }
  async function routerDialog() {
    const setup = await H().request("setup"), cfg = setup.configuration || {}, router = setup.router || {};
    const candidates = (setup.nodes || []).filter(n => n.access === "ssh" && n.os === "linux");
    const port = String(router.listen || "").split(":").at(-1);
    UI().open({title:"Router placement",confirm:"Save for next start",body:`<p>The router receives API requests and forwards them to compute workers. The panel stays on this controller.</p><label class="field" for="cc-router-machine">Run the router on<select class="inp" id="cc-router-machine"><option value="local" ${router.machine === "local" ? "selected" : ""}>This controller</option>${candidates.map(n => `<option value="${esc(n.id)}" ${router.machine === n.id ? "selected" : ""}>${esc(n.id)} · Linux node</option>`).join("")}</select></label><label class="field" for="cc-router-port">Port<input class="inp" id="cc-router-port" type="number" min="1" max="65535" value="${/^\d+$/.test(port) ? esc(port) : "18701"}"></label><p class="hint">This changes the saved launch configuration. Running profiles must be stopped before it can be saved.</p>`,submit:async () => {
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
      else if (a === "cc-initialize") { button.disabled = true; await UI().post("setup/initialize",{}); notice="Workspace created. Add your compute nodes to continue."; H().announce(notice); await H().refresh(); }
      else if (a === "cc-add-node") await openMachine();
      else if (a === "cc-adopt") UI().open({title:"Adopt node",confirm:"Adopt",body:`<label class="field">Pairing code from the node console<input class="inp" id="cc-pair-code" inputmode="numeric" autocomplete="off"></label>`,submit:async () => {
        const result = await UI().post("discovery/adopt", {id:button.dataset.id,code:document.getElementById("cc-pair-code").value.trim()});
        notice = `Adopted ${result.adopted}${result.provisioning_error ? ` · ${result.provisioning_error}` : ""}`;
        await H().refresh();
      }});
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
    endpoints:() => ["setup", "discovery", ...(H().pages.turns ? ["turns"] : [])],
    ready:() => !!H().state.docs.setup,
    error:() => H().state.errors.setup || "",render,
  };
})();
