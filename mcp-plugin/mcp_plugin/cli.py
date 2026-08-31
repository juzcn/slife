"""mcp-plugin CLI — configure and maintain external MCP servers.

mcp-plugin is a Streamable HTTP MCP server, so server management is done
through MCP tools (``mcp_set`` / ``mcp_remove`` / …); this CLI covers what
the tools do not:
  ``mcp-plugin``               overview of configured servers
  ``mcp-plugin set-embed``     configure the embeddings section (semantic
                               search); --base-url required, --model/--api-key
                               optional (omit to keep, "" to clear)
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
import sys

from mcp_plugin import config as plugin_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-plugin",
        description="Standalone MCP gateway - manage external MCP servers.",
        epilog="Run 'mcp-plugin' with no subcommand to show an overview "
               "of the configured MCP servers (name + description).",
    )
    sub = parser.add_subparsers(dest="command")

    p_embed = sub.add_parser(
        "set-embed", help="Configure the embeddings section (semantic search).",
    )
    p_embed.add_argument(
        "--base-url", required=True,
        help="OpenAI-compatible base URL (e.g. http://127.0.0.1:17347/v1).",
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
    if command == "set-embed":
        return _set_embed_cmd(args)
    if command == "build":
        try:
            return asyncio.run(_build_cmd())
        except KeyboardInterrupt:
            print("\nInterrupted.", flush=True)
            return 130
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
        print("No servers configured. Manage them with the mcp_set/mcp_remove "
              "tools (when hosted), or edit mcp-plugin.json5 and run "
              "'mcp-plugin build'.")
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

    Bounded by an overall wall-clock deadline — the guarantee that ``build``
    always exits (success, per-server failure, or timeout) and never hangs;
    rerun it to finish a partially-rebuilt catalog.
    """
    import asyncio

    from mcp_plugin.connection import ConnectionPool, ServerStatus
    from mcp_plugin.embeddings import EmbeddingClient
    from mcp_plugin.store import ToolStore

    #: Per-server connect cap — a hung npx/uvx spawn must not stall the build.
    _CONNECT_TIMEOUT = 60.0
    #: Overall wall-clock deadline.  Every network path below is already
    #: time-boxed individually (60s connect, 30s handshake, 5s probe, 60s
    #: embed), but the hard outer deadline is what guarantees ``build`` ALWAYS
    #: exits even if something escapes those caps (a synchronously-blocking
    #: spawn, a wedged pipe, an unkillable grandchild).
    _BUILD_DEADLINE = 300.0
    #: Teardown cap.  ``_BUILD_DEADLINE`` only guards ``_run_build()``; the
    #: ``finally`` below (pool.shutdown → per-connection cleanups) runs outside
    #: it and its kills were NOT guaranteed to finish.  On WSL a npx/uvx
    #: grandchild holding the stdio pipes caused ``process.wait()`` to never
    #: resolve, hanging the whole command *after* all work was done.  A
    #: best-effort teardown always exits, so the command never wedges.
    _TEARDOWN_TIMEOUT = 60.0

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
    tasks: "list[asyncio.Task]" = []

    async def _run_build() -> int:
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
        tasks.extend(
            asyncio.create_task(_connect_one(n, e)) for n, e, _ in all_servers
        )
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

        emb = EmbeddingClient.from_plugin_config()
        if not emb.available:
            print("[build] embeddings: disabled (no embeddings configured) — semantic search off (keyword-only)")
        elif not await emb.probe_available():
            # Auto-degrade: the endpoint is configured but unreachable (e.g.
            # local-embed not running, wrong base_url).  Never block the build
            # on it — skip embeddings and tell the user, keyword search works.
            print(
                "[build] embeddings: auto-degraded — endpoint "
                f"'{emb.base_url}' not reachable (semantic search off, keyword-only)",
            )
        elif await emb.load():
            await store.drop_embeddings()
            model_id = f"api:{emb.model}"
            embed_failed = False
            while True:
                docs = await store.get_unembedded_docs(limit=20)
                if not docs:
                    break
                for doc in docs:
                    vec = await emb.embed_one(doc["text"])
                    if not vec:
                        # Endpoint died mid-index (e.g. local-embed restarted or
                        # the model is not ready → 503).  A failed doc stays
                        # unembedded and get_unembedded_docs would hand it back
                        # forever — break and degrade instead of looping.
                        embed_failed = True
                        break
                    await store.replace_embedding(doc["doc_id"], vec, model_id)
                if embed_failed:
                    break
            if embed_failed:
                print("[build] embeddings: failed mid-index — semantic search off (keyword-only)")
            else:
                await store.set_meta("embedding_model", model_id)
                semantic = f"{emb.model} (dim={emb.dimension}, {total} embedded)"
                print(f"[build] embeddings: {semantic}")
        else:
            print("[build] embeddings: configured but failed to load — semantic search off (keyword-only)")
        return 0

    try:
        # Hard outer deadline — the guarantee that build always exits.  On
        # timeout wait_for cancels _run_build; the finally below still tears
        # the pool down (killing spawned npx/uvx trees), then we report.
        return await asyncio.wait_for(_run_build(), timeout=_BUILD_DEADLINE)
    except asyncio.TimeoutError:
        print(
            f"\n[build] exceeded {_BUILD_DEADLINE:.0f}s deadline — catalog partially "
            "rebuilt; run `mcp-plugin build` again to finish.",
            flush=True,
        )
        return 124
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C mid-build: cancel the in-flight connect tasks and report a
        # clean interruption instead of a traceback.  The catalog is partially
        # rebuilt — rerunning `mcp-plugin build` finishes it.
        print("\n[build] interrupted — run `mcp-plugin build` again to finish.", flush=True)
        return 130
    finally:
        for t in tasks:
            t.cancel()
        # Bounded teardown — shutting the pool down (killing npx/uvx trees,
        # closing HTTP sessions) must never hang the command after the work
        # is done.  On timeout shutdown is cancelled and the server
        # processes are left to be reaped by the OS.
        try:
            await asyncio.wait_for(pool.shutdown(), timeout=_TEARDOWN_TIMEOUT)
        except Exception:
            pass
        try:
            await asyncio.wait_for(store.close(), timeout=_TEARDOWN_TIMEOUT)
        except Exception:
            pass


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


if __name__ == "__main__":
    sys.exit(main())