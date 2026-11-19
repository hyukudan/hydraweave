# Engine-owned deployment, capacity and drain

HydraWeave can consume the **Strix inference engine's** `strix-deployment-v1`
contract. This is an explicit profile option. Existing profiles keep their
qualified launch arguments and component pins until the operator selects a
compatible installed release and opts in. Nothing here replaces the inference
engine or activates a different backend.

The adapter is integrated with normal profile launches, non-lab services, the
local router, the router on a worker and the offline memory planner. Node
inspection runs before requesting GPU turns. Native engine admission still runs
at startup and remains authoritative under current memory pressure.

## Select the contract

A profile can request the engine's automatic policy:

```json
{"deploy": {"spec": "auto"}}
```

Or supply a TOML specification available on each selected worker:

```json
{"deploy": {"spec": "/srv/deploy/model.toml"}}
```

The preset's implicit `--slots`, `--capacity` and `--context` arguments are
removed for an explicit deployment request. Profile overrides remain explicit
pins and are applied afterwards. Other model and numerical settings remain
part of the launch. A release without `--deploy-check` support rejects an
explicit deployment request; absence of `deploy` does not probe or change the
legacy launch.

Inspection verifies the immutable release executable and its runtime environment.
The helper bounds both output streams to 256 KiB, reads stdout/stderr concurrently,
limits help to 30 seconds and inspection to 120 seconds, and reaps its owned child
on timeout or excessive output. It does not fetch or update an engine release.

For an automatic two-node profile, HydraWeave first obtains one measured local
device record from each engine. It then writes the complete inventory into a
content-addressed TOML file on each worker and inspects the complete pair. No
peer memory is inferred from the local machine. Custom TOML files must contain
the inventory their engine requires. Independent workers are inspected in
parallel, with at most eight requests in flight.

## Validate every rank

The parser checks the schema and explicit admission decision; a refused plan
never falls back to a historical prediction. Every rank must reference a distinct
measured device. Rank/device total and free memory must agree, and unsigned byte
fields cannot be booleans, negative values or floating-point approximations.

The following bounds must all hold:

```text
margin <= measured free
budget <= measured free - margin
required <= budget
free after = budget - required
```

KV and footprint owners must fit within `required`. A complete footprint's
workspace, weights, staging, KV and extra owners must sum to the declared total.
The placement adapter also applies the common peak-versus-cap-minus-margin check.
The previously accepted 62 GiB requirement with 62 GiB free and 1 GiB margin is
therefore refused.

HydraWeave supports the lease layouts it actually implements: one rank per
replica, or TP2 with two ranks. A plan requiring another topology fails before
launch. Pair inspections must agree on model, topology, format, context and slots.
The engine exchanges current device observations again when starting its pair.

## Inspection identity and live identity

`deployment_id` is a hash of the plan, including measured free memory. It is not a
process identifier. Two equivalent inspections at different times may have
different IDs; identical plans can have the same ID after a process restart.
HydraWeave therefore does **not** require the live ID to equal an earlier
inspection ID.

Readiness for an opted-in process requires a live deployment identity in its
health response. The router validates the live plan and binds its first accepted
ID. Health and plan IDs must agree. A later live ID change stops admission until
router reload; ordinary health/process ownership checks still handle failures
whose plan hash has not changed. The legacy snapshot field
`deployment_restarted` denotes this sticky **plan identity change**, not proof
that a process restarted.

The immutable plan is cached for the current live ID. Health and capacity are
sampled in parallel; a plan is fetched again on identity change. Older endpoints
that report no deployment are re-probed after 30 seconds rather than downloading
an absent/full plan every health tick. Each completed node sample is published
without waiting for another node's slow response.

## Runtime capacity

The router reads `/api/status.capacity` with these current engine semantics:

- `slots_total`, `slots_busy` and `queue_depth` are nonnegative integer counts.
- `slots_busy` cannot exceed `slots_total`.
- `kv_bytes_free` is either a nonnegative integer or `null`. Fixed-slot engines
  legitimately use `null` because they do not have a shared free-byte allocator.
- Zero known KV bytes stops new admission; unknown KV bytes leave slot accounting
  authoritative.

The runtime limit remains bounded by configured `max_inflight`. It accounts for
busy slots and queue depth. Local reservations are captured **before** requesting
the remote sample, so work admitted during that request is not accidentally
returned as new capacity when the response arrives.

Authenticated health, capacity and deployment requests use the upstream key.
Unavailable legacy endpoints (404/405/501) preserve configured routing; a malformed
or inconsistent new contract does not become a legacy fallback. Transport errors
stop admission to that node immediately.

## Stop and drain

Local HTTP leaders are asked to drain before TERM. Independent leaders drain in
parallel within one shared 30-second window. A `draining` response with
`drained:false` is polled until completion or that deadline. Unsupported legacy
endpoints proceed to the existing bounded signal/cleanup sequence. Followers do
not receive an HTTP drain request; MiniMax's leader-first shutdown ordering is
preserved.

The health and drain helper does not follow redirects with engine credentials.
A pair's readiness checks use rank 0's health credential, because rank 0 owns the
HTTP listener. Cancelling/finishing a lease retains the existing ownership and
process-group guarantees.

## Offline use and legacy estimates

A recorded engine plan can be inspected without opening model weights:

```sh
hydraweave vramplan ds41-node --deployment-json plan.json --deployment-rank 0
```

For the legacy DS41 prefill predictor, `--device-free-gib` provides an actual free
memory observation after context creation. When absent, the estimate is bounded
by the supplied cap and is explicitly labelled unmeasured. The former hard-coded
93.09 GiB observation from one historical machine is no longer treated as a
measurement of every machine. Historical replay tests still supply that number
explicitly to reproduce their original receipt.

Engine-measured admission is not a throughput optimizer or a fidelity certificate.
Current calibrated performance predictions use the separate recipe/measurement
identity and expiry checks. Hardware acceptance still requires the selected
engine revision, model assets, numerical settings and actual machines.

## Validation

The host regressions cover margin/footprint consistency, null KV capacity,
per-node and endpoint concurrency, reservations made during a slow probe,
plan-cache reuse, live identity changes, pair inventory collection, authenticated
readiness, bounded drain, preserved leader ordering and bounded real child
inspection. The router tests use the repository's pinned TensorMeld pool. No GPU,
SSH or real systemd service is required by these tests.

The source of the contract is the engine's
[deployment documentation](https://github.com/hyukudan/strix-halo-engine-lab/blob/5c1298e85a314a95091ae5babe9a8a61ef3f8bf1/docs/deployment.md)
and `src/deploy/plan.cpp`, `src/deploy/spec.cpp`, `include/strix/deploy/binding.hpp`
and `src/serve/deployment.cpp`. This integration ports and corrects the adapter
proposed in HydraWeave PR #11; its old head should not be merged over this code.
