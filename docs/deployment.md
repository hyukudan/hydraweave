# Engine deployment contract

Presets accept an optional `deploy` object. The same field in a cluster profile
overrides the preset:

```json
"deploy": {"spec": "auto"}
```

Use `{"spec": "/srv/deploy/model-tier.toml"}` for a spec already present at
that absolute path on every participating node. Paths with spaces are supported.
Hydraweave passes these as the two argv tokens `--deploy FILE|auto`. It removes
preset `--slots`, `--capacity` and `--context` defaults so the engine can choose
the tier. Explicit profile overrides remain engine pins. Resolver results do
not pass through the legacy preset knob limits.

Before an `up` lease or service installation, each node probes its immutable
release's help for `--deploy-check`. Supported releases run the exact launch
argv with `--deploy-check`, using the release's runtime environment. Releases
without this flag retain their original launch argv; an explicitly configured
deployment requires support. A supported release without a configured spec uses
`--deploy auto`, with preset capacity defaults removed. Refused or invalid plans
stop launch before a lease is requested. Hydraweave retains the measured devices,
chosen settings, rank budgets, reasons and deployment ID in launch state.

Inspection must choose the configured lease topology: a single-node deployment
for each replica, or two ranks for a pair. It cannot silently acquire additional
nodes or change the transport. Pair plans must agree across the nodes. Pair specs
must include the engine's required peer inventory; hydraweave does not invent peer
memory facts. The node spec pins the inspected deployment ID for readiness.

Both local and node-hosted TensorMeld routers use the shared engine contract
bridge. Each health cycle reads `/health`, `/v1/deployment` and `/api/status`
with the replica's API key. Available slots, queued work and exhausted KV memory
bound admission alongside TensorMeld's local reservations. The configured
`max_inflight` remains an upper bound. Capacity is a sampled hint, so the engine
remains responsible for atomic admission when other clients submit work.
Missing optional endpoints (404/405/501) retain legacy routing. Authentication,
malformed contract data and transport failures take the replica out of rotation.

The bridge checks health against the current deployment receipt and retains the
first deployment ID. A changed ID stops routing until the router is explicitly
reloaded. Router status exposes the observed ID, capacity and restart flag.
An identical deployment hash alone cannot distinguish a process restart with
identical effective settings; engines need a separate instance/boot identifier
if that distinction is required.

Engine shutdown requests `/admin/drain` on each HTTP leader before SIGTERM.
It waits for `{"status":"draining","drained":true}` within a shared 30-second
drain budget. Missing or legacy drain endpoints retain the signal-based stop
policy. Pair followers receive no HTTP drain; MiniMax keeps its leader-first
signal ordering. A reload uses this same stop lifecycle before a new launch.

For offline placement, use the engine's plan directly:

```text
hloom vramplan ds41-node --deployment-json plan.json --deployment-rank 0
```

The file may contain `--deploy-check` output or a `/v1/deployment` response.
Measured `mem_free_bytes` and rank budgets apply to unified and discrete devices;
VRAM and GTT are never summed. This path does not read the model or use the
legacy CUDA estimates. The CUDA fallback accepts measured `--device-free-gib`;
without it, the prediction uses only the operator's VRAM cap and policy margins.
Other legacy placement models remain available for releases without the contract.
