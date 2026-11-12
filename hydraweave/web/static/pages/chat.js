/* Chat tab for running models, with API snippets. */
"use strict";
(() => {
  const conversations = new Map();
  let controller = null;
  const esc = value => window.Hydraweave.esc(value);
  const running = () => window.Hydraweave.profiles().filter(p => p.state === "running");
  const endpoint = p => (p?.processes || []).map(row => row.endpoint).find(Boolean)?.replace(/\/$/, "");
  function snippets(p) {
    const base = endpoint(p) || "<router-base-url>", id = window.Hydraweave.profileId(p);
    return `curl ${base}/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"${id}","messages":[{"role":"user","content":"Hello"}]}'\n\nfrom openai import OpenAI\nclient = OpenAI(base_url="${base}/v1", api_key="${"<router-api-key>"}")\nresponse = client.chat.completions.create(model="${id}", messages=[{"role": "user", "content": "Hello"}])\nprint(response.choices[0].message.content)\n\nconst response = await fetch("${base}/v1/chat/completions", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({model: "${id}", messages: [{role: "user", content: "Hello"}]})});\nconsole.log(await response.json());`;
  }
  function render() {
    const H = window.Hydraweave, models = running();
    if (!models.length) return H.empty("No model is running", "Start a model to chat with it.", '<a class="btn pri" href="#/models">Go to Models</a>');
    const query = new URLSearchParams(location.hash.split("?").slice(1).join("?"));
    const id = query.get("model"), p = models.find(x => H.profileId(x) === id) || models[0], name = H.profileId(p);
    const messages = conversations.get(name) || [];
    return `<div class="field"><label for="chat-model">Model</label><select id="chat-model" class="inp">${models.map(m => `<option value="${esc(H.profileId(m))}"${m === p ? " selected" : ""}>${esc(m.label || H.profileId(m))}</option>`).join("")}</select></div><section class="card"><div id="chat-log" aria-live="polite">${messages.map(m => `<p><b>${esc(m.role)}:</b> ${esc(m.content)}</p>`).join("")}</div><label for="chat-message">Message</label><textarea id="chat-message" class="inp" rows="4"></textarea><div class="row"><button class="btn pri" id="chat-send">Send</button><button class="btn" id="chat-stop"${controller ? "" : " disabled"}>Stop</button><button class="btn" id="chat-clear">Clear conversation</button></div></section><section class="card"><h2>Use it from your apps</h2><p>Base URL: <code>${esc(endpoint(p) || "<router-base-url>")}</code> · Model id: <code>${esc(name)}</code></p><pre id="chat-snippets">${esc(snippets(p))}</pre><button class="btn" id="chat-copy">Copy snippets</button></section>`;
  }
  function bind(main) {
    main.addEventListener("click", async event => {
      const target = event.target;
      if (target.id === "chat-model") return;
      if (target.id === "chat-copy") { await navigator.clipboard?.writeText(main.querySelector("#chat-snippets")?.textContent || ""); return; }
      if (target.id === "chat-clear") { const p = running().find(x => window.Hydraweave.profileId(x) === new URLSearchParams(location.hash.split("?").slice(1).join("?")).get("model")) || running()[0]; conversations.set(window.Hydraweave.profileId(p), []); window.Hydraweave.render(); return; }
      if (target.id === "chat-stop") { controller?.abort(); return; }
      if (target.id !== "chat-send") return;
      const H = window.Hydraweave, p = running().find(x => H.profileId(x) === main.querySelector("#chat-model")?.value) || running()[0], base = endpoint(p), input = main.querySelector("#chat-message"), text = input?.value.trim();
      if (!base || !text) { H.announce(base ? "Enter a message" : "Router URL is unavailable"); return; }
      const id = H.profileId(p), messages = conversations.get(id) || [];
      messages.push({role: "user", content: text}, {role: "assistant", content: ""}); conversations.set(id, messages); input.value = ""; H.render();
      controller = new AbortController();
      try {
        const response = await fetch(base + "/v1/chat/completions", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({model: id, messages: messages.slice(0, -1), stream: true}), signal: controller.signal});
        if (!response.ok) throw new Error(`Chat request failed (${response.status})`);
        const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
        while (true) { const {value, done} = await reader.read(); if (done) break; buffer += decoder.decode(value, {stream: true}); const lines = buffer.split("\n"); buffer = lines.pop(); for (const line of lines) if (line.startsWith("data: ") && line !== "data: [DONE]") { try { messages.at(-1).content += JSON.parse(line.slice(6)).choices?.[0]?.delta?.content || ""; } catch (_) {} } H.render(); }
      } catch (error) { if (error.name !== "AbortError") { messages.at(-1).content = `Failed: ${error.message}`; H.render(); } }
      finally { controller = null; }
    });
    main.addEventListener("change", event => { if (event.target.id === "chat-model") { location.hash = `#/chat?model=${encodeURIComponent(event.target.value)}`; } });
  }
  window.HydraweavePages ||= {};
  window.HydraweavePages.chat = {title: "Chat", icon: '<path d="M3 4h14v10H9l-5 3v-3H3z"/>', group: "Inference", order: 22, endpoints: () => [], render, after: bind};
})();
