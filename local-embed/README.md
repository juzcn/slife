# local-embed

**Standalone local embedding server** — loads one GGUF (llama-cpp) or HF
transformer embedding model **once** and exposes it as an
[OpenAI-compatible](https://platform.openai.com/docs/api-reference/embeddings)
`/v1/embeddings` HTTP endpoint **plus** FastMCP tools, on a single port.

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

```bash
# GGUF model (recommended — offline, no HF download)
local-embed --backend gguf --model bge-m3 --gguf-path /path/to/bge-m3-q4_k_m.gguf --port 8000

# HF transformer model (downloads from HF hub on first load)
local-embed --backend transformer --model BAAI/bge-m3 --port 8000
```

By default it binds `127.0.0.1:8000` (local only — never exposed to the
network).

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

Other endpoints:

- `GET /v1/models` — the loaded model + its real embedding dimension.
- `GET /health` — `{status, backend, model, dimension, loaded}`.
- `POST /mcp` — FastMCP streamable-HTTP endpoint (tools `embed_status` and
  `embed`).

## Dimension

The real output width is only known once the model is loaded (`n_embd` /
`get_sentence_embedding_dimension`).  `GET /v1/models` reports the real
dimension, so a client can size its vector table correctly — a wrong width
silently drops every mis-sized embedding.

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

- `--host` / `--port` — bind address (default `127.0.0.1:8000`)
- `--backend gguf|transformer` — model backend (default `gguf`)
- `--model` — model name/id (for metadata and dim guessing)
- `--gguf-path` — path to the GGUF file (required for `backend=gguf`)
- `--device cpu|cuda` — transformer device (default auto)
- `--log-level` — logging level (default `INFO`)

## License

MIT — see the repository root `LICENSE`.
