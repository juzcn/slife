# Extract ngrok sharing into a standalone `sharefile` plugin

## Goal

Move all ngrok tunnel + public-URL sharing functionality OUT of the `memfiles`
plugin into a new, standalone plugin named **`sharefile`**.  `memfiles` becomes
purely a private notes/diary/files cabinet (returns only local clickable paths,
never URLs).  `sharefile` is a self-contained plugin whose **only LLM-visible
tool is `share_file`** — the LLM decides explicitly when a local file becomes a
public HTTPS URL.

This is the cleaner end-state of the "don't auto-share" work: sharing is a
separate concern with a separate plugin, not a feature of the file cabinet.

## Current state (already done this session)

- `note_save` / `diary_write` / `file_save` / `url_save` all return local
  paths only — no share URL, no token registration (share_file is the sole
  publisher, called by the LLM).
- `_saved_result` in memfiles/server.py now just returns `Saved: <path>`.
- Tests updated (note/diary return local path + empty registry).

## What moves from memfiles → sharefile

From `slife/plugins/memfiles/`:
- `tunnel.py` → `slife/plugins/sharefile/tunnel.py` (unchanged; the env var
  `SLIFE_MEMFILES_URL` and `SLIFE_MEMFILES_PORT` are renamed to
  `SLIFE_SHAREFILE_URL` / `SLIFE_SHAREFILE_PORT`).
- The token registry (`_register_file` / `_lookup_file` / `_reset_registry`),
  `_content_disposition`, the `GET /share/{file_id}` custom route
  (`handle_share`), `__register_file`, `__tunnel_status`, and the `share_file`
  MCP tool.
- The tunnel lifecycle (eager start, monitor) and port binding in `main()`.

### New plugin layout: `slife/plugins/sharefile/`

- `__init__.py` — docstring: "Sharefile plugin — public file sharing.  Sole
  LLM-visible tool: `share_file`."
- `tunnel.py` — moved verbatim (NgrokTunnel + module API), with the env-var
  rename (`SLIFE_MEMFILES_URL` → `SLIFE_SHAREFILE_URL`).
- `server.py` — new `mcp = create_plugin_server("slife-sharefile", ...)`,
  owning:
  - `_PLUGIN_PORT` + eager tunnel start in lifespan + `start_monitor` /
    `stop_monitor` / `stop_tunnel` in shutdown,
  - token registry + `_content_disposition`,
  - `@mcp.custom_route("/share/{file_id}", methods=["GET"])` → `handle_share`,
  - `@mcp.tool(name="__tunnel_status")` (internal — harness health check),
  - `@mcp.tool(name="__register_file")` (internal — kept for parity/test),
  - `@mcp.tool(name="share_file")` (the ONLY LLM-visible tool),
  - `main()` binding its own port (must know the port to forward the tunnel),
    like memfiles' current `main()`.

### Removed from memfiles

- `slife/plugins/memfiles/tunnel.py` (whole file).
- `share_file`, `__register_file`, `__tunnel_status`, `handle_share`,
  `_register_file` / `_lookup_file` / `_reset_registry`,
  `_content_disposition`, the token registry, `_ensure_tunnel`, the eager
  tunnel start + monitor in lifespan, and `_PLUGIN_PORT` / port binding in
  `main()`.  `memfiles`' `main()` becomes a plain `run_plugin_server(mcp)`
  (like `media`).
- Update memfiles module docstring + plugin instructions: no more "public
  sharing" — memfiles is the private cabinet only.

## Harness wiring changes

### `slife/mcp/tool_adapter.py`
- `_MEMFILES_SERVER` comment drops "public sharing" (stays a DIRECT built-in).
- Add `_SHAREFILE_SERVER = "sharefile"` to the DIRECT route list (it has its
  own MCP client, same as media/memfiles).

### `slife/agent/service.py`
- `start_plugin_server("sharefile")`: replace the `memfiles` block's tunnel
  wiring with a `sharefile` block that:
  - generic-spawns the plugin,
  - sets `self._tool_ctx.sharefile_client`,
  - exports `SLIFE_SHAREFILE_PORT` for subagent reuse,
  - starts the generic watchdog,
  - calls `_watch_sharefile_tunnel()` (renamed from `_watch_memfiles_tunnel`).
- `memfiles` drops to the fully-generic path (spawn + watchdog, no tunnel
  watch, no port export for subagent reuse).
- `_plugins` dict: add `"sharefile": PluginLifecycle("sharefile", self)`.
- `_watch_sharefile_tunnel` / `_check_sharefile_tunnel` probe
  `__tunnel_status` on the sharefile client.
- `connect_sharefile_http(port)` (renamed from `connect_memfiles_http`) for
  subagent reuse.
- `stop_sharefile()` (renamed from `stop_memfiles()`); `stop_memfiles()` now
  just stops the cabinet.
- `_start_generic_watchdog` restart callback: re-point `sharefile_client`.

