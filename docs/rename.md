# Renamed from hyperloom

Hydraweave is the new name for the control layer that places, schedules and serves LLM inference across nodes and GPUs. New installations use the `hydraweave` package and command. The short `hloom` command remains available.

- `import hyperloom` and its submodule imports forward to the same `hydraweave` modules and emit a `DeprecationWarning`. `python -m hyperloom` and the `hyperloom` console script remain usable.
- Set `HYDRAWEAVE_*` variables for new deployments. Each `HYPERLOOM_*` variable is read only when its corresponding new variable is **unset**; an explicitly empty new value takes precedence. A fallback prints one deprecation line to stderr without printing its value.
- New configuration defaults are `%APPDATA%/hydraweave/cluster.json` on Windows and `~/.config/hydraweave/cluster.json` elsewhere. The corresponding `hyperloom` file is read when the new file is absent. Explicit `--config` paths still work.
- Configuration, saved secrets, trusted host keys and runtime state under the former `hyperloom` directories remain readable. Windows runtime state uses `%LOCALAPPDATA%`; other systems use `~/.config`. Current files win when both versions exist. Existing launch records and their locks continue to use their original runtime location so they can be stopped safely. No migration deletes user data.
- Old `hyperloom/*` configuration and node-response schema names remain accepted. Existing `.hyperloom-root` and `.hyperloom-assets` markers and partial downloads remain usable. Node lookup falls back to `.hyperloom-boot/hyperloom-node`; update nodes with `hydraweave nodes update` before starting new deployments so peer operations use the current tooling.
- Existing `/hyperloom/` portal URLs and browser preference keys remain readable. Old API URLs keep their old schema envelope. New URLs, exports, logs and services use the Hydraweave name.

To migrate, install the new package, update environment names and automation to `hydraweave`, and update the node tools. You may copy configuration and saved data to the new directories after stopping the old control processes. Keep the originals until the new deployment has been checked. Do not run two control processes against one deployment during the transition.
