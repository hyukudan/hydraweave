# Node discovery, pairing and provisioning

An alternative to describing every node by hand ([Installation](install.md) sections 4-6): run one command as
the normal login user on each Linux worker, then adopt it from the controller with its console pairing code.
The controller is installed as in [Installation](install.md) section 3 and serves the portal on the LAN
(`hydraweave ui --listen 0.0.0.0 --port 18700 --no-browser`, or `pc.ui_listen`).

## 1. Run one command per worker

Log into each Ubuntu worker as its ordinary user, not root:

```sh
curl -fsSL http://CONTROLLER:18700/node/bootstrap.sh | HYDRAWEAVE_PKG_SHA256=<sha256-from-portal> sh
# Lab mode: also set HYDRAWEAVE_LAB_DIR="$HOME/<lab>" before sh.
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
Rerunning `install-service` restarts a running agent so it picks up upgraded code. It refuses (exit 3) while an
engine lease started through the agent is still live; stop the engine first or pass `--force`.
Opt out persistently with `hydraweave-node install-service --no-announce`, or set
`HYDRAWEAVE_NO_ANNOUNCE=1` in the user unit environment. Foreground agents accept `serve --no-announce`.
`HYDRAWEAVE_NO_DISCOVERY=1` disables controller browsing.

The agent listens on every interface (`--listen 0.0.0.0`) and prints the same LAN warning as the portal.
Every node call needs the bearer token obtained by pairing, and pairing needs the console code; Host headers
must be IP literals, so a web page cannot rebind a DNS name to the agent. A paired controller can run the
same node verbs as the SSH route, so pair only controllers you would give SSH access.

## 2. Adopt workers

The one-use pairing reply includes a best-effort inventory: lowercase OS name, machine architecture, and device
records with stable per-reply IDs, type, vendor, name, memory in bytes, and driver. The agent probes AMD sysfs
and `nvidia-smi` without root and uses short timeouts; unavailable probes are ignored. A host with no detected
GPU reports a CPU device. Device types are `amd`, `nvidia`, `intel`, `apple`, `cpu`, and `other`. Older agents
that omit inventory are adopted with the legacy Linux/AMD placeholder.

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
The portal shows the source package's SHA-256 next to the bootstrap command. Copy the complete command:
the script downloads to a private temporary file, checks SHA-256 using `sha256sum`, `shasum -a 256`, or
Python 3, and aborts on mismatch before creating a venv or running pip. The temporary download is removed
on exit. Without `HYDRAWEAVE_PKG_SHA256`, it prints a note and checks the hash embedded in the served script.
The controller caches the exact package and digest until source file mtimes, sizes, or the file list change.
Reload the portal and copy a fresh command after updating controller sources.

### Portal access security

On a non-loopback bind the portal generates an access token on first start and prints
`http://HOST:PORT/?token=...`. Open that URL once to receive an `HttpOnly; SameSite=Strict` session cookie;
the redirect removes the token from the address bar. Pages remain readable without login. Mutating portal
requests require that cookie or `Authorization: Bearer <portal-token>`; the UI shows a login form after a
401 response. Browser cookies also retain the existing Origin and CSRF checks. Bearer callers supply
the explicit credential instead of browser CSRF headers.

The token persists in `~/.config/hydraweave/secrets/portal-access` with mode 0600 on POSIX. Windows stores
it in `%APPDATA%\hydraweave\secrets\portal-access`, protected by the current user's DPAPI and private ACL.
Loopback binds need no access token. `--no-auth` or `HYDRAWEAVE_NO_AUTH=1` disables portal authentication
and shows both a console warning and a visible LAN warning banner.

Agent/bootstrap routes retain their own access rules:

- Portal `GET`/`HEAD /node/bootstrap.sh` and `/node/package.tar.gz`: public, so unpaired workers can install.
- Worker `POST /pair`: the worker's short-lived, single-use console pairing code.
- Worker `POST /call`: the worker's paired bearer token, distinct from the portal token.
- Portal adoption/provisioning actions: authenticated portal mutations, including `discovery/adopt` and
  `discovery/provision`.

Use a trusted LAN or a secure tunnel: the downloaded script, login token, and agent credentials still
travel over HTTP. The package hash assumes the displayed hash and bootstrap script came from your trusted controller.

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


## Add a node

Open Home and choose **Add node**. Discovered nodes can be adopted with the short code printed by the node agent; confirm the displayed fingerprint by selecting the matching read-only value. If discovery is unavailable, use the existing address form and verify the SSH host key before trusting the node.
