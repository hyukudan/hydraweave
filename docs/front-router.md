# One OpenAI endpoint for several models

Each running profile has its own OpenAI-compatible router on its own port. `hydraweave front` puts one
endpoint in front of them and routes every request by its `model` field, so clients only need one base URL.

```sh
export HYDRAWEAVE_FRONT_TOKEN=change-me            # optional on 127.0.0.1, required on any other address
hydraweave front serve --port 18710 \
  --upstream qwen=http://127.0.0.1:18600 \
  --upstream deepseek=http://192.168.1.20:18600
```

Clients then use `http://<host>:18710/v1` with `Authorization: Bearer $HYDRAWEAVE_FRONT_TOKEN`.

| Route | Behaviour |
|---|---|
| `GET /v1/models` | Union of every upstream's `/v1/models`; `owned_by` names the upstream that serves the id first |
| `GET /v1/models/<id>` | One entry, or an OpenAI `model_not_found` 404 |
| `POST /v1/*` (chat, completions, embeddings, ...) | Forwarded unchanged to an upstream that lists the `model`; streaming (`"stream": true`) passes through chunk by chunk |
| `GET /health` | `ok` plus the upstreams that did not answer at the last refresh |

- The model list is refreshed every 10 seconds. Up to eight independent catalog probes run concurrently;
  a single refresh publishes one atomic snapshot. Other requests can use an existing snapshot while a refresh
  is in flight. A request for an id nobody listed re-asks the upstreams
  (at most once a second), so a profile that just started is found without restarting the front.
- When several upstreams list the same id (replicas), requests take turns between them. Refreshes preserve the
  next replica when membership is unchanged; duplicate rows from an engine do not give it extra turns.
- An upstream that is down is skipped in the listing; a request routed to it returns a 502
  `upstream_unavailable` error.
- Each upstream may have its own bearer key, independent of the client-facing token. Bind it by name using
  `--upstream-key-env qwen=QWEN_ROUTER_KEY` or `--upstream-key-file deepseek=/path/to/router-key` (repeatable on
  both `serve` and `models`). Keys are read by the front; the client's Authorization header is never relayed.
  A missing, malformed or duplicate key binding is rejected. Do not embed credentials in upstream URLs.
- `hydraweave front models --upstream ...` prints the aggregated list once, with any upstream errors.

The front adds no model logic: request and response bodies are the engines' own. The shared OpenAI contract
test (`tests/openai_contract.py`) is run against it in `tests/test_frontdoor.py`.

POST bodies need one unambiguous Content-Length and are limited to 32 MiB. Chunked request bodies are not
accepted. Request query strings and end-to-end response headers are preserved; connection-specific fields are
removed on each hop. If an upstream closes before its declared response length, the front closes its response
without a successful chunk terminator, so clients can detect the truncation. It never retries a partially
forwarded generation. IPv4 and IPv6 loopback listeners are supported.
