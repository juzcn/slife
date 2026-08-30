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
uv tool install --python 3.13 --reinstall 'local-embed[gguf,transformer]' \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

The `transformer` backend installs the same everywhere:

```bash
uv tool install --python 3.13 'local-embed[transformer]'   # sentence-transformers
```

`gguf` (llama-cpp-python) is the platform-sensitive one: PyPI ships
Linux/macOS CPU wheels but **no Windows wheels**, so on Windows a plain
install silently compiles from source (needs MSVC + CMake).  The upstream
project's prebuilt-wheel indexes (`https://abetlen.github.io/llama-cpp-python/whl/<cpu|cuXXX|metal>`) are the supported one-click path — pick the row
for your platform:

| Platform | Command |
|---|---|
| Linux / macOS, CPU | `uv tool install --python 3.13 'local-embed[gguf]'` (PyPI wheel) |
| **Windows, CPU** | `uv tool install --python 3.13 'local-embed[gguf]' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu` |
| macOS arm64 (Metal) | `uv tool install --python 3.13 'local-embed[gguf]' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/metal` |
| CUDA (Linux or Windows) | `uv tool install --python 3.13 'local-embed[gguf]' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu122` — pick `cu118` / `cu121` / `cu122` / `cu123` / `cu124` / `cu125` / `cu130` / `cu132` to match your CUDA version |

When the index is an *extra*, uv uses its wheel only when PyPI has no
compatible wheel for the platform (the Windows / Metal cases); on Linux
PyPI **does** ship a CPU wheel, so to get the CUDA build you must use the
CUDA index.  Note the CUDA Linux wheels need **glibc ≥ 2.35** (built for
`manylinux_2_35`) — on an older distro uv falls back to a source build.
If the `gguf` backend is missing, the server errors at startup with the
exact command for your platform (the Windows CPU one on Windows).

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

### `local-embed download <model>` — pre-download a transformer model

Downloads a transformer model's snapshot so the server loads without a
first-use download.  `<model>` is a configured model name (resolves its
repo id) or a bare HF repo id (e.g. `BAAI/bge-m3`).  It tries
huggingface.co first and **automatically falls back to the hf-mirror.com**
mirror (mainland China) — no `HF_ENDPOINT` needed:

```bash
local-embed download BAAI/bge-m3
HF_ENDPOINT=https://hf-mirror.com  local-embed download BAAI/bge-m3   # explicit mirror
```

The GGUF route needs no download command: drop a quantized BGE-M3 GGUF at
the default `~/.slife/models/bge-m3-q4_k_m.gguf` (or set `BGE_M3_GGUF_PATH`)
and the `bge-m3` model entry picks it up.

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
  `__embed_status`, probed by the host's `check_local_embed`).  Like the
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
