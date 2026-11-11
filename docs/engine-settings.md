# Engine settings in the control panel

The **Engine settings** page edits the cluster's existing profiles through the same
schema, qualified knob tables and renderer used by the launcher. The saved result
is configuration for the next launch. A successful preview is not evidence that
an engine is running, that a model exists on a worker, or that a newer engine
revision is compatible.

## Everyday workflow

1. Add the Linux SSH workers under Machines. The controller can run the web panel
   on a laptop, desktop, or one of the worker machines. A machine used for inference
   must also be configured as an SSH worker; the local controller entry alone does
   not provide the worker adapter.
2. Create a named profile from a preset. A Qwen replica profile takes one or more
   workers. A DeepSeek or MiniMax pair takes distinct rank 0 and rank 1 workers.
3. Fill the required model asset records with their node-local paths, exact byte
   counts and full SHA-256s. Existing metadata can be edited one asset at a time.
   These are declared identities until the installation or launch verifies the
   files. A missing asset remains visibly pending.
4. Select capacity and optional advanced settings exposed by that preset. Preview
   the exact process arguments, environment and router configuration before saving.
5. Save, install the compatible release, and launch from the ordinary deployment
   flow. Creating a profile does not start a process or create a fictitious release.

For a replica profile every worker runs a whole model; rank 0/rank 1 have meaning
only for a pair. Rank 0 serves the pair's inference requests. The TensorMeld router
is a separate service with its own placement. Neither rank 0 nor the router is
automatically the controller. Profile, configuration, and lease locks coordinate
changes on the configured controller; this is not distributed leader election or
automatic controller failover.

## Defaults and qualification

**Auto** is represented by JSON `null` in an edit. Saving it removes the selected
override. It restores the pinned recipe, including any arguments that recipe
intentionally fixes; it does not rewrite the recipe to a newer engine's defaults.

The UI distinguishes the requested override (`value`) from the resolved literal
recipe value (`effective`). If the engine supplies a default that the recipe does
not spell out, `effective` is null and its source is `engine-default`. The panel
does not infer that value from a range limit or from a different release.

The current recipes deliberately have narrow contracts. Examples include the
12,288-token Qwen context and the fixed context/slot combinations in the original
DeepSeek and MiniMax recipes. They appear as fixed controls with an explanation.
The engine repository supporting a larger context today does not establish that
the older pinned release, assets and launch line have been qualified for it.

Advanced options come from `presets/*.json` through `knobs.check_table` and
`knobs.validate`. The table carries the permitted values, target argument or
environment variable, production value, dependencies, conflicts and evidence at
the exact engine commit. Changing a non-production value makes that distinction
visible in the launch summary. Arbitrary command lines, environment variables,
numerical identities, transport changes and dependency pins are not accepted by
this form.

The existing `render.render_processes` and `render.render_router` produce the
preview. No engine is executed to build it. Missing model records, calibration
files or incompatible router placement are reported as separate preview errors.
The selected release and engine/router API-key paths are managed by the existing
launch workflow. Missing managed resources appear in `generated_at_launch` and
`deferred`, not as a request to manually invent paths or keys. Final command lines
remain unavailable until those resources are resolved. A valid incomplete profile
may still be saved for setup; the regular installer and launcher retain their own
admission checks.

## API

All endpoints use the normal authenticated portal and CSRF handling. Saves and
creates use the portal audit log.

| Route | Contract |
|---|---|
| `GET engine-settings` | Configuration version, configured profiles, supported presets, worker choices, controls, assets and previews. Uses existing cached runtime state; does not poll a node. |
| `GET engine-settings?profile=NAME` | The same document restricted to one configured profile. |
| `POST engine-settings/validate` | Validate a patch or proposed creation and return its diff and launch preview; writes nothing. |
| `POST engine-settings/save` | Apply a reviewed patch to an existing profile. Requires the version last read. |
| `POST engine-settings/create` | Create one named profile from a supported preset; no launch or installation side effects. Requires the version last read. |

An edit is a partial merge:

```json
{
  "version": "<64-character configuration SHA-256 returned by GET>",
  "profile": "chat",
  "changes": {
    "port": 18502,
    "overrides": {"--http-default-max-tokens": 512},
    "advanced": {"mtp_ngram_draft": "off"}
  }
}
```

Use `null` to remove one override, for example
`"advanced":{"mtp_ngram_draft":null}`. Setting `changes.port` also removes a
legacy port argument override so that the dedicated field and router cannot
silently disagree.

Create a replica profile with `{version,id,preset,nodes}` or a pair with
`{version,id,preset,rank0,rank1}`. Optional creation fields are `port`, `assets`,
`api_key_files`, and `changes`. For preview, send the same payload to `/validate`
with `"op":"create"`; `profile` may be used as an alias of `id` there. A
`release_id` cannot be supplied through creation: installation resolves it.

`changes.assets` is a map of machine IDs to asset roles. Each provided entry
replaces only that role's metadata and preserves other machines and roles. The
asset schema is `{path,bytes,sha256,control_path?}`. Only required roles of the
selected preset and its workers are accepted. Full schema validation rejects
relative paths, malformed hashes, unknown fields and invalid byte counts. The
optional `control_path` is useful for a bounded calibration-token file also needed
on the controller. `null` removes the selected record without deleting any file.

