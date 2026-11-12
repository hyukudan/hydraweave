# Operations

## Logging

Start the portal with `hydraweave ui --log-level INFO --log-json` to select the
log level and JSON output. The node agent's `serve` command accepts the same
flags. Logs are written to `logs/hydraweave.log` in the portal state directory (`state/hydraweave.log` in a node install root), with
rotated copies retained. Credential-like values are redacted before output.

## Metrics

The portal serves Prometheus exposition text at `/metrics`. Loopback access
works directly; LAN access uses the portal's access authentication. The
standalone `hydraweave metrics serve` command remains available for exporter
deployments.

## Effective configuration

Run `hydraweave config show --effective` to print the parsed configuration with
defaults applied. Credential fields are redacted. Omit `--effective` to view
the saved configuration as written.
