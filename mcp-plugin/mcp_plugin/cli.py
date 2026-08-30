"""mcp-plugin CLI — configure and maintain external MCP servers.

Commands:
  ``mcp-plugin``               overview of configured servers
  ``mcp-plugin set <s>``       interactively add/configure a server
  ``mcp-plugin set-embed``     configure the embeddings section (semantic
                               search); --base-url required, --model/--api-key
                               optional (omit to keep, "" to clear)
  ``mcp-plugin remove <s>``    remove a server (takes effect next server start)
  ``mcp-plugin build``         rebuild the tool catalog DB + index from live
                               connections (manual config edits, external MCP
                               updates, embeddings model changes)

The CLI is a thin front-end over the same library the server uses: reads and
writes mcp-plugin.json5 through :mod:`mcp_plugin.config` and connects through
:class:`mcp_plugin.connection.ConnectionPool`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from mcp_plugin import config as plugin_config
from mcp_plugin.config import _is_env_ref, _resolve_embedded_refs, _resolve_secret
from mcp_plugin.connection import ServerConfig
from mcp_plugin.platform import resolve_command

# Snakeable for tests.
_input_fn = input
_getpass_fn = getpass.getpass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-plugin",
        description="Standalone MCP gateway — manage external MCP servers.",
        epilog="Run 'mcp-plugin' with no subcommand to show an overview "
               "of the configured MCP servers (name + description).",
    )
    sub = parser.add_subparsers(dest="command")

    p_set = sub.add_parser("set", help="Interactively add/configure a server.")
    p_set.add_argument("server", help="Server name.")

    p_remove = sub.add_parser("remove", help="Remove a server from config.")
    p_remove.add_argument("server", help="Server name.")

    p_embed = sub.add_parser(
        "set-embed", help="Configure the embeddings section (semantic search).",
    )
    p_embed.add_argument(
        "--base-url", required=True,
        help="OpenAI-compatible base URL (e.g. http://127.0.0.1:8000/v1).",
    )
    p_embed.add_argument(
        "--model", default=None,
        help="Embedding model; omit to keep the current value, pass '' to clear "
             "and use the endpoint's active model.",
    )
    p_embed.add_argument(
        "--api-key", "--apikey", dest="api_key", default=None,
        help="API key: empty / plaintext / ${VAR}; omit to keep the current "
             "value, pass '' to clear.",
    )

    sub.add_parser(
        "build", help="Rebuild the tool catalog DB + index from live connections.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    command = args.command
    if command is None:
        return _overview()
    if command == "set":
        return _set_cmd(args.server)
    if command == "set-embed":
        return _set_embed_cmd(args)
    if command == "remove":
        return _remove_cmd(args.server)
    if command == "build":
        return asyncio.run(_build_cmd())
    print(f"Unknown command: {command}")
    return 2


# ── Overview ────────────────────────────────────────────────────────────


def _overview() -> int:
    path = plugin_config.current_path()
    raw = plugin_config.load_config(path)
    servers = plugin_config._servers_dict(raw)
    print(f"mcp-plugin config: {path}")
    items = [(n, e) for n, e in servers.items() if isinstance(e, dict)]
    if not items:
        print("No servers configured. Use 'mcp-plugin set <server>' to add one.")
        return 0
    width = len(str(len(items)))
    for i, (name, entry) in enumerate(items, 1):
        transport = "http" if entry.get("url") else "stdio"
        target = entry.get("url") or entry.get("command") or ""
        flag = "" if entry.get("enabled") is not False else "  (disabled)"
        print(f"  {i:>{width}}. {name:<18} {transport:<5} {target}{flag}")
        if entry.get("description"):
            print(f"{' ' * (width + 8)}{entry['description']}")
    return 0


# ── build ───────────────────────────────────────────────────────────────


async def _build_cmd() -> int:
    """Rebuild the tool catalog DB + index from live connections.

    Handles manual config edits, external MCP tool updates, and embeddings
    model changes.  Connects every enabled server IN PARALLEL, re-syncs its
    tools, rebuilds the FTS index, and (when an embeddings endpoint is
    configured) re-embeds the whole catalog.  Unreachable servers are
    reported, not fatal.
    """
    import asyncio

    from mcp_plugin.connection import ConnectionPool, ServerStatus
    from mcp_plugin.embeddings import EmbeddingClient
    from mcp_plugin.store import ToolStore

    #: Per-server connect cap — a hung npx/uvx spawn must not stall the build.
    _CONNECT_TIMEOUT = 60.0

    raw = plugin_config.load_config()
    servers = plugin_config._servers_dict(raw)
    # Full rebuild — EVERY configured server, including disabled ones (a
    # disabled server's tools are cataloged but marked disabled).
    all_servers = [
        (n, e, e.get("enabled") is not False)
        for n, e in servers.items() if isinstance(e, dict)
    ]
    if not all_servers:
        print("No servers configured. Nothing to build.")
        return 0

    store = ToolStore(plugin_config.db_path())
    await store.open()
    pool = ConnectionPool()
    connected: list[str] = []
    failed: list[str] = []
    try:
        n_disabled = sum(1 for _, _, en in all_servers if not en)
        header = f"[build] connecting to {len(all_servers)} servers"
        if n_disabled:
            header += f" ({n_disabled} disabled — their tools will be marked disabled)"
        print(header, flush=True)

        async def _connect_one(name: str, entry: dict):
            try:
                cfg = plugin_config.resolve_server_config(name, entry)
                cfg.enabled = True  # build catalogs disabled servers too (then marks them)
                conn = await asyncio.wait_for(
                    pool.add_server(cfg), timeout=_CONNECT_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001 - report any connect failure
                return name, None, f"{type(e).__name__}: {e}"
            if conn.status == ServerStatus.CONNECTED:
                return name, conn, None
            return name, None, conn.error or conn.status.value

        enabled_by_name = {n: en for n, _, en in all_servers}
        tasks = [asyncio.create_task(_connect_one(n, e)) for n, e, _ in all_servers]
        for fut in asyncio.as_completed(tasks):
            name, conn, err = await fut
            if err:
                failed.append(f"{name}: {err}")
                print(f"  [--] {name}: {err}", flush=True)
            else:
                assert conn is not None  # err is None ⇒ a connected connection
                result = await store.sync_server(name, conn.list_tools())
                connected.append(name)
                if not enabled_by_name[name]:
                    await store.disable_server_tools(name)
                    print(
                        f"  [--] {name} (disabled): {result['upserted']} tools "
                        "marked disabled",
                        flush=True,
                    )
                else:
                    print(
                        f"  [OK] {name}: {result['upserted']} tools", flush=True,
                    )

        await store.rebuild_fts()
        total = await store.count_tools()
        print(f"[build] catalog: {total} tools from {len(connected)}/{len(all_servers)} servers")

        semantic = "disabled (no embeddings section in mcp-plugin.json5)"
        emb = EmbeddingClient.from_plugin_config()
        if emb.available:
            if await emb.load():
                await store.drop_embeddings()
                model_id = f"api:{emb.model}"
                while True:
                    docs = await store.get_unembedded_docs(limit=20)
                    if not docs:
                        break
                    for doc in docs:
                        vec = await emb.embed_one(doc["text"])
                        if vec:
                            await store.replace_embedding(doc["doc_id"], vec, model_id)
                await store.set_meta("embedding_model", model_id)
                semantic = f"{emb.model} (dim={emb.dimension}, {total} embedded)"
            else:
                semantic = "configured but failed to load"
        print(f"[build] embeddings: {semantic}")

        return 0
    finally:
        await pool.shutdown()
        await store.close()


# ── single-server check (used by `set`'s "Test connection now?") ────────


async def _test_mcp_cmd(server: str) -> int:
    """Bare-connect to one MCP server (no pool/framework) to confirm it works."""
    raw = plugin_config.load_config()
    servers = plugin_config._servers_dict(raw)
    entry = servers.get(server)
    if not isinstance(entry, dict):
        print(f"Server '{server}' is not configured.")
        return 1
    cfg = plugin_config.resolve_server_config(server, entry)
    print(f"[connect] {server} ({cfg.transport}) ...")
    try:
        if cfg.transport == "http":
            tools, info = await _raw_connect_http(cfg)
        else:
            tools, info = await _raw_connect_stdio(cfg)
    except Exception as e:  # noqa: BLE001 - report any bare-connect failure
        print(f"[FAIL] {server}: {type(e).__name__}: {e}")
        return 1
    print(f"[OK] {server}: connected ({info}), {len(tools)} tools")
    for tool in tools:
        print(f"  {tool}")
    return 0


async def _raw_connect_stdio(cfg: ServerConfig) -> tuple[list[str], str]:
    """Spawn *cfg* as a subprocess and bare-connect over stdio (no framework)."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    if cfg.env:
        env.update({k: _resolve_secret(v) for k, v in cfg.env.items()})
    resolved_args = [
        _resolve_secret(arg) if _is_env_ref(arg) else _resolve_embedded_refs(arg)
        for arg in cfg.args
    ]
    if cfg.os_paths:
        from mcp_plugin.os_detect import get_os_accessible_paths
        for p in get_os_accessible_paths():
            resolved_args += ["--allow-path", p]
    params = StdioServerParameters(
        command=resolve_command(cfg.command), args=resolved_args, env=env or None,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        result = await session.list_tools()
    info = f"{init.serverInfo.name} {init.serverInfo.version}".strip()
    return [t.name for t in result.tools], info


async def _raw_connect_http(cfg: ServerConfig) -> tuple[list[str], str]:
    """Bare-connect to an HTTP MCP endpoint (no framework)."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = _resolve_embedded_refs(cfg.url)
    headers = {}
    if cfg.headers:
        headers = {k: _resolve_embedded_refs(v) for k, v in cfg.headers.items()}
    # The SDK does not own a caller-provided http_client, so it is created and
    # closed here (headers carry e.g. bearer-token auth).
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0),
    ) as http_client, streamable_http_client(
        url, http_client=http_client,
    ) as (read, write, _), ClientSession(read, write) as session:
        init = await session.initialize()
        result = await session.list_tools()
    info = f"{init.serverInfo.name} {init.serverInfo.version}".strip()
    return [t.name for t in result.tools], info


# ── set-embed (embeddings section) ──────────────────────────────────────


def _set_embed_cmd(args: argparse.Namespace) -> int:
    """Write/update the top-level ``embeddings`` section.

    ``--base-url`` is required; ``--model`` / ``--api-key`` are optional —
    omitted values are preserved (see :func:`set_embeddings`).  The api_key
    stores whatever form the user passed (empty / plaintext / ``${VAR}``)
    verbatim — resolution happens at use time, so the command never prints
    the key itself.
    """
    plugin_config.set_embeddings({
        "base_url": args.base_url,
        "model": args.model,
        "api_key": args.api_key,
    })
    print(f"[OK] embeddings saved. base_url={args.base_url}")
    if args.model is not None:
        print(f"     model={args.model}")
    if args.api_key is not None:
        print(f"     api_key={'empty' if args.api_key == '' else '(set, hidden)'}")
    if not args.base_url or args.base_url.startswith("${"):
        print("NOTE: a placeholder/empty base_url leaves semantic search disabled "
              "(keyword fallback only).")
    print("Changes apply at the next server start, or run 'mcp-plugin build' "
          "to (re)index now.")
    return 0


# ── remove ──────────────────────────────────────────────────────────────


def _remove_cmd(server: str) -> int:
    if plugin_config.remove_server_entry(server):
        print(f"[OK] Removed server '{server}' "
              "(takes effect at the next server start).")
        return 0
    print(f"Server '{server}' is not configured.")
    return 1


# ── set (interactive) ───────────────────────────────────────────────────


def _prompt(label: str, default: str = "") -> str:
    if default:
        raw = _input_fn(f"{label} [{default}]: ").strip()
        return raw or default
    return _input_fn(f"{label}: ").strip()


def _ask_yes_no(label: str, default: str = "no") -> bool:
    return _prompt(label, default).strip().lower() in ("y", "yes")


def _set_cmd(server: str) -> int:
    print(f"Configuring server '{server}' "
          f"(config: {plugin_config.current_path()}).")
    entry: dict = {}

    transport = _prompt("Transport", "stdio")
    if transport == "http":
        entry["url"] = _prompt("URL (SSE or /mcp endpoint)")
    else:
        entry["command"] = _prompt("Command (e.g. npx)")
        args_raw = _prompt("Args (space-separated; empty to skip)")
        if args_raw:
            entry["args"] = args_raw.split()

    env_raw = _prompt("Env overrides (KEY=VALUE, comma-separated; empty to skip)")
    if env_raw:
        entry["env"] = {
            kv.split("=", 1)[0]: kv.split("=", 1)[1]
            for kv in env_raw.split(",")
            if "=" in kv
        }

    desc = _prompt("Description (empty to skip)")
    if desc:
        entry["description"] = desc

    if _ask_yes_no("OAuth 2.0 device flow?"):
        auth: dict = {
            "type": "oauth",
            "device_auth_url": _prompt("Device auth URL"),
            "token_url": _prompt("Token URL"),
            "client_id": _prompt("Client ID"),
        }
        if _ask_yes_no("Client secret?"):
            auth["client_secret"] = _getpass_fn("Client secret (hidden): ")
        scopes = _prompt("Scopes (space-separated; empty to skip)")
        if scopes:
            auth["scopes"] = scopes.split()
        entry["auth"] = auth

    plugin_config.add_server_entry(server, entry)
    print(f"[OK] Saved server '{server}'.")
    if _ask_yes_no("Test connection now?"):
        return asyncio.run(_test_mcp_cmd(server))
    return 0


if __name__ == "__main__":
    sys.exit(main())