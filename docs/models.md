# Model registry

Model descriptions live in `hydraweave/models/*.json`; user descriptions may be placed in the configuration's
`models/` directory. Each file has an `id`, a `family` or `backend`, and `files` entries with a required `name` and
optional byte count and SHA-256. `source`, `context_max` and `chat_template` are optional metadata.

Presets describe engine flags and may reference a model with `"model": "<id>"`. Use `hydraweave models registry` to
list declarations, and `hydraweave models list` to view scanned model units on configured machines. Existing cluster
asset records remain supported.
