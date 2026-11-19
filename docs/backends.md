# Backends

Backends describe how Hydraweave starts a serving engine and how it recognizes readiness. The controller registry is
in `hydraweave/backends`; add a module with a `Backend` record and include its module name in `MODULES`. Put serving
defaults in a JSON file under `hydraweave/presets`. Data readiness rules (`stream` plus `regex`, `json_event`, or
`http`) are sent with leases, so adding an adapter does not require a node redeploy.

## Integration status

The managed profiles use the project's own inference engines. The optional
llama.cpp registry entry is an adapter declaration, not the engine used by those
profiles and not a requirement for completing their orchestration.

Qwen, DeepSeek and MiniMax families are connected to the managed profile renderer and lifecycle. The `llamacpp`
record currently provides metadata and a validated argument builder for a prebuilt `llama-server`; it is **not a
launchable managed profile**. It is deliberately absent from `engines.FAMILIES`, and the JSON example in
`backends/profiles` is not a deployable preset. Completing that integration still requires an install/asset
contract, rendering, resource ownership, readiness, stop/recovery and a qualified end-to-end fixture. Declaring
CUDA/HIP/Vulkan/CPU requirements in registry metadata does not establish acceptance on those accelerators.

The argument builder accepts a model id and GGUF path:

```json
{"model":"llamacpp-generic","model_path":"/models/example.gguf","binary":"/opt/llama/llama-server",
 "context":8192,"n_gpu_layers":99,"host":"0.0.0.0","port":8080}
```

The backend renders `-m`, host, port, context and GPU layer arguments, followed by optional `extra_args` (a list of
individual strings). Context must be positive, ports are in 1..65535 and GPU layers are at least -1; booleans,
fractional values and malformed arguments are rejected before constructing a process command.
