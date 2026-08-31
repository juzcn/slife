# local-embed

**Standalone local embedding server** — loads one GGUF (llama-cpp) or HF
transformer embedding model **once** and exposes it as an
[OpenAI-compatible](https://platform.openai.com/docs/api-reference/embeddings)
HTTP service on a single port: `POST /v1/embeddings`, the Models API
(`GET /v1/models`, `GET /v1/models/{id}`), plus FastMCP tools.

Two backends: **`gguf`** loads a local `.gguf` file (llama-cpp-python);
**`transformer`** loads an HF repo id (sentence-transformers).  The server
never downloads weights — you pre-download them ([Model
weights](#model-weights) below).

Built for **slife** (its `memdb` and `memfiles` plugins call it over HTTP, so
a model is loaded once per process tree); it is fully standalone — any
OpenAI-compatible client works.

```
                POST http://127.0.0.1:17347/v1/embeddings
   slife memdb ─────────────────────────┐
   slife memfiles ──────────────────────┤
   any OpenAI client ───────────────────┤
                                        ▼
                            ┌────────────────────────────┐
                            │  local-embed               │
                            │  /v1/embeddings  (HTTP)    │
                            │  /v1/models      (HTTP)    │
                            │  /v1/models/{id} (HTTP)    │
                            │  /health         (HTTP)    │
                            │  /mcp  (FastMCP tools)     │
                            │  ONE loaded model          │
                            └────────────────────────────┘
```

## Install

Requires [uv](https://docs.astral.sh/uv).  All installs below pin
**Python 3.13** (`requires-python` is `>=3.13`, but 3.13 is what CI tests,
and uv would otherwise pick the newest 3.14 it finds).  Re-running any of
them over an **existing** install requires `--reinstall` (`uv tool install`
is a no-op on an already-installed tool otherwise), e.g. to upgrade to a new
release or pick up the `json5` dependency fix:

```bash
uv tool install --python 3.13 --reinstall 'local-embed[gguf,transformer]'
```

The `transformer` backend installs the same everywhere:

```bash
uv tool install --python 3.13 'local-embed[transformer]'   # sentence-transformers
```

`gguf` (llama-cpp-python) is the platform-sensitive one: PyPI ships **only
the sdist** (no prebuilt wheels), so on Linux / WSL / macOS a plain install
**compiles from source** — the standard build (needs a C compiler + CMake
≥ 3.21).  **Windows has no default C toolchain** (no MSVC), so it uses the
upstream prebuilt CPU wheel — the one workaround.  GPU variants pass
`CMAKE_ARGS`:

| Platform | Command |
|---|---|
| Linux / WSL / macOS, CPU | `uv tool install --python 3.13 'local-embed[gguf]'` (compiles from source) |
| macOS arm64 (Metal) | `CMAKE_ARGS="-DGGML_METAL=on" uv tool install --python 3.13 'local-embed[gguf]'` |
| NVIDIA CUDA (Linux) | `CMAKE_ARGS="-DGGML_CUDA=on" uv tool install --python 3.13 'local-embed[gguf]'` (needs the CUDA toolkit) |
| **Windows, CPU** | `uv tool install --python 3.13 'local-embed[gguf]' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu` (prebuilt wheel — no MSVC by default) |

The Windows CPU row is the only workaround — everywhere else uses the
standard source build from PyPI.  If the `gguf` backend is missing, the
server errors at startup with the exact command for your platform (the
Windows CPU one on Windows).

**Want both backends in one environment?** Install the two extras together
in a single command — running `uv tool install` twice replaces the first
environment with the second, so they would not coexist:

```bash
# both backends, Windows CPU:
uv tool install --python 3.13 'local-embed[gguf,transformer]' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
# both backends, Linux / macOS CPU:
uv tool install --python 3.13 'local-embed[gguf,transformer]'
```

Core deps are `fastmcp` + `starlette`; the model backends are optional
extras.  Installing does **not** fetch a model — get the weights first.

## Model weights

The server never downloads models; both backends fail at load time when
their weights are missing (`gguf` needs the `gguf_path` file, `transformer`
needs the repo in the HF cache).

### GGUF

`.gguf` files are, in general, **community conversions** — upstream releases
are PyTorch weights, not GGUF — so there is **no single authoritative
source**: conversions are scattered across Hugging Face, ModelScope, Ollama,
llama.cpp community uploads and various project sites.  Pick any source you
trust; the transport doesn't matter, local-embed only needs the file on disk.

HF is the most common host and its CLI pulls a single file:

```bash
uv tool install "huggingface-hub[cli]"      # provides `hf`
hf download <owner>/<repo> <model>.gguf --local-dir D:\models\bge-m3
# one-off, no permanent tool install:
uvx --from huggingface-hub hf download <owner>/<repo> <model>.gguf --local-dir D:\models\bge-m3
```

Any source also works from a browser or `wget`/`curl` — on HF, every file is
fetchable from `https://huggingface.co/<owner>/<repo>/resolve/main/<model>.gguf`.

Prefer a high-fidelity quant such as **Q8_0** (~99 % of the original
accuracy at roughly a third of the size).  Point `gguf_path` at the file:

```json5
models: {
  "bge-m3": { backend: "gguf", gguf_path: "D:\\models\\bge-m3\\bge-m3-q8_0.gguf" }
}
```

### Transformer

`sentence-transformers` resolves an HF repo id against the local hub cache:

```bash
hf download BAAI/bge-m3     # -> ~/.cache/huggingface/hub/models--BAAI--bge-m3
```

```json5
models: {
  "bge-m3": { backend: "transformer", model: "BAAI/bge-m3" }
}
```

## Config: `local_embed.json5`

Everything — host, port, models, active model, backend — comes from
`local_embed.json5`.  Path resolution: `$LOCAL_EMBED_FILE` > slife project
root (dev) > `~/.local-embed/local_embed.json5`.

```json5
{
  active_model: "bge-m3",          // model served by default
  models: {
    "bge-m3": { backend: "gguf", gguf_path: "…", device: "" },
    "bge-m3-transformer": { backend: "transformer", model: "BAAI/bge-m3", device: "" }
  },
  env: {                            // injected into this process before any model loads
    HF_HUB_CACHE: "C:\\…\\HuggingFace\\hub",   // where transformer repos resolve
    HF_HUB_OFFLINE: "1"                        // force offline
  },
  host: "127.0.0.1",                // standalone only
  port: 17347                        // standalone only (slife plugin spawn binds a free port)
}
```

- `models` — map of name → `{backend, gguf_path | model, device, max_tokens}`.
- `active_model` — key into `models`.
- `env:` — injected before any backend loads; an existing shell env var
  wins.  Without `HF_HUB_CACHE`, transformer repos resolve against the
  default cache and a model downloaded elsewhere is silently re-fetched.
- Single-model convenience — top-level `backend`/`model`/`gguf_path`/`device`
  — still works.

## Run

```bash
local-embed                 # binds 127.0.0.1:17347 by default
```

The server CLI takes no model/endpoint flags — the config is the only
source of truth.

## CLI — configure models via subcommands

`local-embed set` (transformer) and `local-embed set-gguf` (gguf) upsert a
model in the config, make it **active**, and pin port (and, for `set`, the
HF cache + offline flag).  Both are **idempotent** — re-running yields the
same config — and leave other models untouched.

| | `set` (transformer) | `set-gguf` |
|---|---|---|
| `<model_name>` | required — HF repo id (key + repo loaded) | required — config key |
| weight ref | `--HF_HUB_CACHE <dir>` — cache must contain the repo | `--path <PATH>` required — existing `.gguf` file |
| cache fallback | env `HF_HUB_CACHE`, else error | — |
| env written | `HF_HUB_CACHE` + `HF_HUB_OFFLINE: "1"` (offline server) | — |
| `--port <n>` | default `17347` | default `17347` |

```bash
local-embed set BAAI/bge-m3 [--HF_HUB_CACHE <dir>] [--port 17347]
local-embed set-gguf bge-m3 --path D:\models\bge-m3\bge-m3-q8_0.gguf [--port 17347]
```

Any error — cache unset, repo not in cache, file missing — exits non-zero
and writes nothing.  Changes apply on the next server start.

### Model download — offline by default

`HF_HUB_OFFLINE=1` by default, so the server **never downloads** a model
(it's too slow).  Make the model available yourself — see [Model
weights](#model-weights): `hf download BAAI/bge-m3` for the transformer route
(into the HF cache), or drop a GGUF at the default `~/.local-embed/models/…` for the
GGUF route.

## Use

```bash
curl http://127.0.0.1:17347/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model": "bge-m3", "input": ["hello world", "another text"]}'
```

Returns the standard OpenAI shape:

```json
{
  "object": "list",
  "data": [
    {"object": "embedding", "index": 0, "embedding": [0.012, ...]},
    {"object": "embedding", "index": 1, "embedding": [...]}
  ],
  "model": "bge-m3",
  "usage": {"prompt_tokens": 3, "total_tokens": 3}
}
```

Any OpenAI-compatible client works — point `base_url` at
`http://127.0.0.1:17347/v1` (e.g. a slife `embeddings` provider, or the
`openai` Python package):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:17347/v1", api_key="local")
vecs = client.embeddings.create(model="bge-m3", input=["hello"])
```

Other endpoints (OpenAI Models API):

- `GET /v1/models` — every configured model, each with its real embedding
  dimension (`dimension` / `dimension_known`), backend, load state, and the
  `active` flag the host uses to pick the active model.
- `GET /v1/models/{id}` — one model's detail (the OpenAI `retrieve`
  endpoint); 404 with a standard error envelope when the id is unknown.
- `GET /health` — `{status, backend, model, dimension, loaded}`.
- `POST /mcp` — FastMCP streamable-HTTP endpoint (internal tool
  `__check`, probed by the host's `system_health`).  Like the
  sharefile plugin, local-embed is a *service provider* — the host consumes
  the model over the OpenAI-compatible HTTP routes, never through MCP tools.

## Dimension

The real output width is only known once the model is loaded (`n_embd` /
`get_sentence_embedding_dimension`).  `GET /v1/models` (or `/v1/models/{id}`)
reports the real dimension, so a client can size its vector table correctly
— a wrong width silently drops every mis-sized embedding.

## As a slife external plugin

slife treats every embedding model as a **remote OpenAI-compatible
endpoint** — local-embed is just one such endpoint.  The model is
**determined by the plugin's active model**: slife discovers it from
`GET /v1/models` (the entry flagged `active: true`) on load.

Register local-embed as an external plugin (like `mcp-plugin`) so slife
manages the process, and point slife's embedding config at it with the
unified OpenAI format:

```json5
plugins: {
  external: [
    { name: "local-embed", module: "local_embed.server" }
  ]
},
embeddings: {
  providers: {
    "local-embed": {
      base_url: "http://127.0.0.1:17347/v1",  // stable port from local_embed.json5
      api_key: "local",
    }
  },
  active_model: "local-embed",   // provider-id only, or "local-embed/<model>"
  enabled: true
}
```

The plugin binds the **stable port** from `local_embed.json5` (default
`17347`), so slife's `base_url` is fixed whether local-embed runs as the
plugin or standalone.  When the service is unreachable, slife degrades
gracefully to keyword search.

## License

MIT — see the repository root `LICENSE`.
