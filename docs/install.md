# Installing Hydraweave from scratch

This guide takes a control machine with nothing installed to a validated configuration and a passing
`doctor` on every node. Historical checks are listed in [How this guide was checked](#how-this-guide-was-checked);
the release installer is covered by hardware-free unit tests.
Running a profile on the GPUs (`install`, `up`) needs the node toolchain and, in lab mode, the lab's
turn scripts; those steps are listed at the end.

`hloom` is a short alias for `hydraweave` everywhere.

## 1. What you need

**Control machine** (where you run `hydraweave`; Linux and Windows are supported):

| Need | Windows | Linux |
|---|---|---|
| Python | controller requires ≥ 3.11 (node agent ≥ 3.10); the Windows helper requires ≥ 3.11 (`py` or `python` on PATH) | `python3` ≥ 3.11 with `venv` and `ensurepip` (node-only `--node`: ≥ 3.10) |
| git | Git for Windows | distribution package |
| ssh client | the Windows OpenSSH client (Settings → Optional features), plus Git for Windows' copy, see [Troubleshooting](#troubleshooting) | `openssh-client` |
| ssh key | a key in `%USERPROFILE%\.ssh` (for example `id_ed25519`) whose public half is in each node's `~/.ssh/authorized_keys` | the same in `~/.ssh` |
| network | the nodes' private LAN addresses; PyPI once for `setuptools`, unless the release has `wheels/` | same |

**Each node** (Linux; `hydraweave doctor <machine>` checks all of this and lists every command it ran):

| Need | Checked by doctor as |
|---|---|
| `python3` ≥ 3.10, `git` | `python`, `git` |
| `cmake` ≥ 3.24, `ninja` | `cmake`, `ninja` |
| ROCm SDK with `amdclang++` for source builds; root detected automatically (see below) | `rocm_root`, `rocm_version`, `rocm_package_version`, `compiler` |
| a gfx1151 GPU visible in sysfs | `gfx_targets` |
| disk space for sources, builds and releases | `disk` |
| lab mode only: the lab directory with `gpu-turn.sh` / `gpu-turn-pair.sh` | `lab` |
| DeepSeek pair only: the USB4 stream driver and layout 1 | `usb4` |

Hydraweave never installs or changes any of these, needs no root, and never touches the kernel, drivers or system units.

## 2. Get Hydraweave

### Versions and node compatibility

`hydraweave --version` reports the installed package version and, when the source checkout is available, its Git commit. A checkout uses `git describe --tags --dirty --always`; an unavailable package and Git version fall back to `0.0.0+unknown`. `pyproject.toml` keeps the static setuptools build version so builds need no additional versioning dependency.

The Nodes view compares an agent's announced version with the controller. A different release is shown as **Out of date**; local and development suffixes do not count as a release difference. An absent version is shown as unknown. `versions.lock` records component pins, and CI warns when those commits differ from the repository's submodule gitlinks. Run `python tools/check_versions_lock.py --strict` to make drift fail locally.

### Linux one-line installer

```sh
curl -fsSL https://github.com/hyukudan/hydraweave/releases/latest/download/install.sh | sh
```

**No GitHub release exists yet.** Releases are produced by pushing a `v*` tag that matches
`pyproject.toml` (for example `v0.1.0`). The release workflow builds a wheel, sdist, source archives,
`install.sh` and `SHA256SUMS`. A manual workflow run only builds artifacts and never publishes,
regardless of its `dry_run` input. Until the first tag release, use a source checkout below.

The installer resolves the latest GitHub release, verifies the wheel's SHA-256 against that same
release's `SHA256SUMS`, and stages a separate environment in
`~/.local/share/hydraweave/venvs/<id>` (`<id>` includes the version and wheel digest).
It writes `~/.local/bin/hydraweave` and prints a PATH instruction if needed. No root command runs.
If Python venv support is missing, it prints `sudo apt-get install python3-venv` for you to run.
Git and SSH are needed for cluster operations, not for installing the wheel.

Download `install.sh` once to pass options:

```sh
sh install.sh --dry-run
sh install.sh --version v0.1.0 --node
sh install.sh --prefix "$HOME/tools/hydraweave"
sh install.sh --from-file ./hydraweave-0.1.0-py3-none-any.whl
```

`--node` installs the wheel's node extras and adds a `hydraweave-node` shim. Start, pairing and
service configuration remain explicit; see [Node discovery](node-discovery.md). `--prefix` replaces
`~/.local`, including both `share/hydraweave/venvs` and `bin`. Dry runs resolve metadata (or verify
local wheels) without downloading release assets or writing an environment or shim. An existing
version environment is left intact and reported as already installed.

### Linux releases, updates and rollback

The data root is `<prefix>/share/hydraweave` (default prefix `~/.local`):

```text
releases/<id>/release.json   # id, version, commit, created, wheel_sha256, extras
venvs/<id>/                # independent Python environment per wheel
current -> venvs/<id>       # atomically replaced symlink
previous                   # previous release id
```

The shims in `<prefix>/bin` always run through `current/bin`; activation does not
rewrite them. The standalone installer embeds `hydraweave/releases.py` verbatim;
maintainers regenerate it with `python tools/embed_installer.py`.

```sh
hydraweave releases list
hydraweave releases activate <id>
hydraweave releases remove <old-id>
hydraweave update --check
hydraweave update
hydraweave update --version v0.1.0
hydraweave update --wheel ./hydraweave-0.1.0-py3-none-any.whl
```

`list` marks current and previous releases; removing the current release is
refused. For a custom prefix use `hydraweave releases --prefix /path list` or
`hydraweave update --prefix /path`. An installed venv infers its prefix; source
checkouts can also set `HYDRAWEAVE_PREFIX`.

Update verifies the release wheel against `SHA256SUMS`, creates a new venv,
installs the release and its compatible lock/package resources, then activates
it and runs the new `hydraweave --version`. A local `--wheel` requires adjacent
`SHA256SUMS` and uses the same verified, binary-only offline dependencies as the
installer. Node extras are retained from the active release. `--check` only
reads metadata and clearly reports when no release is published.

If a portal user service (`hydraweave-portal.service`, `hydraweave-ui.service` or
`hydraweave.service`) is active, update restarts it and polls `/api/health`.
Configure its `ExecStart` to use the stable `<prefix>/bin/hydraweave ui` shim.
The default health URL is `http://127.0.0.1:18700/api/health`; portal-triggered
updates use the portal's actual listen port. Portal updates run in a separate
transient user unit so restarting the portal cannot interrupt rollback; progress
and the result persist below `update-jobs/` and reappear after reconnecting.

A failed health check automatically restores the old `current` pointer, restarts
the old portal service, and reports **rolled back** with the failure reason.
A failed initial installation reports that no previous release exists. Successful
updates retain three releases by default, always protecting current and previous;
the shared manager's `keep` setting accepts a minimum of two. Existing GPU engine
deployments remain under their profile installer and are not restarted by this
controller update. No root command runs automatically.

For paired agents that discovery reports as **Node out of date**, **Update node**
re-pulls the controller's `/node/package.tar.gz`, verifies its digest from the
authenticated request, reinstalls the node package and restarts its user service.
The agent refuses this action while a GPU lease or turn is live, or turn state
cannot be checked. Stop the active work before retrying; the portal must be
reachable from the node at the browser's origin.

For offline installation, put the wheel, `SHA256SUMS` and any dependency wheels in the same directory.
All sibling wheels must appear in `SHA256SUMS` and pass verification. Pip uses `--no-index` and only
binary wheels. Node-only installs on Python 3.10 require a `tomli` wheel; `--node` also needs its optional dependencies.
The wheel includes the controller and node agent; a local TensorMeld router still needs the pinned
source supplied by the source archive or checkout.

### From a release archive

A release is `hydraweave-<version>-g<commit>.zip` (Windows) or `.tar.gz` (Linux) with a `SHA256SUMS` file.
It contains the product and the pinned TensorMeld source; every file is listed with its sha256 in `RELEASE.json`.

Check the archive before unpacking it:

```powershell
# Windows (PowerShell)
Get-Content SHA256SUMS
certutil -hashfile hydraweave-<version>-g<commit>.zip SHA256
Expand-Archive hydraweave-<version>-g<commit>.zip -DestinationPath .
```

```sh
# Linux
grep tar.gz SHA256SUMS | sha256sum -c -
tar xzf hydraweave-<version>-g<commit>.tar.gz
```

### From a git checkout

Only the TensorMeld submodule is needed on the control machine
(the engine and strix-trellis are fetched and built on the nodes by their pinned SHAs):

```sh
git clone https://github.com/hyukudan/hydraweave.git
cd hydraweave
git submodule update --init --depth 1 third_party/tensormeld
```

## 3. Install on the control machine

### Windows

From the unpacked release (or the checkout), in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -WhatIf   # shows the interpreter, venv, pip arguments and shortcut; changes nothing
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
& "$env:LOCALAPPDATA\hydraweave\hydraweave.cmd" --version
```

What it does:

- picks one interpreter (`py -3.13`, `-3.12`, `-3.11`, then `python`) and checks `git` and `ssh.exe`;
- **release:** verifies every file against `RELEASE.json` (a changed, missing or extra file stops the install before anything is written),
  copies the release to `%LOCALAPPDATA%\hydraweave\releases\<name>`, creates `%LOCALAPPDATA%\hydraweave\venvs\<name>`,
  and makes it the active release;
- **checkout:** one venv at `%LOCALAPPDATA%\hydraweave\venv` with an editable install of the checkout;
- installs TensorMeld from the bundled pinned source, then Hydraweave, from the verified copy; with a `wheels\`
  folder (verified like every other file), pip runs offline;
- writes the launchers `%LOCALAPPDATA%\hydraweave\hydraweave.cmd` and `hloom.cmd`, and a Start-menu shortcut
  **Hydraweave** that opens the portal.

Add `%LOCALAPPDATA%\hydraweave` to your user PATH if you want to type `hloom` anywhere; the rest of this guide assumes you did.

### Linux from source (advanced)

```sh
cd hydraweave-<version>-g<commit>            # or the checkout
python3 -m venv ~/.local/share/hydraweave/venv
~/.local/share/hydraweave/venv/bin/pip install ./third_party/tensormeld -e .
mkdir -p ~/.local/bin
ln -s ~/.local/share/hydraweave/venv/bin/hydraweave ~/.local/bin/hloom   # optional
hloom --version
```

Keep the unpacked directory: the install is editable, and the node tool is copied from it.

For this manual source path, `sha256sum -c` checks the archive as a whole. Unpack into an empty directory
and do not edit it. The one-line installer above instead verifies the wheel before invoking pip.

### ROCm discovery and optional engine assets

Builds, runtime manifests and node inspection share one detector: an existing `$ROCM_PATH`, then
`/opt/rocm` (normally a symlink to the installed SDK), then the numerically newest directory matching
`/opt/rocm-*` or `/opt/rocm/core-*`. If `/opt/rocm` only contains versioned `core-*` children, it is
treated as a parent directory. Set `ROCM_PATH` explicitly to select an SDK; the build records that
root and library paths in its manifest. Discovery does not establish hardware compatibility;
the qualified gfx1151 baseline remains ROCm core 10.1.

Repository maintainers can set the `HYDRAWEAVE_BUILD_NODE_ENGINE` Actions variable to `true` to try
building the gfx1151 Qwen engine (`strix-hip-decode-token`) inside `rocm/dev-ubuntu-24.04`. Successful builds attach
`hydraweave-node-engine-<version>-linux-x86_64-gfx1151.tar.gz`, including a manifest, to the tag release
and its `SHA256SUMS`. Compiler failures do not block the Python release. These optional binaries
require a compatible ROCm runtime; this installer does not automatically activate them or replace
profile-specific source builds (including Trellis-enabled profiles).

Linux workers on the same LAN can instead announce themselves and be adopted from the portal with a console
pairing code: see [Node discovery](node-discovery.md). The manual steps below remain the reference.

On Linux, the optional portal user service is managed with `hydraweave service install`, `hydraweave service
status`, and `hydraweave service uninstall`. Install enables it immediately and attempts `loginctl enable-linger
$USER` so it can start without an interactive login; if that command is unavailable, run it yourself.
Node bootstrap prints a pairing code and the command `hydraweave-node pair` to issue a fresh code after the
10-minute expiry.

## 4. Describe the cluster

The configuration lives in `%APPDATA%\hydraweave\cluster.json` (Linux: `~/.config/hydraweave/cluster.json`).
It never holds secrets, only paths. Start from [`examples/cluster.example.json`](../examples/cluster.example.json):

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\hydraweave" | Out-Null
Copy-Item examples\cluster.example.json "$env:APPDATA\hydraweave\cluster.json"
notepad "$env:APPDATA\hydraweave\cluster.json"
hloom config validate "$env:APPDATA\hydraweave\cluster.json"
```

Per node, set:

- `http_address`: the node's **numeric** private IP. Hostnames are not accepted, and the ssh connection also goes to this address.
- `ssh_target`: **`<user>@<name>`**. Hydraweave runs ssh with its own minimal config, so aliases in `~/.ssh/config`
  are not used: only the user part matters, and the host part is a label. A bare name means your local user name.
- `ssh_port` if it is not 22.
- `install_root`: an absolute path the node user owns or can create, for example `/home/<user>/hydraweave`. It must
  not be or contain your home directory, not be inside a git checkout, and not overlap the lab directory or any asset path.
  Hydraweave marks it and writes only inside it (plus `~/.hydraweave-boot` for the node tool).
- `lab_mode` and `lab_dir`: new workspaces default to `lab_mode: false`; in a shared lab, explicitly set `lab_mode: true` and the lab directory that holds the turn scripts;
  every GPU process then runs inside a GPU turn. This guide covers lab mode only.
- `model_roots` (optional): up to 16 absolute directories that hold this machine's models. Without it, a machine
  with a `lab_dir` uses `<lab_dir>/models`. The model library scans these roots, and extra roots inside them can be
  saved from the portal.
- `assets`: per node, each file's path, size in bytes and sha256. Files are never downloaded or converted automatically.

`config validate` rejects unknown keys, non-private addresses and malformed values, and names the field.

More than one configuration (`--config FILE`) can run on the same control machine, but they share one runtime
directory (`%LOCALAPPDATA%\hydraweave\run`, Linux: `~/.config/hydraweave/run`) where each launch state is named by its
profile: **profile names must be unique across those configurations**. An `up` of a profile name that another
configuration is running is refused, and the message names that configuration's path; stop it there
(`hloom down <profile> --config <path>`) or rename the profile in one of them. `status`, `logs` and `metrics` show
the launches of `--config` (default: the default configuration); `--all-configs` shows every configuration's.

## 5. Pair each node (host key)

Hydraweave keeps its own `known_hosts` and connects only to a key you confirmed:

```powershell
hloom hosts scan halo-1                       # candidate keys with their SHA256 fingerprints; nothing is trusted yet
```

On the node itself (console or an ssh session you already trust), print the real fingerprint and compare:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Then pin exactly that key:

```powershell
hloom hosts pin halo-1 --fingerprint SHA256:<the fingerprint you compared>
```

A later change of that host key is a hard failure until you pin again.

## 6. Check every node

```powershell
hloom doctor halo-1
hloom doctor halo-2
```

`doctor` changes nothing on a node. On a node without the node tool it says so (`node_tool.status: absent`) and
stops: the tool reaches a node only through `hloom nodes update <profile> --machine <machine>`, which copies it to
`~/.hydraweave-boot` (fixed commands: `mkdir`, `scp`, `chmod`); a machine without the tool has no lease, so no
`--force` is needed there. With the tool in place, `doctor` runs the read-only checks: each is `ok` or `unavailable`
with the reason, and `commands` lists everything that ran. `node_tool` compares the deployed node tool with this
release's: `stale` means it came from another release; `doctor` does not replace it (a lease may be running on it), so
run `hloom nodes update <profile> --machine <machine>`, which refuses while a lease is live.
Fix whatever your profile needs (section 1) before going on.

## 7. Build the engine and run a profile

These steps use the nodes' CPUs (build) and GPUs (run). In lab mode they follow the lab's rules: builds only
outside measurement turns, and every GPU process inside a GPU turn of at most 20 minutes.

```powershell
hloom install qwen-replicas --build-on halo-1   # fetch the pinned sources, build once, copy to halo-2, verify shas, activate
hloom up qwen-replicas                          # takes the GPU turns, starts the engines and the local router
hloom status qwen-replicas
hloom endpoint qwen-replicas                    # the router address for clients
hloom down qwen-replicas
hloom ui                                        # the portal on http://127.0.0.1:18700
hloom ui --listen 0.0.0.0 --no-browser          # every interface; console prints a token login URL
```

### LAN access security

Non-loopback portal binds protect reads and actions alike with a generated access token, printed on the
console at start. Without it a visitor reaches only the health/version document and the sign-in screen: no
inventory, logs, settings, jobs, history, exports, metrics or event stream. Type the token into the sign-in
screen; it is posted in a request body, kept in an `HttpOnly; SameSite=Strict` session cookie and never
placed in a URL. API clients send `Authorization: Bearer <portal-token>` instead. Cookie requests retain
Origin and CSRF checks; explicit bearer credentials do not need browser CSRF headers. Rotating the token
(`hloom keys rotate portal-access`) signs every session out at once, without a restart.

The token is reused across starts from `~/.config/hydraweave/secrets/portal-access` (0600 on POSIX), or
`%APPDATA%\hydraweave\secrets\portal-access` (user-scoped DPAPI and private ACL on Windows).
Loopback binds keep their existing behavior. To allow unauthenticated LAN control explicitly, use
`hloom ui --listen 0.0.0.0 --no-auth` or `HYDRAWEAVE_NO_AUTH=1`. The console and portal display a warning.

For workers, copy the bootstrap command and package SHA-256 from the portal. It uses
`curl -fsSL http://HOST:PORT/node/bootstrap.sh | HYDRAWEAVE_PKG_SHA256=<hex> sh`; on a token-protected
portal first `export HYDRAWEAVE_PORTAL_TOKEN=<portal-token>` on the worker and add
`-H "Authorization: Bearer $HYDRAWEAVE_PORTAL_TOKEN"` to that `curl`.
The script downloads the package to a temporary file and verifies it before installing with pip; a mismatch
aborts installation. If the environment variable is missing, the script prints a note and uses its embedded
hash. Bootstrap/package downloads need the portal token, while worker pairing and agent calls keep their own
pairing-code/bearer authentication. Use a trusted LAN or secure tunnel because HTTP carries scripts and
tokens without encryption. See [Node discovery — security](node-discovery.md#portal-access-security).

## 8. Upgrade, roll back, uninstall

Windows, release installs:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1            # from the newer release: installs and activates it
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -List      # installed releases; * marks the active one
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -Activate hydraweave-<version>-g<commit>   # roll back
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -Remove hydraweave-<version>-g<commit>     # not the active one
```

Stop every profile (`hloom down`) before switching releases. Your configuration, keys and host keys in
`%APPDATA%\hydraweave` are shared by all releases and are never removed by the installer.

Stop running profiles with `hydraweave down <profile>` before uninstalling. Preview the removal first:

```sh
hydraweave uninstall --dry-run
hydraweave uninstall --nodes --dry-run                 # include configured SSH nodes
hydraweave uninstall --nodes --yes                     # keep models and configuration
hydraweave uninstall --nodes --remove-models --purge-config --yes
hydraweave-node remove-service --dry-run               # only the discovery agent's user unit
hydraweave-node remove-service
```

Without `--yes`, uninstall prints the plan and asks for confirmation. Dry-run never
stops services or deletes files. Uninstall stops and disables the project's portal/UI,
agent and generated engine user units (`hl-*.service` and `hydraweave-*.service`).
Vendor unit files stay package-owned. A failed service stop/disable prevents directory
removal on that host. SSH nodes use pinned host keys and require no bootstrap or root.

Only directories with a regular `.hydraweave-root` or `.hydraweave-assets` marker
can be deleted. Symlink/junction roots, unmarked directories (including older boot
and release directories), and unrelated units are retained. Models are kept by
default (`--keep-models`); only `--remove-models` opts out. A marked install root
containing retained models or config is kept as a whole. The output lists retained
paths, reasons and logical sizes in bytes. `--purge-config` also requires a marker.
Unmarked paths, Python packages and Windows shortcuts can be removed manually
after reviewing the plan; this command does not use administrative privileges.

New user paths follow XDG on Linux: `~/.config/hydraweave` for config,
`~/.local/state/hydraweave/run` for runtime state, and `~/.local/share/hydraweave`
for data. `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_DATA_HOME` override those
bases (relative XDG values are ignored). Windows uses `%APPDATA%\hydraweave`
for config and `%LOCALAPPDATA%\hydraweave` for data, with runtime state under
`state\run`. `HYDRAWEAVE_HOME` instead contains `config`, `state` and `data`.
Existing config/secrets and runtime directories remain readable in place; an
existing run directory is reused. Explicit workspace paths remain local to that
workspace. No automatic migration deletes data, and existing node `install_root`
and model directories stay unchanged. Uninstall never adds ownership markers to
existing unmarked directories.

### Debian packaging (Ubuntu 22.04 and 24.04)

`python3 packaging/build.py --dry-run` stages a `.deb` and prints its fpm command
without fpm, networking or root. Add `--format rpm` for the RPM recipe; omit
`--dry-run` only on a packaging host with fpm installed. CI checks the Debian dry-run
with each Ubuntu release's distribution Python. Runtime dependencies include
Python 3.10+, venv, curl, git and `python3-tomli`. CMake and Ninja are recommendations
for optional source builds, with no CMake 3.24 requirement for package installation.
On Python 3.10 without tomli, JSON recipes and JSON `--set` values work; TOML
reports how to install tomli. A normal Python package install supplies tomli on 3.10.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `hosts scan` fails with `OpenSSH command failed (exit 1)` | The `ssh-keyscan` bundled with Windows 11 (OpenSSH_for_Windows 9.5p2) aborts with `choose_kex: unsupported KEX method sntrup761x25519-sha512@openssh.com` against current servers. Hydraweave then retries with Git for Windows' `ssh-keyscan` (`Git\usr\bin`); install Git for Windows if it is missing. |
| `doctor` reports `node_tool` `stale`, or `up` says `node tool differs from this checkout` | The node tool on the node is from another release. Stop the profile, then `hloom nodes update <profile>` (add `--machine <machine>` for one node). |
| `unknown host key; confirm a fingerprint before connecting` | Section 5 was skipped for this machine, or `http_address`/`ssh_port` changed since pinning. |
| `Permission denied (publickey)` | Your public key is not in the node user's `~/.ssh/authorized_keys`, or `ssh_target` names the wrong user (it must be `<user>@...`, see section 4). |
| `install root is or contains HOME`, `overlaps a git repository` | Choose another `install_root` (section 4). |
| `Python 3.11 or newer was not found` | Install Python 3.11+ or the `py` launcher, then rerun the installer. |
| The installer stops with `sha256 mismatch` or `File not in RELEASE.json` | The unpacked release was changed or mixed with other files; unpack the archive again into an empty directory. |
| `hydraweave` runs a different version than installed | Use the launchers: they run Python with `-P`, so a Hydraweave checkout in the current directory cannot shadow the install. |

## How this guide was checked

On 2026-10-23, with release `hydraweave-0.1.0-ga937c4c7c82b`:

- **Windows 11**, fresh `LOCALAPPDATA`/`APPDATA` in a path with spaces: archive verified and installed, launcher and
  `--version`, a second release installed, `-List`, rollback with `-Activate`, `-Remove` refused for the active release,
  a tampered file refused, the shortcut target checked.
- **Linux**, a clean `python:3.11-slim` container as an unprivileged user: `sha256sum -c`, unpack, venv install,
  `--version`, `config validate`.
- **Node first contact** against a fresh Debian 12 container with only `openssh-server`, `git` and Python 3.11:
  `hosts scan` (this found the Windows `ssh-keyscan` failure handled under Troubleshooting), fingerprint compared
  with `ssh-keygen -lf` on the machine, `hosts pin`, `nodes update` (the node tool reaches a node only that way), `doctor`;
  doctor then reported the missing node toolchain as `unavailable` with reasons.
- Section 7 needs the real nodes and GPU turns; it follows the Phase 1a acceptance runs in `docs/acceptance-1a.md`.
