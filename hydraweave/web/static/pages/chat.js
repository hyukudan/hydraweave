/* Chat landing page; the interactive conversation is supplied by the chat service. */
"use strict";
(() => {
  window.HydraweavePages = window.HydraweavePages || {};
  window.HydraweavePages.chat = {
    title: "Chat",
    icon: '<path d="M3 4h14v10H9l-5 3v-3H3z"/>',
    group: "Lab tools",
    order: 1,
    render: () => '<section class="card"><h2>Chat</h2><p>Chat is not available in this portal build yet.</p><p>Use the OpenAI compatible API at <code>/v1/chat/completions</code>. See <a href="#/endpoints">API and endpoint details</a>.</p></section>',
  };
})();