`changes.api_key_files` accepts node-local key-file paths, never key contents,
only for a preset with key-file support. The usual key provisioning and router
configuration flows remain authoritative.

Validation returns `valid`, `error`, `version`, `changes`, `configuration`,
`controls`, `assets`, `qualification`, and `preview`. A valid configuration can
have `preview.rendered:false` when setup is incomplete or launch-managed resources
have not been resolved. `preview.pending_setup` lists actionable setup errors;
`preview.generated_at_launch` lists resources provided by the launcher. Save returns the new
version and `saved:true`; `runtime_applied` remains false. Clients should preserve
an unsaved form when a version conflict occurs, reload the current document and
let the operator review the changes again.

## Concurrent changes and active engines

An update uses the existing operations configuration mutex, cross-process
`ConfigLock`, SHA-256 version comparison and atomic writer. The previous file is
kept as `.prev` by that writer. It also takes the launcher locks for affected
profiles and checks saved launch state, active jobs, benchmark admission and the
workers' authoritative lease records before replacement. Shared asset changes
check every affected worker and lock the profiles that use it. A second version
check immediately before replacement catches external changes during the reads.

An unreadable lease state blocks an update. A first-run node without a helper is
accepted only when the existing orchestration probe positively establishes that
the helper is absent. A new profile without shared asset edits can be created
without a node call because it only adds an inert configuration entry. Every
actual launch still validates its workers, release, assets and leases.

The page's cached status is an indication for the UI; the save checks again. It
does not stop a live profile to apply a setting. Once stopped, edits take effect
on the next launch. It does not unload kernel drivers or restart machines.

## Engine integration findings

The engine's newer upstream contracts are useful for the next revision of the
adapter, but must not be silently applied to these older presets. Read-only review
of engine commit `51b42906ebbb55edf126dc3471e58b6e5892d85b` found:

- [`scripts/pair/ds41-pair`](https://github.com/hyukudan/strix-halo-engine-lab/blob/51b42906ebbb55edf126dc3471e58b6e5892d85b/scripts/pair/ds41-pair)
  already implements resumable model downloads, verification, dependency builds,
  preflight, paired launch, logs and smoke checks. Its dependency pins form a
  compatible recipe; newer commits in each repository are not interchangeable.
- [`--describe-json` and `--describe-capabilities-json`](https://github.com/hyukudan/strix-halo-engine-lab/blob/51b42906ebbb55edf126dc3471e58b6e5892d85b/apps/deepseek41_serve_main.cpp)
  expose DeepSeek's TensorMeld description, per-rank memory and launch templates.
  They are not universal options implemented by every engine family.
- [`--print-effective-config`, profiles and the option catalog](https://github.com/hyukudan/strix-halo-engine-lab/blob/51b42906ebbb55edf126dc3471e58b6e5892d85b/include/strix/engine_profile.hpp)
  distinguish configured, pending, provisional and unqualified settings. The
  descriptive `docs/optimizations.json` catalog is not a typed editable schema.
  In that revision a `fast` profile may select provisional arithmetic; it must
  not be labelled bitwise exact or fully qualified by the panel.
- [`/v1/capabilities` and server contracts](https://github.com/hyukudan/strix-halo-engine-lab/blob/51b42906ebbb55edf126dc3471e58b6e5892d85b/docs/serving.md)
  describe executed features. The current settings document exposes only the
  pinned adapter's declarations and reports runtime capabilities as unknown;
  it does not manufacture a live capability result from a preset.
- [`Service health`](https://github.com/hyukudan/strix-halo-engine-lab/blob/51b42906ebbb55edf126dc3471e58b6e5892d85b/docs/operations/service-health.md)
  can distinguish HTTP reachability from device readiness. `ready:false` while
  idle or during a long prefill is not, by itself, a reason to restart an engine.
- [`H3`](https://github.com/hyukudan/strix-halo-engine-lab/blob/51b42906ebbb55edf126dc3471e58b6e5892d85b/docs/models/minimax-h3/README.md)
  has a finite audio/video runner. Its `--fast-vae` chooses a different decoder
  identity, not a generic CPU/GPU device switch. It needs its own qualified
  adapter before controls appear in the chat-engine form.

This change does not promote upstream engine pins, add unqualified model families
or claim new hardware qualification. The next adapter extension should consume
verified release capability evidence and preserve requested versus effective
settings, restart scope and model/cache identity.

## Verification

`tests/test_engine_settings.py` exercises real preset rendering and override
removal; known context limits and type rejection; creation without fabricated
assets or releases; partial shared asset edits; inactive/unknown/live lease cases;
profile and configuration locks; concurrent stale writes and job races. These are
host-side contract tests. They do not establish inference quality or GPU timing.

```sh
python -m pytest -q tests/test_engine_settings.py tests/test_config.py tests/test_knobs.py tests/test_render.py tests/test_ops_ops.py
```
