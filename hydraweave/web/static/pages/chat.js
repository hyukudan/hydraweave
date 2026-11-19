/* Same-origin chat jobs. Credentials stay on the portal; the shell polls and renews jobs. */
"use strict";
(() => {
  const conversations = new Map(), drafts = new Map(), requests = new Map(), bound = new WeakSet();
  const H = () => window.Hydraweave;
  const esc = value => H().esc(value);
  const running = () => H().profiles().filter(p => p.state === "running");
  const selected = () => {
    const id = new URLSearchParams(location.hash.split("?").slice(1).join("?")).get("model");
    return running().find(p => H().profileId(p) === id) || running()[0];
  };
  const info = p => (H().state?.docs?.chat?.profiles || []).find(row => row.id === H().profileId(p));
  const endpoint = p => info(p)?.endpoint || (p?.processes || []).find(row => row.name === "router" || row.role === "router")?.endpoint;
  const active = request => request && ["submitting", "running"].includes(request.state);

  function snippets(p) {
    const base = (endpoint(p) || "<router-base-url>").replace(/\/$/, "");
    return `# Use the router key, not the portal access token.\n` +
      `curl "${base}/v1/models" -H "Authorization: Bearer $HYDRAWEAVE_API_KEY"\n` +
      `curl "${base}/v1/chat/completions" -H 'Content-Type: application/json' -H "Authorization: Bearer $HYDRAWEAVE_API_KEY" -d '{"model":"<id from /v1/models>","messages":[{"role":"user","content":"Hello"}]}'\n\n` +
      `import os\nfrom openai import OpenAI\nclient = OpenAI(base_url="${base}/v1", api_key=os.environ["HYDRAWEAVE_API_KEY"])\nmodel = client.models.list().data[0].id\nresponse = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Hello"}])\nprint(response.choices[0].message.content)\n\n` +
      `// Node.js\nconst headers = {"Authorization": "Bearer " + process.env.HYDRAWEAVE_API_KEY, "Content-Type": "application/json"};\nconst models = await (await fetch("${base}/v1/models", {headers})).json();\nconst response = await fetch("${base}/v1/chat/completions", {method: "POST", headers, body: JSON.stringify({model: models.data[0].id, messages: [{role: "user", content: "Hello"}]})});\nconsole.log(await response.json());`;
  }

  function settle() {
    const jobs = H().state?.lastJobs || [];
    for (const [id, request] of requests) {
      if (!request.job || request.completed) continue;
      const job = jobs.find(row => (row.id || row.job) === request.job) || request.accepted;
      if (!job) continue;
      request.state = job.state;
      request.progress = job.progress;
      if (job.state === "running") continue;
      request.completed = true;
      if (job.state === "succeeded" && job.result?.profile === id && typeof job.result?.message?.content === "string") {
        const messages = conversations.get(id) || [];
        messages.push({role:"assistant", content:job.result.message.content});
        conversations.set(id, messages);
        request.error = "";
      } else request.error = job.state === "cancelled" ? "Request cancelled." : job.error || "The request ended without an answer.";
    }
  }

  function render() {
    settle();
    const models = running(), p = selected();
    if (!p) return H().empty("No model is running", "Start a model to chat with it.", '<a class="btn pri" href="#/models">Go to Models</a>');
    const name = H().profileId(p), messages = conversations.get(name) || [], request = requests.get(name);
    const busy = active(request), unavailable = info(p)?.why;
    const status = busy ? (request.cancelling ? "Cancelling the request…" : request.progress || "Sending request…") : "";
    return `<div class="field"><label for="chat-model">Model</label><select id="chat-model" class="inp">${models.map(m => `<option value="${esc(H().profileId(m))}"${m === p ? " selected" : ""}>${esc(m.label || H().profileId(m))}</option>`).join("")}</select></div>` +
      `<section class="card"><div id="chat-log" aria-live="polite">${messages.map(m => `<p><b>${esc(m.role)}:</b> ${esc(m.content)}</p>`).join("")}</div>` +
      `<p class="hint">Requests use this portal. The complete answer appears when generation finishes.</p>` +
      `${status ? `<p role="status">${esc(status)}</p>` : ""}${request?.error || unavailable ? `<p class="field-error" role="alert">${esc(request?.error || unavailable)}</p>` : ""}` +
      `<label for="chat-message">Message</label><textarea id="chat-message" class="inp" rows="4" maxlength="32768"${busy ? " disabled" : ""}>${esc(drafts.get(name) || "")}</textarea>` +
      `<div class="row"><button class="btn pri" id="chat-send"${busy || unavailable ? " disabled" : ""}>Send</button><button class="btn" id="chat-stop"${busy && !request.cancelling ? "" : " disabled"}>Stop</button><button class="btn" id="chat-clear"${busy ? " disabled" : ""}>Clear conversation</button></div></section>` +
      `<section class="card"><h2>Use it from your apps</h2><p>Router base URL: <code>${esc(endpoint(p) || "<router-base-url>")}</code> · Profile: <code>${esc(name)}</code></p>` +
      `<p class="hint">Read the served model id from /v1/models. Supply the router key through your application's environment; it is never shown here.</p><pre id="chat-snippets">${esc(snippets(p))}</pre><button class="btn" id="chat-copy">Copy snippets</button></section>`;
  }

  async function post(action, args) {
    const token = H().csrf();
    if (!token || token === "__HYDRAWEAVE_CSRF__") throw new Error("Launch the portal through hydraweave ui to enable chat.");
    return H().request(action, {method:"POST", headers:{"Content-Type":"application/json", "X-Hydraweave-CSRF":token}, body:JSON.stringify(args)});
  }

  async function cancel(request) {
    request.cancelling = true;
    H().render();
    if (!request.job) return; // Stop during submission is sent as soon as the job is accepted.
    try { await post("cancel", {job:request.job}); await H().refresh(); }
    catch (error) { request.cancelling = false; request.error = error.message; H().render(); }
  }

  async function send(main) {
    const p = selected();
    if (!p) return;
    const id = H().profileId(p), text = (drafts.get(id) ?? main.querySelector("#chat-message")?.value ?? "").trim();
    if (active(requests.get(id))) return;
    if (!text) { H().announce("Enter a message"); return; }
    const messages = conversations.get(id) || [], request = {state:"submitting", progress:"Sending request…"};
    requests.set(id, request);
    drafts.set(id, text);
    H().render();
    try {
      const job = await post("chat/send", {profile:id, messages:[...messages, {role:"user", content:text}], max_tokens:512});
      request.job = job.id || job.job;
      request.accepted = job;
      request.state = "running";
      H().own(request.job);
      messages.push({role:"user", content:text});
      conversations.set(id, messages);
      drafts.delete(id);
      if (request.cancelling) await cancel(request);
      await H().refresh();
    } catch (error) {
      if (!request.job) request.state = "failed";
      request.error = error.message;
    }
    H().render();
  }

  function bind(main) {
    if (bound.has(main)) return;
    bound.add(main);
    main.addEventListener("input", event => {
      if (event.target.id === "chat-message" && selected()) drafts.set(H().profileId(selected()), event.target.value);
    });
    main.addEventListener("click", async event => {
      const target = event.target.closest("button");
      if (!target || target.disabled) return;
      const p = selected(), id = p && H().profileId(p), request = requests.get(id);
      if (target.id === "chat-copy") { await navigator.clipboard?.writeText(main.querySelector("#chat-snippets")?.textContent || ""); return; }
      if (target.id === "chat-send") { await send(main); return; }
      if (target.id === "chat-stop" && active(request)) { await cancel(request); return; }
      if (target.id === "chat-clear" && id && !active(request)) { conversations.delete(id); drafts.delete(id); requests.delete(id); H().render(); }
    });
    main.addEventListener("change", event => {
      if (event.target.id === "chat-model") location.hash = `#/chat?model=${encodeURIComponent(event.target.value)}`;
    });
  }
  window.HydraweavePages ||= {};
  window.HydraweaveChat = {endpoint, snippets};
  window.HydraweavePages.chat = {title:"Chat", icon:'<path d="M3 4h14v10H9l-5 3v-3H3z"/>', group:"Inference", order:22,
    endpoints:()=>["chat"], render, after:bind};
})();