### `slife/tools/context.py`
- Rename `memfiles_client` field → `sharefile_client` (docstring updated:
  "the sharefile plugin's MCP client — used by `check_sharefile` to query
  tunnel status via `__tunnel_status`").

### `slife/tools/system.py`
- `check_memfiles` → `check_sharefile`: probes `__tunnel_status` on the
  sharefile client; component label `"sharefile"`.
- `CheckMemfilesTool` → `CheckSharefileTool` (name `check_sharefile`,
  description "File sharing tunnel status (online/offline) for share_file").
- `_CHECK_FUNCTIONS`: `"check_memfiles"` → `"check_sharefile"`.
- `_CLIENT_FIELD`: `"check_sharefile": "sharefile_client"`.

### `slife/ui/app.py`
- `_stop_plugins`: `_stop_one("memfiles", ...)` → add `_stop_one("sharefile", ...)`
  and keep memfiles stop.
- `on_tunnel_down` stays; the `_check_memfiles_tunnel` naming inside service
  moves to sharefile.
- Plugin list auto-discovery picks up `sharefile` automatically (no change
  needed beyond service's special-case rename).

### `slife/subagent/headless.py`
- Reuse the main agent's sharefile plugin (not a second tunnel): read
  `SLIFE_SHAREFILE_PORT`, call `connect_sharefile_http`.  (Subagents still
  reuse memfiles' cabinet too — keep `connect_memfiles_http`.)

### `slife/plugins/mcp/server.py`
- Add `"sharefile"` to `_RESERVED_SERVER_NAMES` (reserve the plugin name so
  a user can't define an external MCP server called "sharefile").

### `slife/tools/factory.py` comment
- Update the comment that lists memfiles tools (share_file now lives in the
  sharefile plugin).

### `slife/tools/vision.py`
- Docstring: `memfiles__share_file` → `sharefile__share_file`.

### `slife/agent/templates/slife.j2`
- Line 37: `memfiles__share_file` → `sharefile__share_file`.

### `slife/plugins/media/` (server.py + adapters/dashscope_aigc.py)
- Comments referencing "share_file" stay (they're generic), but update the
  phrasing if they say "from share_file" → keep; no functional change.  (Only
  comment touch-ups where a plugin name is named.)

## Docs

- `DESIGN.md` — memfiles section: memfiles is now the private cabinet (all
  save tools return local paths; no auto-publish).  New "Sharefile plugin"
  subsection under the plugin architecture: standalone plugin, sole tool
  `share_file`, token registry + tunnel + `/share/{token}` route owned by it.
  Update the "Ngrok Tunnel" section to say the tunnel is owned by the
  sharefile plugin.
- `README.md` — tool table: add `sharefile__share_file`; note memfiles tools
  return local paths.  Update the line that says "`memfiles__share_file`
  publishes any local file".
- `slife/plugins/__init__.py` docstring — add `sharefile` to the built-in list.
- `slife/paths.py` `get_memfiles_dir` docstring — it only serves the cabinet
  now (the "accessible via both local path and sharing URL" phrase is
  outdated).

## Tests

- `tests/test_memfiles_tunnel.py` → move to `tests/test_sharefile_tunnel.py`,
  retarget imports from `slife.plugins.memfiles.tunnel` →
  `slife.plugins.sharefile.tunnel`, env var `SLIFE_MEMFILES_URL` →
  `SLIFE_SHAREFILE_URL`.
- `tests/test_memfiles_plugin.py`:
  - Move `TestShareFile`, `TestShareRoute`, `TestRegistry` (token registry),
    the `__tunnel_status` / `__register_file` tests → new
    `tests/test_sharefile_plugin.py` targeting `slife.plugins.sharefile.server`.
  - Keep note/diary/file/url save tests in memfiles (they now assert local
    path, no URL, empty registry — no tunnel patch needed).
  - `_active_tunnel` / `_offline_tunnel` helpers move to the sharefile test
    (memfiles tests no longer need them).
- `tests/test_agent_service.py` — retarget the tunnel-readiness-watch tests
  from `_check_memfiles_tunnel` → `_check_sharefile_tunnel`, `__tunnel_status`
  on the sharefile client.
- `tests/test_system_prompt.py` — update if it asserts tool names/schema
  (check for `memfiles__share_file` / `sharefile__share_file`).

## Migration steps (order)

1. Create `slife/plugins/sharefile/` (tunnel.py copied + env rename, server.py
   assembled from the moved pieces, `__init__.py`).
2. Remove the moved pieces from memfiles/server.py + delete tunnel.py; fix
   memfiles docstrings/instructions/main().
3. Harness wiring: tool_adapter, service.py, context.py, system.py, ui/app.py,
   subagent/headless.py, mcp/server.py `_RESERVED_SERVER_NAMES`, factory.py,
   vision.py, slife.j2.
4. Docs + paths.py + plugins/__init__ docstring.
5. Tests: split/move/retarget.
6. Run the full suite; verify no `memfiles__share_file` references remain
   (except historical git).

## Verification

- `uv run pytest` — full suite green.
- `grep -rn "memfiles__share_file\|check_memfiles\|connect_memfiles\|SLIFE_MEMFILES\|memfiles_client" slife/ tests/` — only the cabinet references remain (connect_memfiles_http for subagent cabinet reuse).
- Sanity: the sharefile plugin loads via auto-discovery (like media), exposes
  exactly one LLM tool `share_file`, and its internal tools
  (`__tunnel_status`, `__register_file`) are filtered from the LLM registry.
