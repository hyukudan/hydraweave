# Node discovery, pairing and provisioning

An alternative to describing every node by hand ([Installation](install.md) sections 4-6): run one command as
the normal login user on each Linux worker, then adopt it from the controller with its console pairing code.
The controller is installed as in [Installation](install.md) section 3 and serves the portal on the LAN
(`hydraweave ui --listen 0.0.0.0 --port 18700 --no-browser`, or `pc.ui_listen`).

## 1. Run one command per worker

Log into each Ubuntu worker as its ordinary user, not root:

```sh
curl -fsSL http://CONTROLLER:18700/node/bootstrap.sh | sh
# lab mode: curl -fsSL http://CONTROLLER:18700/node/bootstrap.sh | HYDRAWEAVE_LAB_DIR="$HOME/<lab>" sh
journalctl --user -u hydraweave-node -n 20 --no-pager
```

Replace `CONTROLLER` with the controller's LAN address. The controller serves its own Hydraweave source
package; publishing is unnecessary. Bootstrap upgrades `~/hydraweave/agent-venv` and enables
`hydraweave-node.service` with `systemctl --user`. Nodes support Python 3.10+. Existing `etc`, `incoming`,
`lab-arm`, `log` and `releases` stay in place. For lab mode, run bootstrap with `HYDRAWEAVE_LAB_DIR=<lab>` set (the directory holding the turn scripts);
without it the agent runs in non-lab mode.

Bootstrap prints one administrator block for missing Python/venv, DKMS, kernel headers, render/video
permissions, udev rules and login lingering. It never runs `sudo`. Review and run the block yourself only
when required. `amdgpu-dkms` needs your approved AMD repository first; match the package to your kernel,
GPU and ROCm version. Secure Boot may require module signing/enrollment. Current engine recipes require
ROCm core 10.1 at `/opt/rocm/core-10.1`, CMake ≥3.24 and Ninja.
The lab's USB4 `thunderbolt_stream` DKMS package/layout must come from its maintainer: this repository
contains its reader, not that driver's source or a qualified stock-kernel driver package.

With an available wheel/index, the alternative is:

```sh
pipx install 'hydraweave[node]'
hydraweave-node install-service
```

`pipx install` installs entry points; the second command registers the service. The optional `node` extra
installs python-zeroconf plus user-local CMake/Ninja, avoiding Ubuntu 22's older CMake. Without zeroconf the
stdlib mDNS implementation advertises the same DNS-SD service.
Opt out persistently with `hydraweave-node install-service --no-announce`, or set
`HYDRAWEAVE_NO_ANNOUNCE=1` in the user unit environment. Foreground agents accept `serve --no-announce`.
`HYDRAWEAVE_NO_DISCOVERY=1` disables controller browsing.

The agent listens on every interface (`--listen 0.0.0.0`) and prints the same LAN warning as the portal.
Every node call needs the bearer token obtained by pairing, and pairing needs the console code; Host headers
must be IP literals, so a web page cannot rebind a DNS name to the agent. A paired controller can run the
same node verbs as the SSH route, so pair only controllers you would give SSH access.

## 2. Adopt workers

Open `http://CONTROLLER:18700`. Home shows **discovered, not adopted** nodes. Click Adopt and enter the
eight-digit code from that node's journal/console. Codes expire after ten minutes, are rate limited and
single use. Restart an unpaired agent for a new code. Codes never appear in LAN TXT records.
Mutual USB4 peer UUIDs produce pair suggestions; pair each node separately.

```sh
hydraweave nodes discover --wait 5
hydraweave nodes adopt NODE_ID          # privately prompts for the console code
```

Adoption preserves an existing host's ID (for example `node-a`), SSH access/aliases, install root, lab
directory, devices, assets and profiles. Ambiguous matches are refused. Credentials are private files
beside controller config (`node-agents.json`) and on the node (`etc/agent-pairing.json`), separate from
cluster JSON and audit records. Provisioning uses the paired agent even when the host retains SSH access.

## 3. Install engine and models

Adoption starts a visible provisioning job for profiles already assigned to that worker. For a new host,
choose a supported model and its nodes in Engine configuration, then click Install under the discovered
node. Discovery cannot choose which weights you want.

```sh
hydraweave nodes provision node-a PROFILE --plan
hydraweave nodes provision node-a PROFILE
# Unpublished revisions use the existing verified Git-bundle path:
hydraweave nodes provision node-a PROFILE --source engine=/path/to/engine@FULL_COMMIT
```

Plans name exact engine pins, recipes, model files/checksums, lab model roots and blockers. Provisioning
uses the shared model-library scan and registered lab models. New downloadable single-file assets go into
dedicated `~/hydraweave-models/<preset>/<asset>` directories. Downloads check disk headroom, resume
partials and verify size/SHA-256 before exposing final files. Existing models are verified with guarded
hashing jobs and never overwritten. Private models, multi-file assets and required conversions without
a qualified automatic path remain explicit blockers.

The installer builds pinned sources or verified source bundles and runs only selected host tests.
It activates the completed release for the stopped profile. Jobs shows progress and bounded node log
tails. Retry failed downloads to resume. A GPU turn or active lease may block work; retry after it ends.
Provisioning never executes an inference binary.

Lab mode retains `<lab>/.gpu-turn`, `gpu-turn.sh` and `gpu-turn-pair.sh` through the existing
bounded leases. For non-lab Qwen serving use the [user service workflow](non-lab-mode.md).
Paired engines still require the existing lab manager, peer routes, USB4 driver and stream layout.

## 4. Verify, upgrade and troubleshoot

```sh
# Worker
systemctl --user is-active hydraweave-node
journalctl --user -u hydraweave-node -n 30 --no-pager
test -r /dev/kfd && ls -l /dev/dri/renderD*
dkms status

# Controller
hydraweave nodes discover --wait 5
hydraweave doctor node-a
```

Rerun bootstrap to upgrade in place; pairing and node state are retained.
Stop advertising with `systemctl --user stop hydraweave-node`.
If controller credentials are lost: stop the agent, remove only `etc/agent-pairing.json`, restart and
pair again. Retain `etc/agent-id` and all other configuration/logs.

Use a trusted controller: bootstrap, pairing and agent control currently use private-LAN HTTP without TLS.
Discovery uses IPv4 mDNS/DNS-SD `_hydraweave._tcp.local.` on UDP 5353. Workers default to TCP 18701;
controller UI/bootstrap uses TCP 18700. Allow these only between trusted hosts. mDNS generally does not
cross VLANs; retain explicit SSH configuration there. If the default worker port is occupied, the agent
selects a free port and shows it in the journal/DNS-SD record. Pin `--port` in its service when a firewall
needs a fixed port.

## 5. Local node packages

With fpm on the build host (no root or publishing):

```sh
python packaging/build.py --format deb --dry-run
python packaging/build.py --format rpm --dry-run
python packaging/build.py --format deb
python packaging/build.py --format rpm
```

Packages ship the Python implementation and a systemd **user** unit. After the administrator installs
the package, its user runs `systemctl --user enable --now hydraweave-node`.
Packages do not enable a root service or install a driver.
