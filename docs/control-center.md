# Control center: setup, node roles and updates

This change builds on `rename/hydraweave` (`adead58`), which contains the project rename and the three-headed
Hydra logo. The GitHub repository is `hyukudan/hydraweave`; `hyperloom` is the compatibility name. The rename
is the existing PR #8, separate from the control-center work.

## The intended deployment

One controller owns the workspace configuration, trusted host keys, component cache, job history and portal.
Each Linux inference node runs the small existing node helper through pinned SSH. The helper installs builds,
supervises engine processes and reports observations. No additional unauthenticated node HTTP API is needed.

The controller, request router and inference rank 0 are different roles:

| Role | What runs there | Placement |
|---|---|---|
| Controller | Hydraweave CLI, portal (loopback by default), configuration and control jobs | Windows workstation or Linux machine |
| Router | TensorMeld request endpoint and routing | Controller or supported configured Linux SSH node |
| Inference worker | Engine, model files and GPU allocations | Configured Linux SSH worker |
| Rank 0 | First worker of a paired engine; usually the pair's HTTP endpoint | Chosen by the deployment profile |
| Rank 1 | Other worker participating in tensor parallelism | Other node in that profile |

For a dedicated cluster, a Linux machine that stays on is a useful controller. It can also be a worker, but
the worker still needs an explicit SSH entry: the current installer requires SSH even when that host also
runs the controller. The `local` entry describes the controller; it is not an automatic GPU grant.

For interactive work, the Windows workstation remains a valid controller. A remote router can avoid
putting the inference data path through the workstation, subject to the selected engine's constraints.
Moving the router does not move the portal or its state.

The portal binds `127.0.0.1:18700` by default. There are two ways to use a controller that has no screen.

