# Hosting derived model artifacts: user decision

This document proposes choices only. No repository, release, tag or hosted artifact is created by
this work. The public-only `qwen-starter` needs only its existing upstream model; the tuned profiles
also need the derived artifacts below.

| Artifact | Consumers | Size known from presets |
|---|---|---|
| Distilled MTP sidecar | Qwen tuned profiles | 2,786,568,256 bytes (about 2.60 GiB), pinned SHA-256 |
| Golden qualification file | Qwen tuned profiles | Actual size not pinned; `--mtp-golden-max-bytes` is 6,442,450,944 bytes (6 GiB), a cap rather than a file size |
| Vocabulary packs | Qwen draft vocab; DeepSeek `ds41_vocab` and tuned packs | Not pinned; directory contents need an inventory and reproducible packaging |
| Calibration token input | Qwen tuned receipt startup prompts | Not pinned; publish token IDs with their provenance and exact bytes/hash |
| Weight overlays | DeepSeek tuned profiles | Not pinned; include base-model identity, conversion provenance and exact bytes/hash |

Runtime memory budgets, sidecar header sizes and replay/window arenas are not hosted artifact sizes.
Other tuned DeepSeek assets (model, drafter and trellis packs) also remain unavailable in the catalog
until their immutable sources are supplied. A hosting choice alone does not make those assets reproducible.

## Choices

| Option | Fit and constraints | Pinning and integration |
|---|---|---|
| GitHub release assets | Each file must be **under 2 GiB**. The 2.60 GiB sidecar cannot be attached as one asset; it would need a separately designed split/reassembly format. | Record repository, immutable release identity and SHA-256 for every file. Tags alone can move. The current downloader does not accept GitHub release URLs. |
| Hugging Face model repository | Fits the existing model catalog and download path; accommodates versioned model artifacts. | Use a full 40-hex commit revision, repository ID, file path, byte count and SHA-256. No branch-name pins. |
| Self-hosted HTTPS/object storage | User controls retention, access, availability and transfer costs. | Use immutable object versions/paths plus exact bytes and SHA-256. Endpoint allowlisting and a source-schema extension would be needed before automatic downloads. |

The GitHub limit is documented in [GitHub's release documentation](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
Hugging Face documents [downloads at a full commit revision](https://huggingface.co/docs/huggingface_hub/en/guides/download).
These are options for the owner to assess; no hosting account or repository has been selected.

## Licensing and provenance

The Qwen source entry describes a derivative of the Qwen Community License 1.0 checkpoint and says
the license mapping is **pending in the engine ledger**. Preserve that pending status. Before
redistribution, resolve the derivative license, applicable notices and rights for each sidecar,
golden, token input, vocabulary pack and overlay. The project's code license does not establish
redistribution rights for derived model data. Record the base-model revision, conversion revision,
recipe and content hashes beside each artifact.

## Connecting the decision to sources.json

For a public Hugging Face model repository, replace an asset's `unavailable` row with the existing
`download` shape below. Placeholders must be replaced with independently measured pins:

```json
{
  "status": "download",
  "repo": "owner/derived-model-artifacts",
  "revision": "<full 40-hex commit>",
  "files": [{"file": "sidecar.gguf", "bytes": 2786568256, "sha256": "<64-hex SHA-256>"}],
  "ledger": "<reviewed provenance entry>",
  "license": "<resolved derivative license and notices>",
  "metadata": "<how the file size and digest were verified>"
}
```

Each `repo`/`revision` identifies immutable source contents; each `files[].sha256` verifies the
downloaded bytes. Ordered GGUF shards additionally declare `"merge": "gguf-split"` and may pin
`"merged": {"bytes": ..., "sha256": "..."}`. This merger handles GGUF tensor shards, not arbitrary
byte chunks or directory archives. Keep `after` for manual packaging/conversion until a corresponding
native installer exists. Directory assets still require a defined extraction/inventory workflow;
do not register an archive as though it were the unpacked directory.

The current node permits Hugging Face sources only. A GitHub or self-hosted decision needs an
explicit downloader/schema extension with equivalent path, HTTPS, immutable identity and hash checks;
changing `repo` into a URL would not work.

Decision needed: choose the hosting provider and repository owner, resolve derivative licensing,
and approve the artifact inventory, immutable revisions, sizes and SHA-256 pins before publication.
