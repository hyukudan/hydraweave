# Components and updates

The Updates page's **Update** action installs the verified Hydraweave release
side by side, including its compatible package resources and lock file. It
activates the new release, checks its CLI and any active portal user service,
and automatically rolls back if health checks fail. Progress and the final
**updated** or **rolled back** result (with the failure reason) appear on the
same page, including after the portal reconnects. See
[Linux releases, updates and rollback](install.md#linux-releases-updates-and-rollback).

**Advanced: Check upstream HEAD** remains beside the update action and on each
component card. It compares upstream source revisions; it does not install a
Hydraweave release or change a profile's integration pins. In the control center,
discovered paired agents with a version/commit mismatch offer **Update node**;
live GPU leases or turns block that maintenance action with an explanation.

The component center separates four different facts: **the revision a profile
requires**, **the revision now available upstream**, **source downloaded on the
controller**, and **the revision recorded in an engine launch**. Downloading a
new upstream commit does not make it a compatible profile or a running release.

The catalog contains Strix Halo Engine, TensorMeld, Strix Trellis and the Strix
Halo USB4 driver. Engine/Trellis/TensorMeld pins come from `versions.lock` and the
selected preset's overrides. Different profiles can require different commits;
the panel retains all of them rather than collapsing them to one "version".

## Where work happens

The portal and this source cache run on the controller. The controller may be a
workstation or a Linux control host; it is a role, not necessarily inference
rank 0. Its component operations do not require a GPU. The existing SSH node
tool remains responsible for node builds and activation.

| Action | Location | Effect |
|---|---|---|
| Update | Controller release environments | Verifies, stages and activates a release; health failure restores the previous release |
| Advanced: Check upstream HEAD | Controller → each fixed Git remote | Records upstream HEAD and time, without changing checkouts or pins |
| Download source | Controller component cache | Stages the exact chosen Git commit in a new directory |
| Install profile | Selected SSH build node, then its peers | Downloads the profile's pins, builds, verifies and distributes a complete release |
| Activate the built profile | Inference nodes | Existing installer checks require the profile to be stopped |
| Install a USB4 kernel/driver | Separate node maintenance | Not performed by source-download operations |

Use the component center's **Install profile** action for a runnable pinned
deployment. Controller source downloads are useful for inspecting or retaining
a revision; the existing node installer fetches its own sources and does not
consume this cache implicitly.

## Upstream checks and downloads

`GET /hydraweave/api/components` is passive: it reads local metadata and saved
launch evidence. It does not start network requests, SSH calls or downloads.
An unavailable configuration still returns the catalog so a first installation
can start from an empty state.

The `components/check` background job accepts `{}` for all components or
`{"component":"engine"}` for one. Git's default-branch HEAD is resolved to a
full SHA. Each component retains its own success or error. A failed check is a
failed job and never appears as "up to date". This operation writes only check
metadata, not source checkouts.

The `components/download` background job accepts
`{"component":"engine","commit":"<40-character SHA>"}`. The commit must be a
configured pin or the HEAD from the most recent successful check, no more than
24 hours old. The server chooses the repository and destination; a browser
cannot supply a URL, command or path.

Downloads use a temporary directory, fetch the exact commit, verify the fetched
SHA, check out a clean detached tree and publish it with an atomic directory
rename. Cancelling or failing an operation removes its own partial stage and
leaves completed revisions intact. A repeated download verifies the existing
HEAD, origin and working tree before reuse. Local modifications are refused and
left untouched. Submodules are not recursively fetched; this is a source
checkout, not a finished build environment.

The default cache is `components/` beside the selected cluster configuration.
An operator can choose a dedicated absolute directory with
`HYDRAWEAVE_COMPONENT_CACHE`; its legacy environment spelling is accepted by the
existing compatibility layer. Browser requests cannot change it. A nonempty
directory without Hydraweave's cache marker is not adopted. Individual source
directories are never reset or overwritten.

## Installing a coherent profile

`GET /hydraweave/api/components/plan?profile=<id>` explains the existing install
steps, exact component pins, build node and receiving nodes without contacting
hardware. Its action refers to the existing `install` job. This deliberately
keeps engine, Trellis ABI and TensorMeld versions together.

The standard workflow is:

1. Add and verify the node's SSH host key.
2. Update the node tools for the profile.
3. Install the profile: build once, verify binaries, copy and verify peers, then
   activate the release references while the profile is stopped.
4. Start the profile through the existing supervised launch flow.

An upstream commit that has not been qualified for a preset can be downloaded
and inspected. It is not written into that preset or activated. Presets with
unpublished source pins still require their matching local source through the
CLI installer; the plan reports this instead of silently substituting HEAD.

USB4 driver sources have no invented compatibility claim. This catalog does not
currently pin a Hydraweave-qualified driver/kernel combination. Downloading them
never invokes a kernel build, `sudo`, a module loader or a reboot. Node driver
maintenance must use the engine/driver's documented matching kernel workflow.

## Evidence and limitations

"Recorded launch" revisions come from saved launch manifests. They are marked
`live_verified:false`; they are not proof that an offline node still has the same
release running. Missing active/install evidence remains unknown.

Git operations have bounded output and deadlines and cancellation stops their
process tree. Cache writes use atomic metadata replacement and component
downloads use cross-process locks. Jobs use the portal's existing progress,
failure and cancellation display. No background auto-update is enabled.

Closing the portal cancels component checks and downloads and waits up to 15
seconds for feature cleanup. A normal close terminates their Git process trees
and removes unpublished stages. Detached profile installs and starts continue
under the existing CLI supervisor. An abrupt operating-system kill cannot run
portal cleanup; it never turns a partial download into a published revision.

The test suite uses local Git repositories to exercise immutable downloads,
wrong revisions, failed checks, dirty checkouts, cancellation and preservation
of existing data. These tests do not claim GPU, compiler, USB4 or physical-node
acceptance.
