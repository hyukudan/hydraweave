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

- The model list is refreshed every 10 seconds. A request for an id nobody listed re-asks the upstreams
  (at most once a second), so a profile that just started is found without restarting the front.
- When several upstreams list the same id (replicas), requests take turns between them.
- An upstream that is down is skipped in the listing; a request routed to it returns a 502
  `upstream_unavailable` error.
- Upstreams are called without a key: engines that require their own API key are not supported by the front
  yet. Keep the engines on a trusted network and let the front's token guard the shared endpoint.
- `hydraweave front models --upstream ...` prints the aggregated list once, with any upstream errors.

The front adds no model logic: request and response bodies are the engines' own. The shared OpenAI contract
test (`tests/openai_contract.py`) is run against it in `tests/test_frontdoor.py`.
