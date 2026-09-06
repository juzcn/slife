# local-embed

**A standalone local embedding service.** It loads one local GGUF
(llama-cpp) or HF transformer embedding model and exposes it as an
[OpenAI-compatible](https://platform.openai.com/docs/api-reference/embeddings)
`/v1/embeddings` URL — any OpenAI-compatible client can embed text through it.

On one port it serves two surfaces: an OpenAI-compatible `/v1/*` API for
embedding, and a Streamable HTTP MCP endpoint at `/mcp`.

local-embed is three things at once: an independent app, a Streamable HTTP
**MCP server** (see [As an MCP server](#as-an-mcp-server)), and a conforming
**slife plugin** (see [Loading as a slife plugin](#loading-as-a-slife-plugin)).
The product is the embedding URL; the MCP and plugin surfaces are how hosts
manage the same standalone service.

Two backends: **`gguf`** loads a local `.gguf` file (llama-cpp-python);
**`transformer`** loads an HF repo id from the local hub cache
(sentence-transformers). The server never downloads weights — you pre-download
them ([Model weights](#model-weights)).

```
   any OpenAI-compatible client ──┐
   slife (memdb / memfiles) ──────┤── POST /v1/embeddings ──┐
   vector DB / RAG pipeline ──────┘                          ▼
                                              ┌───────────────────────┐
   slife (plugin host) ─── /mcp ────────────> │       local-embed     │
                                              │  ONE loaded model     │
                                              └───────────────────────┘
```

## Features

- **OpenAI-compatible** `/v1/embeddings` + Models API + `/health`.
- Two backends (GGUF / transformer), **many models configured, one active**.
- **Lazy load** — startup is fast; the model materialises on first use.
- **Real dimension** reported after load (a guessed width is never served).
- Runs standalone, and can also be **loaded as a slife plugin** (see
  [Loading as a slife plugin](#loading-as-a-slife-plugin)).

Requires Python ≥ 3.13.

## Install

The core package is `fastmcp` + `starlette` + `json5`; the model backends are
optional extras.

```bash
# the app + CLI (standalone tool)
uv tool install local-embed

# with a backend:
uv tool install "local-embed[transformer]"
uv tool install "local-embed[gguf]"
```

If you run local-embed inside an *existing* environment (alongside another
tool), install the backend into **that** environment's interpreter instead of
creating a separate one:

```bash
uv pip install --python <venv-python> llama-cpp-python==0.3.34
```

where `<venv-python>` is the interpreter that runs `local-embed`.

### `gguf` backend — platform matrix

`gguf` (llama-cpp-python) is platform-sensitive: PyPI ships **only the sdist**
(no prebuilt wheels), so on Linux / WSL / macOS a plain install **compiles from
source** (needs a C compiler + CMake ≥ 3.21). **Windows has no default C
toolchain** (no MSVC), so it uses the upstream prebuilt CPU wheel — the one
workaround. GPU variants pass `CMAKE_ARGS`:

| Platform | Command |
|---|---|
| Linux / WSL / macOS, CPU | `uv pip install --python <venv-python> llama-cpp-python==0.3.34` (compiles from source) |
| macOS arm64 (Metal) | `CMAKE_ARGS="-DGGML_METAL=on" uv pip install --python <venv-python> llama-cpp-python==0.3.34` |
| NVIDIA CUDA (Linux) | `CMAKE_ARGS="-DGGML_CUDA=on" uv pip install --python <venv-python> llama-cpp-python==0.3.34` (needs the CUDA toolkit) |
| **Windows, CPU** | `uv pip install --python <venv-python> --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python==0.3.34` (prebuilt wheel — no MSVC) |

The Windows CPU row is the only workaround — everywhere else uses the standard
source build from PyPI. If the `gguf` backend is missing, the server errors at
startup with the exact command for your platform.

**Both backends at once** — one `uv pip install`:

```bash
uv pip install --python <venv-python> sentence-transformers llama-cpp-python==0.3.34
# Windows CPU: add the upstream wheel index (see the matrix above)
```

Installing does **not** fetch a model — get the weights first.

## Quick start

1. **Install** the app and a backend (above).
2. **Download weights** — see [Model weights](#model-weights).
3. **Configure a model** — the CLI helper writes `local_embed.json5`:

   ```bash
   local-embed set BAAI/bge-m3 --HF_HUB_CACHE <dir>     # transformer
   local-embed set-gguf bge-m3 --path D:\models\bge.gguf # gguf
   ```

4. **Start the service**:

   ```bash
   local-embed          # serve on http://127.0.0.1:17347
   ```

5. **Embed text** — point any OpenAI-compatible client at
   `http://127.0.0.1:17347/v1` (see [HTTP API](#http-api-openai-compatible)).

## Configuration: `local_embed.json5`

Everything — host, port, models, active model, backend — comes from
`local_embed.json5`, written by the CLI helpers or by hand. Path resolution:
`$LOCAL_EMBED_FILE` > slife project root (dev) > `~/.local-embed/local_embed.json5`.

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
  port: 17347                        // standalone only
}
```

- `models` — map of name → `{backend, gguf_path | model, device, max_tokens}`.
- `active_model` — key into `models`.
- `env:` — injected before any backend loads; an existing shell env var wins.
  Without `HF_HUB_CACHE`, transformer repos resolve against the default cache
  and a model downloaded elsewhere is silently re-fetched.
- Single-model convenience — top-level `backend`/`model`/`gguf_path`/`device`
  — still works.

Reads are read-only at runtime — the server has no config-mutating tools.

## Model weights

The server never downloads models; both backends fail at load time when their
weights are missing (`gguf` needs the `gguf_path` file, `transformer` needs the
repo in the HF cache).

### GGUF

`.gguf` files are, in general, **community conversions** — upstream releases
are PyTorch weights, not GGUF — so there is **no single authoritative source**:
conversions are scattered across Hugging Face, ModelScope, Ollama, llama.cpp
community uploads and various project sites. Pick any source you trust; the
transport doesn't matter, local-embed only needs the file on disk.

HF is the most common host and its CLI pulls a single file:

```bash
uv tool install "huggingface-hub[cli]"      # provides `hf`
hf download <owner>/<repo> <model>.gguf --local-dir D:\models\bge-m3
# one-off, no permanent tool install:
uvx --from huggingface-hub hf download <owner>/<repo> <model>.gguf --local-dir D:\models\bge-m3
```

Any source also works from a browser or `wget`/`curl` — on HF, every file is
fetchable from `https://huggingface.co/<owner>/<repo>/resolve/main/<model>.gguf`.

Prefer a high-fidelity quant such as **Q8_0** (~99 % of the original accuracy at
roughly a third of the size). Point `gguf_path` at the file:

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

## HTTP API (OpenAI-compatible)

The endpoint surface is a superset of the OpenAI Embeddings + Models APIs, all
on one port.

### `POST /v1/embeddings`

```bash
curl http://127.0.0.1:17347/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model": "bge-m3", "input": ["hello world", "another text"]}'
```

`input` is a string or a list of strings; `model` names any configured model
(defaults to the active one). Returns the standard shape:

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

Empty/whitespace inputs get a zero vector of the model's dimension, keeping
row alignment.

### `GET /v1/models`

Every configured model, each with its real embedding dimension
(`dimension` / `dimension_known`), backend, load state, and an `active` flag.

### `GET /v1/models/{id}`

One model's detail (the OpenAI `retrieve` endpoint); 404 with the standard
error envelope when the id is unknown.

### `POST /v1/models/{id}/activate`

Switch the active model (loads it on demand). A local-embed extension, not an
OpenAI endpoint.

### `GET /health`

`{status, active_model, backend, model, dimension, dimension_known, loaded}`.

### Any OpenAI client

Point `base_url` at `http://127.0.0.1:17347/v1` — the `openai` package, or any
OpenAI-compatible SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:17347/v1", api_key="local")
vecs = client.embeddings.create(model="bge-m3", input=["hello"])
```

## CLI (auxiliary tools)

The `local-embed` CLI is a **helper**, not the product: it starts the service
and writes the config file. The service is configured by `local_embed.json5`
and consumed over the HTTP API — the CLI only makes both steps convenient.

### `local-embed` — run the service

```bash
local-embed                 # binds 127.0.0.1:17347 by default
```

The CLI takes no model/endpoint flags — the config is the only source of truth.
A port already in use is a hard error with one actionable line (no silent
fallback to a free port): stop the other instance or change `port` in the
config.

### `local-embed set` / `set-gguf` — write the config

`set` (transformer) and `set-gguf` (gguf) upsert a model in the config, make it
**active**, and pin port (and, for `set`, the HF cache + offline flag). Both
are **idempotent** — re-running yields the same config — and leave other models
untouched.

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

Any error — cache unset, repo not in cache, file missing — exits non-zero and
writes nothing. Changes apply on the next server start.

### Model download — offline by default

`HF_HUB_OFFLINE=1` by default, so the server **never downloads** a model (it's
too slow). Make the model available yourself — see
[Model weights](#model-weights): `hf download BAAI/bge-m3` for the transformer
route (into the HF cache), or drop a GGUF file and point `gguf_path` at it.

## Dimension

The real output width is only known once the model is loaded (`n_embd` /
`get_sentence_embedding_dimension`). `GET /v1/models` (or `/v1/models/{id}`)
reports the real dimension, so a client can size its vector table correctly —
a wrong width silently drops every mis-sized embedding. A guessed dimension is
never served as authoritative.

## As an MCP server

`/mcp` is a standard **MCP Streamable HTTP** endpoint, so any MCP client that
supports Streamable HTTP can register local-embed as an MCP server. Start it
first:

```bash
local-embed          # serve on http://127.0.0.1:17347
```

then register the URL. The MCP client config shape is `{"type": "http",
"url": …}` — for example, Claude Code's `.mcp.json` (or `~/.claude.json`):

```json
{
  "mcpServers": {
    "local-embed": {
      "type": "http",
      "url": "http://127.0.0.1:17347/mcp"
    }
  }
}
```

Or with the Claude Code CLI:

```bash
claude mcp add --transport http local-embed http://127.0.0.1:17347/mcp
```

> **Claude Desktop** registers remote HTTP MCP servers through its **connectors
> UI** (Settings → Connectors → Add connector → Remote), not
> `claude_desktop_config.json` — and a remote connector expects a hosted HTTPS
> URL, so a `127.0.0.1` server needs a tunnel or a stdio→HTTP bridge to be
> reachable from the desktop app.

**Over MCP you get status, not embeddings.** The only MCP tool is the internal
`__check` status probe (active model, dimensions, load state) — local-embed is
a *service-provider* MCP server, not a tool provider. To embed text, point
your consumer at the OpenAI-compatible `/v1/*` API instead. A client that only
speaks stdio can reach it through a stdio→HTTP bridge (e.g. `mcp-remote`).

## Loading as a slife plugin

local-embed is a standalone app, but it also conforms to the **slife plugin
contract** (defined in slife's `slife/server_utils.py`), so slife can load and
manage it as an external plugin — spawn the process, discover the port, and
monitor status — without any slife code changes. Conformance is additive:
none of it constrains standalone use, and local-embed never imports slife. The
few pieces of the contract it needs are carried as minimal, self-contained
copies (`local_embed.server_utils`, `local_embed.threads`,
`local_embed.logging`).

The MCP endpoint's single tool is the internal **`__check`**, which returns
engine status (active model, model list, dimensions, load state) as JSON. It
is a **service-provider** surface — slife probes `__check` programmatically and
consumes embeddings over the HTTP API, never through MCP tools.

How it conforms:

- **Spawn target** — `local_embed/server.py` exposes `main()`, spawnable via
  `python -m local_embed.server` (the contract's `sys.executable -m <module>`
  shape).
- **Port signal** — emits `{"port": N}` on stdout (then closes stdout) only
  after the FastMCP app is ready to serve (its lifespan completed), so the
  host's first `initialize` always lands on a ready server.
- **Readiness = MCP `initialize` handshake** — startup is handshake-fast:
  backend imports are resolved lazily and the model warms up *after* the first
  `tools/list` (`warm_after_handshake`), never in the lifespan.
- **Internal `__check` tool** — `__`-prefixed, so the host's tool registration
  filters it out of the LLM registry and `system_health` probes it
  programmatically.
- **Non-MCP routes on the same port** — the OpenAI endpoints are
  `@mcp.custom_route` handlers on the same uvicorn app (the "one port, two
  protocols" pattern).
- **Logging adoption** — when spawned by slife it adopts `SLIFE_LOG_DIR` /
  `SLIFE_AGENT_NAME` / `SLIFE_SESSION_ID` / `SLIFE_PLUGIN_NAME`, so the session
  log lands next to the main log; an uncaught startup exception prints one
  clean stderr line (the host relays it) with the full traceback in the file.
  Standalone falls back to `~/.local-embed/logs`.
- **Config path** — honors `$LOCAL_EMBED_FILE` (slife exports it as
  `<dir of slife.json5>/local_embed.json5`), else the standalone default.
- **Stable port** — binds the configured `17347` rather than an OS-assigned
  port, so slife's `base_url` is fixed; a taken port is a hard error, not a
  silent fallback.

### Registering with slife

Register local-embed as an external plugin and point slife's embedding config
at it (the unified OpenAI-compatible format slife uses for every embedding
model):

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

slife treats every embedding model as a remote OpenAI-compatible endpoint —
local-embed is one such endpoint. The model is **determined by the plugin's
active model**: slife discovers it from `GET /v1/models` (the entry flagged
`active: true`) on load. When the service is unreachable, slife degrades
gracefully to keyword search.

## License

MIT — see the repository root `LICENSE`.
