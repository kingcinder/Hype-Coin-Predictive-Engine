# Local LLM providers

The optional LLM ensemble accepts either Ollama or an OpenAI-compatible
chat-completions server. Set `LLM_ENABLED=false` to disable it. Invalid or
unstructured model output produces no narrative and zero score deltas.

| Setting | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `ollama` | `ollama` or `openai_compat` |
| `LLM_BASE_URL` | provider-specific | Server origin, without an API path |
| `LLM_API_KEY` | empty | Optional bearer token |
| `LLM_MODEL` | `qwen2.5:7b` | Ollama model name or OpenAI-compatible model alias |
| `LLM_TIMEOUT_SECONDS` | `180` | Per-request HTTP timeout and maximum time for the optional scoring batch |
| `LLM_MAX_RETRIES` | `2` | Additional retries for timeout, transport, and HTTP 5xx failures |

`LLM_OLLAMA_URL` remains supported as a deprecated Ollama URL when
`LLM_BASE_URL` is empty. The default circuit breaker opens after three
consecutive failed provider calls and allows a new attempt after 60 seconds.
The batch watchdog lets the scan continue without LLM results when inference
exceeds its deadline.

## llama.cpp `llama-server`

Build llama.cpp with its Vulkan backend enabled (`-DGGML_VULKAN=ON`) and start
the OpenAI-compatible server with an explicit model alias. For example:

```bash
llama-server -m /models/model.gguf --host 127.0.0.1 --port 8080 \
  --alias serpent-local --n-gpu-layers 99
```

Use a model quantization and GPU layer count that fit the available VRAM; reduce
`--n-gpu-layers` or choose a smaller quantization if model loading fails. On an
RX 5700 XT, compile/run with Vulkan/RADV and confirm the selected Vulkan device
in llama.cpp logs. The exact model and layer count depend on its quantization,
context size, and other GPU workloads.

Configure the engine with:

```dotenv
LLM_PROVIDER=openai_compat
LLM_BASE_URL=http://127.0.0.1:8080
LLM_MODEL=serpent-local
LLM_TIMEOUT_SECONDS=180
# Optional when llama-server is started with --api-key:
LLM_API_KEY=replace-with-server-key
```

If the engine runs in a container, `127.0.0.1` points to that container; use
the server address routable from the container instead. The provider probes
`GET /v1/models` and falls back to `GET /health`, then posts to
`POST /v1/chat/completions`.

The llama.cpp server documents the OpenAI-compatible endpoints, `--alias`,
`--api-key`, and health route in its [server guide](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
Its [Vulkan build guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)
covers the `GGML_VULKAN` build option.
