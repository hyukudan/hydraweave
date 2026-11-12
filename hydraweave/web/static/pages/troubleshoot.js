/* Read-only health checks using the existing doctor action. */
"use strict";
(() => {
  const H = () => window.Hydraweave;
  let checks = [], running = false;
  const esc = value => H().esc(value ?? "");
  function render() {
    const rows = checks.map(item => {
      const error = item.error || item.check?.error || item.check?.status === "error" && item.check?.why;
      const explanation = error ? H().explainError(error) : null;
      const okay = !error && item.check?.status !== "unavailable";
      return `<article class="card"><h2>${okay ? "OK" : "Needs attention"} · ${esc(item.name)}</h2><p>${esc(okay ? "No problem reported." : explanation?.cause || "No diagnostic result was reported.")}</p>${!okay ? `<a class="btn" href="${H().fixHref(explanation)}">${esc(explanation?.fixLabel || "Troubleshoot")}</a>${explanation?.hasDetails ? `<details><summary>Details</summary><pre>${esc(explanation.detail)}</pre></details>` : ""}` : ""}</article>`;
    }).join("");
    return `<section class="card"><h2>Troubleshoot</h2><p>Run diagnostics on this computer and each configured node.</p><div class="row"><button class="btn pri" type="button" data-troubleshoot-run ${running ? "disabled" : ""}>${running ? "Checking…" : "Run all checks"}</button><button class="btn" type="button" data-troubleshoot-copy>Copy report</button></div></section><div class="grid g2">${rows || `<p>${checks.length ? "No checks reported." : "Run checks to see the health of this computer and each node."}</p>`}</div>`;
  }
  async function run() {
    running = true; H().render();
    const targets = [{name:"This computer", args:{}}].concat(H().machines().map(m => ({name:m.name || m.id,args:{machine:m.id}})));
    checks = [];
    for (const target of targets) {
      try {
        const result = await H().mutate("doctor", target.args, true);
        const items = Object.entries(result?.checks || {}).map(([name, check]) => ({name:target.name + " · " + name, check}));
        checks.push(...(items.length ? items : [{name:target.name, check:result}]));
      } catch (error) { checks.push({name:target.name, error:error.message}); }
    }
    running = false; H().render();
  }
  (window.HydraweavePages ||= {}).troubleshoot = {title:"Troubleshoot",group:"Control center",order:90,
    endpoints:() => ["devices"],ready:() => true,render,
    after(root) {
      root.querySelector("[data-troubleshoot-run]")?.addEventListener("click", () => run());
      root.querySelector("[data-troubleshoot-copy]")?.addEventListener("click", async () => {
        const report = checks.map(row => `${row.name}: ${row.error || JSON.stringify(row.check)}`).join("\n");
        await navigator.clipboard?.writeText(report.replace(/(token|authorization)\s*[:=]\s*\S+/ig, "$1: [redacted]"));
      });
    }};
})();