**Listen on the network** (like ComfyUI's `--listen`), for example on a headless Strix Halo node:

```sh
hydraweave ui --listen 0.0.0.0 --port 18700 --no-browser
```

Then open `http://<node address>:18700` from any machine on the LAN. `--listen` takes `0.0.0.0` or `::` for every
interface, or one interface address. The same value can be saved as `pc.ui_listen` (for example `"0.0.0.0:18700"`);
the command-line options win. **There is no login**: anyone who can reach that address can configure and start or
stop the cluster, and the portal prints a warning saying so. Use it only on a trusted network. The portal still
refuses requests whose `Host` is a DNS name other than this machine's own names (IP addresses are accepted), so a web
page elsewhere cannot rebind its name to the portal.

A Linux controller that is also a worker polls and runs its own node tool as a local child, with the same commands
an SSH session would run, so it needs no SSH key to itself. Hydraweave treats an `ssh` machine as this host when its
`http_address` is bound to one of the controller's interfaces and its `ssh_target` user (if any) is the user running
Hydraweave. Set `"local": true` or `"local": false` on the machine to decide explicitly.

**Or keep it on loopback and tunnel**:

```sh
# On the controller, from its checkout and Python environment:
hydraweave ui --config ~/.config/hydraweave/cluster.json --no-browser

# On the desktop; use your existing SSH alias and trust configuration:
ssh -N -L 18700:127.0.0.1:18700 halo-controller
```

Open `http://127.0.0.1:18700` on the desktop. The tunnel forwards the portal; it does not make the desktop an
inference worker. Use only one active controller for a workspace; automatic leader election/failover is not
implemented.

## First-run workflow

1. Open **Home** and create the workspace. This creates valid local configuration with no invented GPUs,
   model files, hashes, releases or deployed workers.
2. Add Linux nodes using the visual form. Set the host, SSH target, installation folder and the lab directory
   used by the bounded GPU turn manager for a normal **Start profile** launch.
   Pin host fingerprints after comparing them with the actual machines, then test connectivity.
   For a pair, add both nodes first, then add the node-side SSH alias from the intended rank 0/build node
   to its peer. Before the first installation, an unknown node state permits only adding new aliases;
   existing aliases and all identity fields stay protected. Saving an alias does not test the connection.
3. Create an engine profile from a supported preset. Choose one or more replicas or the two ordered ranks.
4. Assign the model assets by node: absolute paths, expected sizes and SHA-256 identities. These are declarations;
   a saved form is not proof the file exists or was verified.
5. Review **Components**. Update node helpers and install the profile. The existing installer fetches its exact
   engine/Trellis/TensorMeld revisions, builds on the chosen node, distributes verified binaries and activates
   the stopped profile. A source download on the controller is a separate staging operation.
6. Preview engine settings, save them and start the profile. Follow the job through checks, loading and readiness.
   Read logs and machine details when a step fails.

The navigation keeps operational pages close at hand and groups specialist diagnostics under advanced tools.
Existing benchmarks, recipes, telemetry, USB4 diagnostics and other feature pages remain accessible.

**Current node prerequisite:** the regular launch path acquires bounded GPU turns using the existing lab
scripts. Preparing node tools does not create a turn manager. Register its actual `lab_dir` on each lab node;
do not enter a placeholder path. A non-lab machine can be registered and monitored, but its continuous Qwen
service uses the separate [non-lab service workflow](non-lab-mode.md). Paired profiles currently require
the lab lease path. A generic first-time installation of the turn manager and a panel for continuous services
remain integration work; the UI must expose this prerequisite before a user attempts to launch.

## Lifecycle and the master question

There is no requirement that the portal run on inference rank 0. Keep these lifecycle facts distinct:

- Closing a browser tab does not issue a profile stop. Some browser-owned diagnostic jobs have their own
  abandonment policy; the normal model launch is not a browser rendering process.
- Node supervisors own engine process groups. In lab mode, the engines remain bounded by their GPU lease.
  Losing an SSH session is not equivalent to an unbounded persistent service.
- The local router requires its controller host to remain online. Remote router placement is explicit and
  does not change GPU lease duration.
- Persistent `systemd --user` engine service mode exists for non-lab Qwen replicas. Paired engines remain
  lease-based, and the router is not included in those Qwen units. See [non-lab mode](non-lab-mode.md).
- MiniMax's loopback-only preset requires the router on rank 0. Current recipe orchestration and loopback
  RTX prefill paths may require the router on the controller. The editor checks preset constraints; explicit
  recipe launch placement remains authoritative.

Saving router placement now affects default CLI and portal launches. An explicit `--router` still wins.
Changing the default does not migrate a live router; stop the owned profile before saving a different placement.

## What was missing and what changed

| Finding in the starting code | Implemented behavior |
|---|---|
| The existing project rename/logo was still on a separate branch | Build on that work and preserve legacy compatibility |
| No actionable initial state when `cluster.json` was absent | Home explains the state and creates a valid workspace without overwriting existing files |
| Node setup exposed structured JSON as a normal user task | Visual machine editor preserves advanced configuration and existing validation |
| The first installation could require a peer route that the frozen editor could not add | New peer aliases can be added without changing existing aliases, machine identity or active-launch protections |
| Engine parameter controls were read-only | Typed profile forms, preview, save and creation, with optimistic version checks |
| Router defaults were configured but `up` still defaulted to local | CLI and portal resolve the saved placement before launch admission |
| The saved router port was ignored by the actual launch | The rendered endpoint uses the selected host and saved port |
| The portal still rejected paired engines as a later phase | Supported pairs reach the existing launcher, preserving rank order, router constraints and preflight |
| The first-run form could create a node that normal Start could never use | Lab turn-manager prerequisites are explicit and missing prerequisites block Start before remote work |
| No component-wide upstream update view | Explicit checks for engine, TensorMeld, Trellis and USB4 driver |
| Source checkout, build and activation were easy to confuse | Separate upstream/pinned/downloaded/active evidence and an installation plan |
| Too many equally prominent navigation entries | Operational entry points plus expandable specialist tools |
| Generic jobs could offer profile-stop controls | Cancellation names the actual job; profile controls remain attached to profiles |
| Portal exit could leave a component Git process running | Feature jobs are cancelled and given bounded cleanup while detached engine launches retain their lifecycle |

## Engine controls and compatibility

The engine upstream has grown beyond the older revisions qualified by these Hydraweave presets. In the audit,
engine `main` was `51b42906ebbb55edf126dc3471e58b6e5892d85b` (2026-11-10); local earlier audit checkouts were older.
Its `--describe-json`, `--describe-capabilities-json`, `--print-effective-config`, `/v1/capabilities` and platform
probe are the next integration contracts. They must be tested per executable and revision; not every family
implements the same flags, defaults or lifecycle.

The source catalogue's 274 optimization entries are documentation, not a typed executable settings schema.
Some contain descriptive expressions, multiple environment variables or bitmasks. This UI edits only the
current preset's validated controls. Auto removes an override and retains the preset's launch line; it does
not silently replace the preset with upstream `main` or override fixed qualified context limits.

The displayed preview is produced by the existing launcher and is labelled as a preview. Unsupported options
are explained rather than rendered as inert toggles. The current engine's `fast` profile can contain provisional
or numerically different behavior; it must not be sold as an exact/qualified speed button. Checkpoint compatibility
also depends on binary/model/numerical identities when rolling to a new release.

## Updates and the driver

**Check** contacts allowlisted component remotes and records their full revision. **Download** stages a chosen
configured or recently checked SHA, verifies repository identity and revision, and atomically exposes a complete
checkout. Interrupted downloads leave no apparently complete release. Existing dirty checkouts are refused,
not reset. Jobs report progress, timeout, cancellation and errors.

**Install profile** uses the existing node installer and profile pins, including profile-specific overrides of
`versions.lock`. A newer source commit does not prove it matches a Trellis ABI, driver UAPI, build recipe or model
pack. The update view never changes all repositories to their latest independent heads.

The USB4 driver's source can be fetched and staged. Kernel build/install, privileged device changes and reboot
are separate maintenance, with a distinct verification step. The engine's current `scripts/pair/ds41-pair`
already includes DeepSeek-specific driver/build/fetch/check workflows; a future qualified adapter should use
those contracts instead of duplicating their pinned kernel/driver rules. This change does not claim a staged
driver is loaded, and does not alter the user's nodes from the development environment.

## Validation boundaries

Host-side tests cover configuration persistence and stale forms, existing runtime-state guards, router
precedence, renderer parity, fixed context bounds, source revisions, dirty checkouts, cancellation and partial
failure. A JSDOM walkthrough exercises first-run navigation and working forms against the real portal,
with hardware access disabled in the fixture. Chromium could not start in the development environment
because its local socket creation is prohibited, so visual browser acceptance is still pending. These checks
do not validate ROCm builds, USB4 performance, numerical fidelity or an actual multi-node activation.

Remaining hardware acceptance is explicit: bootstrap a real node, build one supported profile, verify its assets,
start/stop and reconnect, exercise a two-node failure/rollback, and confirm which persistent modes the selected
engine supports. Extending latest GLM/MiniMax/H3 or every new engine toggle requires the corresponding adapter
and qualification; a finished settings form cannot substitute for that contract.
