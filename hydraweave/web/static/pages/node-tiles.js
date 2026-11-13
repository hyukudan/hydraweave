/* Nodes page trend tiles: GPU memory, system memory, GPU busy and GPU temperature of every node over the last
   30 minutes, drawn from the history store's existing series (no new sampler, no new storage).
   "GPU memory" is the sum of the driver's memory domains as the back end reports them (VRAM + GTT on
   unified-memory AMD hardware): driver accounting of what the GPU can address, not RAM on top of system memory.
   Every tile has its own states: loading, request failed (reason + Retry), history store unavailable (reason),
   no samples (reason), and the chart. HydraweaveNodeTiles.combine is shared with the Machine page. */
"use strict";
(() => {
  const H = () => window.Hydraweave;
  const TS = () => window.HydraweaveTS;
  const RANGE = 1800;
  const SERIES = ["node_gpu_mem_used_bytes", "node_gpu_mem_total_bytes", "node_mem_available_bytes", "node_mem_total_bytes",
    "node_gpu_busy_percent", "node_gpu_temp_celsius"];
  const num = v => typeof v === "number" && Number.isFinite(v);
  const endpointOf = name => TS().endpoint(name, {range: RANGE});
  const endpoints = () => window.HydraweaveTS ? SERIES.map(endpointOf) : [];
  /* State of one series document: {error} | {loading} | {unavailable} | {doc}. A failed request is never shown as
     "loading" and a missing store is never shown as "no samples". */
  function seriesState(endpoint) {
    const state = H().state, error = state.errors?.[endpoint], doc = state.docs?.[endpoint];
    if (error) return {error: String(error).replace(/^history\/[^:]*:\s*/, "")};
    if (!doc) return {loading: true};
    if (doc.lines == null) return {doc, unavailable: doc.why?.lines || "history store not open in this portal process"};
    return {doc};
  }
  /* Points present at the same bucket in EVERY input line, combined with fn (a sum never mixes buckets and a
     missing sample is never treated as zero). */
  function combine(lines, fn) {
    if (!lines.length) return [];
    const maps = lines.map(points => new Map(points.filter(p => num(p[1])).map(p => [p[0], p])));
    return [...maps[0].keys()].filter(t => maps.every(m => m.has(t))).sort((a, b) => a - b)
      .map(t => [t, fn(maps.map(m => m.get(t)[1])), maps[0].get(t)[2] ?? null, maps[0].get(t)[3] ?? null]);
  }
  /* The highest value of each bucket among the lines that have a sample there (sensors need not share buckets). */
  function highest(lines) {
    const best = new Map();
    for (const points of lines) for (const p of points) if (num(p[1]) && (!best.has(p[0]) || best.get(p[0])[1] < p[1])) best.set(p[0], p);
    return [...best.values()].sort((a, b) => a[0] - b[0]);
  }
  const sum = values => values.reduce((a, b) => a + b, 0);
  const pointsOf = line => (line.points || []).map(p => [p[0], p[1], p[7] ?? null, p[8] ?? null]);
  function byMachine(doc) {
    const out = new Map();
    for (const line of doc?.lines || []) {
      const id = line.labels?.machine || "";
      if (!out.has(id)) out.set(id, []);
      out.get(id).push(line);
    }
    return out;
  }
  /* The CURRENT value of a line for a headline: its last sample, only while that sample is recent against the
     end of the requested range (within FRESH_SPACINGS x the bucket step or the sampling cadence recorded with it).
     An older sample stays on the chart as history and is never presented as "now". */
  const FRESH_SPACINGS = 3;
  function current(points, doc) {
    const p = points.filter(p => num(p[1])).at(-1);
    if (!p) return undefined;
    const spacing = Math.max(doc?.step || 2, p[3] || (doc?.tier === "raw" ? doc?.interval_s || 0 : 0));
    const end = num(doc?.end) ? doc.end : Date.now() / 1000;
    return end - p[0] <= FRESH_SPACINGS * spacing ? p[1] : undefined;
  }
  // Which contributors a headline leaves out, by name: a headline over fewer nodes than the chart says so.
  const without = names => names.length ? `no current sample: ${names.join(", ")}` : "";
  const notes = (...parts) => parts.filter(Boolean).join(" · ");
  const colorOf = i => TS().colors[i % TS().colors.length];
  function gpuMemory(docs) {
    const used = byMachine(docs.node_gpu_mem_used_bytes), total = byMachine(docs.node_gpu_mem_total_bytes);
    const lines = [], latest = [], stale = [], doc = docs.node_gpu_mem_used_bytes, totalDoc = docs.node_gpu_mem_total_bytes;
    [...used.keys()].sort().forEach((id, i) => {
      const domains = [...new Set(used.get(id).map(l => l.labels?.domain).filter(Boolean))].sort().reverse();
      const points = combine(used.get(id).map(pointsOf), sum);
      const totals = combine((total.get(id) || []).map(pointsOf), sum);
      lines.push({label: `${id} · ${domains.map(d => d.toUpperCase()).join(" + ") || "GPU memory"}`, color: colorOf(i), points});
      latest.push([current(points, doc), current(totals, totalDoc)]);
      if (!num(latest.at(-1)[0])) stale.push(id);
    });
    const known = latest.filter(([u]) => num(u));
    const u = known.length ? sum(known.map(([x]) => x)) : null, t = known.length && known.every(([, x]) => num(x)) ? sum(known.map(([, x]) => x)) : null;
    return {lines, unit: "bytes", value: num(u) ? `${TS().fmt(u, "bytes")}${num(t) ? ` / ${TS().fmt(t, "bytes")}` : ""}` : null, note: without(stale)};
  }
  function systemMemory(docs) {
    const available = byMachine(docs.node_mem_available_bytes), total = byMachine(docs.node_mem_total_bytes);
    const lines = [], latest = [], stale = [];
    [...total.keys()].sort().forEach((id, i) => {
      if (!available.has(id)) return;
      const points = combine([pointsOf(total.get(id)[0]), pointsOf(available.get(id)[0])], ([all, free]) => all - free);
      lines.push({label: `${id} · used`, color: colorOf(i), points});
      latest.push([current(points, docs.node_mem_total_bytes), current(pointsOf(total.get(id)[0]), docs.node_mem_total_bytes)]);
      if (!(num(latest.at(-1)[0]) && num(latest.at(-1)[1]))) stale.push(id);
    });
    const known = latest.filter(([u, t]) => num(u) && num(t));
    return {lines, unit: "bytes", value: known.length ? `${TS().fmt(sum(known.map(([x]) => x)), "bytes")} / ${TS().fmt(sum(known.map(([, x]) => x)), "bytes")}` : null, note: without(stale)};
  }
  function busy(docs) {
    const lines = (docs.node_gpu_busy_percent.lines || []).map((line, i) => ({label: `${line.labels?.machine || ""} · ${line.labels?.device || ""}`, color: colorOf(i), points: pointsOf(line)}));
    const now = lines.map(l => current(l.points, docs.node_gpu_busy_percent)), values = now.filter(num);
    return {lines, unit: "%", max: 100, value: values.length ? `${Math.max(...values).toFixed(0)} %` : null,
      note: notes(values.length > 1 ? "highest now" : "", without(lines.filter((_, i) => !num(now[i])).map(l => l.label)))};
  }
  function temperature(docs) {
    const lines = [];
    [...byMachine(docs.node_gpu_temp_celsius)].sort(([a], [b]) => a.localeCompare(b)).forEach(([id, sensors], i) => {
      // One line per node: its hottest reporting GPU sensor at each bucket.
      lines.push({label: `${id} · hottest GPU sensor`, color: colorOf(i), points: highest(sensors.map(pointsOf))});
    });
    const now = lines.map(l => current(l.points, docs.node_gpu_temp_celsius)), values = now.filter(num);
    return {lines, unit: "°C", value: values.length ? `${Math.max(...values).toFixed(0)} °C` : null,
      note: notes(values.length > 1 ? "hottest now" : "", without(lines.filter((_, i) => !num(now[i])).map(l => l.label)))};
  }
  const TILES = [
    {id: "gpu-memory", title: "GPU memory", needs: ["node_gpu_mem_used_bytes", "node_gpu_mem_total_bytes"], build: gpuMemory, open: "node_gpu_mem_used_bytes",
      about: "Memory the GPU driver accounts as used: the sum of its domains as reported (VRAM + GTT on unified-memory hardware). Driver accounting, not RAM on top of system memory."},
    {id: "memory", title: "System memory", needs: ["node_mem_available_bytes", "node_mem_total_bytes"], build: systemMemory, open: "node_mem_available_bytes",
      about: "System memory in use: total minus available, as the node's kernel reports them."},
    {id: "busy", title: "GPU busy", needs: ["node_gpu_busy_percent"], build: busy, open: "node_gpu_busy_percent",
      about: "Share of time the GPU was busy, per node and card, as its driver reports it."},
    {id: "temperature", title: "GPU temperature", needs: ["node_gpu_temp_celsius"], build: temperature, open: "node_gpu_temp_celsius",
      about: "Hottest GPU temperature sensor of each node."},
  ];
  function message(kind, text, retry) {
    const {esc} = H();
    return `<p class="nt-state nt-${kind}" data-nt-state="${kind}"><span class="unavailable">${esc(text)}</span>${retry ? ` <button type="button" class="btn sm" data-page-action="nt-retry">Retry</button>` : ""}</p>`;
  }
  function tile(spec) {
    const {esc, info} = H();
    const states = spec.needs.map(name => [name, seriesState(endpointOf(name))]);
    const head = value => `<div class="lbl">${esc(spec.title)}${info ? info(spec.about) : ""}</div><div class="val nt-val">${value}</div>`;
    const link = `<a class="hint nt-open" href="#/history?series=${encodeURIComponent(spec.open)}">Open in Metrics history</a>`;
    const wrap = (value, body) => `<div class="card kpi nt-tile" data-nt-tile="${spec.id}">${head(value)}${body}${link}</div>`;
    const failed = states.find(([, s]) => s.error), missing = states.find(([, s]) => s.unavailable);
    if (failed) return wrap("", message("error", `History request failed · ${failed[1].error}`, true));
    if (states.some(([, s]) => s.loading)) return wrap("", message("loading", "Loading history…"));
    if (missing) return wrap("", message("unavailable", `History unavailable · ${missing[1].unavailable}`));
    const docs = Object.fromEntries(states.map(([name, s]) => [name, s.doc]));
    const built = spec.build(docs), main = docs[spec.needs[0]];
    if (!built.lines.some(l => l.points.some(p => num(p[1])))) return wrap("", message("empty", `No samples in the last ${RANGE / 60} min · ${main.why?.lines || "nothing recorded for this series yet"} (unavailable, not zero)`));
    const chart = TS().chart({title: `${spec.title} · last ${RANGE / 60} min`, unit: built.unit, lines: built.lines, max: built.max, step: main.step || 2,
      cadence: main.tier === "raw" ? main.interval_s || 0 : 0, start: main.start, end: main.end, group: "nodes", height: 120, width: 340});
    return wrap(`${built.value ? esc(built.value) : `<span class="unavailable">no current sample</span>`}${built.note ? ` <small>${esc(built.note)}</small>` : ""}`, `<div class="nt-chart">${chart}</div>`);
  }
  function render() {
    if (!window.HydraweaveTS || !window.Hydraweave) return "";
    return `<div class="grid g4 kpis nt-tiles" aria-label="Node trends">${TILES.map(tile).join("")}</div>`;
  }
  document.addEventListener("click", event => {
    if (!event.target.closest?.('[data-page-action="nt-retry"]')) return;
    H().refresh();
  });
  window.HydraweaveNodeTiles = {endpoints, render, combine, seriesState, current, bind: root => TS().bind(root)};
})();
