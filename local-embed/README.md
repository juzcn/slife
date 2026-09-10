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
                                              │  models load on demand │
                                              └───────────────────────┘
```

## Features

- **OpenAI-compatible** `/v1/embeddings` + Models API + `/health`.
- Two backends (GGUF / transformer), **many models as peers — each request
  names the one it wants** (standard OpenAI semantics, no "active model").
- **Lazy load** — startup is fast; each model materialises on the first
  request that names it (per-model `autoload: true` preloads a specific
  model at startup).
- **Real dimension** reported after load (a guessed width is never served).
- Runs standalone, and can also be **loaded as a slife plugin** (see
  [Loading as a slife plugin](#loading-as-a-slife-plugin)).

Requires Python ≥ 3.13.

## Install

The core package is `fastmcp` + `starlette` + `json5`; the model backends are
optional extras.

One-click installers (install `uv` if needed, then `uv tool install --force
local-embed` — the backend is **not** included, see below):

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/local-embed/install.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/local-embed/install.ps1 | iex"
```

Manual installs are equivalent:

```bash
# the app + CLI (standalone tool)
uv tool install --force local-embed

# with a backend:
uv tool install --force "local-embed[transformer]"
uv tool install --force "local-embed[gguf]"
```

`--force` (used by the installers too) is **idempotent** — the first run
installs, re-running **updates** to the latest PyPI release.  A plain
`uv tool install local-embed` no-ops when the tool is already installed.

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

Everything — host, port, models, backend — comes from
`local_embed.json5`, written by the CLI helpers or by hand. Path resolution:
`$LOCAL_EMBED_FILE` > slife project root (dev) > `~/.local-embed/local_embed.json5`.

```json5
{
  models: {
    "bge-m3": { backend: "gguf", gguf_path: "…", device: "", autoload: false },
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

- `models` — map of name → `{backend, gguf_path | model, device, max_tokens, autoload}`.
  Every configured model is a peer; there is **no `active_model`** — each
  request names the model it wants (standard OpenAI semantics).
- `autoload` (per model, default `false`) — a model's weights are large and
  memory-hungry, so nothing loads until a request names it (**lazy**).
  `autoload: true` on one model eager-loads just that model in the
  background at startup (memory paid up front for a warm first embed); every
  unflagged model stays lazy.
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

`input` is a string or a list of strings; `model` is **required** and names
any configured model.  Returns the standard shape:

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

OpenAI forbids empty-string input, so an empty/whitespace string in the
batch is rejected with a strict `400` — there is no zero-vector row
alignment on the wire; filter blanks before batching.

#### Errors — the OpenAI contract

Every error uses the standard envelope
`{"error": {"message", "type", "param", "code"}}` (`param` / `code` are
`null` when not applicable):

| Status | `type` | When | `param` / `code` |
|---|---|---|---|
| `400` | `invalid_request_error` | unparseable JSON body; `input` not a string/array of strings; missing `model`; empty / whitespace-only input | `input` / `model` |
| `400` | `invalid_request_error` | input exceeds the model's context length (`max_tokens` — never silently truncated) | `input` / `context_length_exceeded` |
| `404` | `invalid_request_error` | unknown `model` ("does not exist or you do not have access to it") | — / `model_not_found` |
| `503` | `server_error` | the model's engine is **still loading** (is_loading — retry shortly) or unavailable (backend dependency missing, load failed) | — |
| `500` | `server_error` | unexpected internal failure | — |

Split long documents into pieces of at most `max_tokens` tokens client-side
(slife's semantic drainer does this automatically).

### `GET /v1/models`

Every configured model, each with its real embedding dimension
(`dimension` / `dimension_known`), backend, and load state.  The listing is
standard — no `active` marker, all models are peers.

### `GET /v1/models/{id}`

One model's detail (the OpenAI `retrieve` endpoint); 404 with the standard
error envelope when the id is unknown.

### `GET /health`

Liveness + engine state: `{status, models: [{name, backend, model,
dimension, dimension_known, loaded, available, max_tokens}]}` — `status`
is `ok` when any configured model's backend is usable.

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

`set` (transformer) and `set-gguf` (gguf) upsert a model in the config and
pin port (and, for `set`, the HF cache + offline flag). Both are
**idempotent** — re-running yields the same config — and leave other models
untouched.  There is no `active_model`: every configured model is a peer
and requests name the one they want.

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
`__check` status probe (model list, dimensions, load state) — local-embed is
a *service-provider* MCP server, not a tool provider. To embed text, point
your consumer at the OpenAI-compatible `/v1/*` API instead. A client that only
speaks stdio can reach it through a stdio→HTTP bridge (e.g. `mcp-remote`).

## Running as a slife embedding backend

local-embed is a **standalone daemon** — slife does not spawn it, manage it,
or mount it as a plugin. Start it yourself (`local-embed` on PATH, or
`python -m local_embed.server`), like Mosquitto; slife consumes it purely as
an OpenAI-compatible HTTP endpoint, never through slife's plugin lifecycle or
MCP tools.

Point slife's embedding config at the daemon's stable port:

```json5
embeddings: {
  providers: {
    local: {
      base_url: "http://127.0.0.1:17347/v1",  // stable port from local_embed.json5
      api_key: "local",
      model: "bge-m3",                        // id POSTed on /v1/embeddings
    }
  },
  active_model: "local",      // provider-id only, or "local/<model>"
  enabled: true
}
```

slife treats every embedding model as a remote OpenAI-compatible endpoint —
local-embed is one such endpoint. The model a request hits is the one the
caller names; when the config names no model, slife discovers it from
`GET /v1/models` (the first entry) on load. When the daemon is unreachable,
slife degrades gracefully to keyword search (`check_local_embed` in
`system_health` probes the daemon's HTTP endpoint and reports it down).

The MCP surface it still serves is the internal **`__check`** (engine status:
model list, dimensions, load state) — a service-provider facade
for direct probing, not a slife plugin contract. It also serves plain OpenAPI
routes (`/v1/embeddings`, `/v1/models`, `/health`) on the same port via
`@mcp.custom_route` — one port, two protocols, no slife involvement.

## License

MIT — see the repository root `LICENSE`.
