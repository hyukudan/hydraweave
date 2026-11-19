# Current performance evidence for Strix placement

The recipe solver accepts current measurements through the existing local solver
bundle. It does not benchmark hardware during a solve, refresh missing evidence,
or choose a deployment from historical throughput alone. Feasible placements can
still be shown and saved without a throughput prediction.

The `placement/hetero.py` comparison retains its October 2026 experiment model for
historical analysis. Its output explicitly sets `current_hardware: false`. Its
published throughput, assumed link saving, and hit-rate interpolation do not
qualify a current engine, driver, model or machine.

## What must match

A bundle may add `calibration`, either an inline receipt or the name of a local
JSON file relative to the bundle. The receipt uses
`hydraweave/performance-calibration-v1` and binds the supplied `costs` and `curves`
to all of the following:

| Input | Required evidence |
| --- | --- |
| Engine and model | Full engine revision, verified executable SHA-256 and verified model SHA-256 |
| Physical devices | Unique nonzero GPU identities and current driver; capacity, architecture, platform and available topology are included in the identity |
| Request | Exact recipe bytes, route, workload, context, materialized component documents and profile/override settings |
| Link costs | Explicit link inventory; each used link has measured bytes per second, latency in nanoseconds and the source measurement digest |
| Residency | Explicit `cold`, `warm` or `mixed` state plus the digest of its recorded state |
| Storage and transfer | Current storage identity, core-supplied transfer identities and host topology |
| Benchmark provenance | Named method, at least three completed runs and source receipt digests |
| Freshness | Actual measurement time, explicit expiry at most seven days later, and a probe no older than 24 hours |

No links must be represented by an explicit empty `links` list, and an unused
store by `file_store: "not-used"`. A network's nominal line rate is not a link
measurement. A card number, PCI address alone, zero unique ID or missing driver
does not identify a calibrated physical GPU. The passive AMD probe reads the
kernel's unique ID and PCI slot and combines the observed kernel/ROCm versions;
unavailable values remain unknown.

These checks bind evidence to a request. They do not authenticate a benchmark
publisher or independently prove that submitted measurements were performed.
Use the completed receipts from the exact own-engine workload, retain their
source documents locally, and apply the same review used for other imported
measurement data. This change contains CPU-only regression fixtures; it is not
a performance acceptance result for any GPU or USB4 cluster.

## Supplying already measured inputs

Keep the ordinary Tierstream input schema for `inputs`. Expand local JSON and
component cache references before creating a calibration receipt, so its digest
describes the same materialized documents the solver will read. Include every
choice whose cost or curve is supplied. Calibration covers that exact request;
it does not grant validity to a neighboring context, another budget or another
model merely because a fitted curve can return a number.

The following example packages **existing local measurement files**. It performs
no benchmark and supplies no invented rates or hardware identities. The filenames
are portable examples; `run-evidence.json` must contain the real measurement and
expiry times plus `evidence.method`, `evidence.runs` and
`evidence.source_sha256` from those completed runs.

```python
import hashlib
import json
from pathlib import Path

from hydraweave.recipes import calibration, session

recipe = Path("recipe.toml")
probe = json.loads(Path("current-probe.json").read_text())
inputs = json.loads(Path("materialized-solver-inputs.json").read_text())
context = json.loads(Path("context.json").read_text())
run = json.loads(Path("run-evidence.json").read_text())
layers = session.Layers(workload="default")
# Supply the actual profile, overrides and sets in layers when the solve uses them.

document = calibration.receipt(
    probe, inputs,
    route="decode", workload=layers.workload, context=context,
    recipe_sha256=hashlib.sha256(recipe.read_bytes()).hexdigest(),
    settings=calibration.layer_settings(layers),
    measured_at=run["measured_at"], expires_at=run["expires_at"],
    evidence=run["evidence"],
)
calibration.validate(
    document, probe, inputs,
    route="decode", workload=layers.workload, context=context,
    recipe_sha256=hashlib.sha256(recipe.read_bytes()).hexdigest(),
    settings=calibration.layer_settings(layers),
)
Path("calibration.json").write_text(json.dumps(document, indent=2))
```

The receipt must be placed alongside the local solver bundle, or referenced by
its local path. A bundle then has the following shape; it contains only filenames
here, no synthetic performance values:

```json
{
  "probe": "current-probe.json",
  "inputs": "materialized-solver-inputs.json",
  "context": "context.json",
  "calibration": "calibration.json"
}
```

Store this bundle under `run/solver-inputs/NAME.json` and run:

```sh
hydraweave solve RECIPE --route decode --bundle NAME
```

The same bundle works through the web planner and `recipes.service.plan` with
`solver_bundle`/`solver_route`. The API result and web card expose the calibration
status and expiry. Successful validation permits the core to consume the supplied
costs/curves; the core still owns schema validation, placement arithmetic and any
prediction refusal.

With no receipt, HydraWeave withholds all top-level and per-choice `costs`/`curves`
and reports why performance is unpredicted. If a receipt is supplied but expired,
foreign, malformed or changed, the solve refuses it and requests fresh evidence.
It does not silently replace it with a historical curve. Explicitly extrapolated
solver rows have no Pick and save action. Existing saved solver selections from
before this evidence format require another solve.

## Transfer and disk cache lifetime

Transfer and host-store measurements continue to use the explicit gated `probe`
commands. The measurement cache now uses versioned receipts with nine complete
samples of the declared method and a 24-hour lifetime. Current physical UUID,
driver, storage and core transfer hash must all match. A missing current identity
cannot be filled from the old cache key. Unknown, stale or malformed entries yield
no prediction, and never trigger a GPU operation or model read during planning.

Picking a saved solver row rechecks the calibration, referenced files and cached
measurements. Expired or changed evidence requires another solve; the prior
selection is not moved to another placement automatically.

The separate legacy disk facts cache remains limited to seven days. Local disk
keys include device, inode, size and nanosecond mtime. Imported node disk receipts
must include their original Unix `measured_at`; importing an old receipt does not
renew its age. Legacy receipts can still be stored for review, but a receipt
without a valid measurement time is refused when used as evidence. Concurrent
cache updates merge under a shared file lock and publish an atomic document.
