# Asset integrity

`hydraweave assets verify PROFILE [--include-large] [--machine MACHINE] [--config PATH]`
hashes the preset's assets on its configured machines through scheduled node jobs.
Assets of 64 GiB or more require `--include-large`. Expected all-zero hashes mean
unknown: verification still computes and records a digest. Known config hashes
must match before an entry is saved. Existing lab hashing restrictions apply.

Hash jobs run at nice 19 and idle I/O priority (`ionice -c3`, required on nodes).
Linux uses `O_DIRECT` with an aligned anonymous mmap and 8 MiB requests. Only
unsupported direct I/O falls back to buffered reads with `POSIX_FADV_NOREUSE`.
Other I/O errors fail the job. Verification detects metadata changes during hashing.

A configured `bytes` of 0 means the size is not pinned (directory assets such as vocab and trellis packs);
any other value must match. A directory's bytes are the sum of its regular files. Its content digest is SHA-256
over the UTF-8 JSON sorted list `[relative_path, bytes, file_sha256]`, using POSIX
relative paths, ASCII escaping, and compact separators without a trailing newline.
Internal symlinks are followed under their relative names; escaping links, cycles,
and special files are refused. Empty directories do not affect the content digest.

The trusted cache is `%APPDATA%\hydraweave\assets-manifest.json` on Windows and
`~/.config/hydraweave/assets-manifest.json` on POSIX. Entries are keyed first by
machine, then by configured path, and contain bytes, mtime_ns, inode, dev, sha256,
and verified_at (Unix timestamp). Writes are atomic and restricted to the owner.
Directories also store a fingerprint of descendant metadata to detect changes
without reading contents. Verification does not move or modify assets.

`hydraweave assets status PROFILE [--config PATH]` compares the manifest to fresh
node stats and flags stale entries. Before any lease, `up` checks all asset sizes,
cached identities, known config digests, and known digests across pair ranks.
An unverified asset is allowed and labelled `size only` in asset status, the `up`
result, and running profile status.

This metadata cache is a convenience, not an integrity guarantee: content changed
while preserving all cached metadata needs a full `assets verify` to detect it.

## Model downloads in the portal

**Model downloads** uses the same immutable source catalog and node downloader as
`hydraweave assets plan/download`. Choose a profile, node, asset and final path
under a configured model root. Preparing a directory is a separate audited action:
it marks only an empty dedicated directory, rejects symlinks and installation/lab
overlap, and never adopts unmarked existing contents.

Review the source commit, file sizes, SHA-256 digests and space requirements before
confirming. The review code binds the node's plan to the current configuration,
including machine transports and paths. The job recomputes the passive plan before
starting; a changed target requires a new review. Browser requests cannot supply
custom URLs, hashes, shell commands or credentials.

The admitted job owns its machine, participates in the portal's cancellation and
page-renewal contract, and renews a separate 120-second owner on the node. A lost
controller stops renewal; the worker then retires and keeps reusable data. A
network failure triggers cancellation and bounded observation through the owner
expiry. An unobserved termination is reported as **unresolved**, never as success;
the node's model-I/O lock continues to exclude a new engine lease.

Existing final files and complete split parts are reused only after hashing. A
resident merged artifact is verified directly instead of downloading its input
shards again. Downloads resume retained curl partials and hash the whole result.
The final verified stat/digest receipt is rechecked before its path, bytes and hash
are registered in the profile and integrity cache. A changed configuration keeps
the verified file but refuses automatic registration.

Sources requiring an unpublished or derived artifact remain explicitly unavailable
for automatic registration. Downloading source parts cannot certify a derived
model, invent its pin, or change an engine's qualification.

## Library verification and direct copies

Library **Verify** runs the complete model manifest reader as an owned job. Remote
Linux nodes use their protected, cache-free reader under a model-I/O lock. The
local controller uses the same reader in process, a user-wide lock and the RTX
big-job ledger guard. Cancellation, changed files, manifest conflicts and partial
coverage remain visible in the result. Native Windows unbuffered reading still
requires platform acceptance; a host test on Linux is not that acceptance.

Library **Copy to…** requires a managed Linux source and an explicit `peer_ssh`
route to a Linux SSH destination whose host key is already pinned. The source
uses that public host key and the configured numeric address; it does not change
SSH trust or copy the controller's private credentials. Source-to-peer SSH
authentication must already work. Agent-only destinations without this SSH route
are shown as ineligible rather than falling back to a controller relay.

Copy plans enumerate a complete, unambiguous manifest and bind source/destination
configuration to their review code. Model payloads travel directly between the
nodes. The receiver hashes resident content and reusable complete staging files,
checks space for missing files, writes through protected directory descriptors,
hashes incoming data and publishes without overwriting an existing unit. Single
file and sidecar publication rolls back only this attempt's own link if the second
link fails. Directory publication uses Linux `renameat2(RENAME_NOREPLACE)`.

Both nodes hold model-I/O ownership, which excludes new leases, services and agent
activation. Status, renewal and cancellation remain available. A source disconnect
also interrupts receiver-side hashing; staging is retained. Copy resumption reuses
**complete verified files**, not an unverified prefix of an interrupted file.
The sender overlaps one next disk read with hashing and sending the current block,
with at most two 8 MiB payload blocks in memory. Completed destination writes are
flushed and their cache pages can be discarded before ownership is released.

Progress includes model payload bytes transferred, verified bytes reused and
receiver-side hash work. These are payload/work counters, not NIC, SSH framing or
USB4 hardware counters; they do not establish a hardware speedup. Inventory scans
fan out to at most eight independent machines and retain per-node failures.

The CLI uses the same admitted jobs and supports Ctrl+C cancellation:

```sh
hydraweave models verify MACHINE MODEL_ROOT NAME
hydraweave models copy-plan SOURCE SOURCE_ROOT NAME DESTINATION DESTINATION_ROOT
hydraweave models copy SOURCE SOURCE_ROOT NAME DESTINATION DESTINATION_ROOT --confirm REVIEW_CODE
```
