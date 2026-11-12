# Backends

Backends describe how Hydraweave starts a serving engine and how it recognizes readiness. The controller registry is
in `hydraweave/backends`; add a module with a `Backend` record and include its module name in `MODULES`. Put serving
defaults in a JSON file under `hydraweave/presets`. Data readiness rules (`stream` plus `regex`, `json_event`, or
`http`) are sent with leases, so adding an adapter does not require a node redeploy.

The `llamacpp` backend uses a prebuilt `llama-server` binary. Declare a model id and point its profile at a GGUF path:

```json
{"model":"llamacpp-generic","model_path":"/models/example.gguf","binary":"/opt/llama/llama-server",
 "context":8192,"n_gpu_layers":99,"host":"0.0.0.0","port":8080}
```

The backend renders `-m`, host, port, context and GPU layer arguments, followed by optional `extra_args`.
