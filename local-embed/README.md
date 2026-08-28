# local-embed

**Standalone local embedding server** — loads one GGUF (llama-cpp) or HF
transformer embedding model **once** and exposes it as an
[OpenAI-compatible](https://platform.openai.com/docs/api-reference/embeddings)
HTTP service — `POST /v1/embeddings` plus the Models API
(`GET /v1/models`, `GET /v1/models/{id}`) — **plus** FastMCP tools, on a
single port.

Built for **slife** (its `memdb` and `memfiles` plugins both call this
service over HTTP, so the model is never loaded twice in one process
tree), but it is a fully standalone package — any OpenAI-compatible
client can use it.

```
                POST http://127.0.0.1:8000/v1/embeddings
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

```bash
# GGUF backend (llama-cpp-python)
uv tool install 'local-embed[gguf]'            # or: uv pip install 'local-embed[gguf]'
# Transformer backend (sentence-transformers)
uv tool install 'local-embed[transformer]'     # or: uv pip install 'local-embed[transformer]'
```

Python 3.13+. The server core only depends on `fastmcp` + `starlette`; the
heavy model backends are optional extras.

## Run

Everything — host, port, model, backend, `gguf_path`, `device` — comes from
`local_embed.json5`: a top-level `models` map (each entry `{backend,
model, gguf_path, device, max_tokens}`), `active_model`, and standalone
`host` / `port` keys (single-model top-level `backend`/`model`/`gguf_path`/
`device` still work).  The CLI takes no model/endpoint flags:

```bash
local-embed
```

By default it binds `127.0.0.1:8000` (local only — never exposed to the
network), configurable via the top-level `host` / `port` keys.

### Transformer models & the `env:` section

A `transformer` model is referenced by its HF *repo name* (`BAAI/bge-m3`).
The HuggingFace hub resolves that name against its cache (default
`~/.cache/huggingface`), downloading on first load if missing.  To keep the
server self-contained — point it at an existing local cache, or force
offline — put the env vars in `local_embed.json5`'s `env:` section (injected
into this process before any model loads; an existing shell env var wins):

```json5
{
  env: {
    HF_HUB_CACHE: "C:\\Users\\me\\HuggingFace\\hub",  // existing cache
    HF_HUB_OFFLINE: "1"                                // never hit the network
  },
  models: {
    "bge-m3-transformer": { backend: "transformer", model: "BAAI/bge-m3", device: "" }
  }
}
```

Without `HF_HUB_CACHE` the model resolves against the default cache — a
model already downloaded elsewhere would be silently re-fetched.

## Use

```bash
curl http://127.0.0.1:8000/v1/embeddings \
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
`http://127.0.0.1:8000/v1` (e.g. slife's `memdb.embedding` api backend, or
the `openai` Python package):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="local")
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
memdb: {
  embedding: {
    base_url: "http://127.0.0.1:8000/v1",  // stable port from local_embed.json5
    api_key: "local",
  }
}
```

The plugin binds the **stable port** from `local_embed.json5` (default
`8000`), so slife's `base_url` is fixed whether local-embed runs as the
plugin or standalone.  When the service is unreachable, slife degrades
gracefully to keyword search.

## CLI

```
local-embed --help
```

- `--log-level` — logging level (default `INFO`)

Host, port and model config are read from `local_embed.json5` — the CLI has no
model/endpoint flags (`--host`, `--port`, `--backend`, `--model`,
`--gguf-path`, `--device` were removed).

## License

MIT — see the repository root `LICENSE`.
