"""mcp-plugin CLI — configure and test external MCP servers.

Commands (global flag ``--config PATH`` overrides the config location):
  ``mcp-plugin``             overview of configured servers
  ``mcp-plugin list [s]``    list servers and each server's tools
  ``mcp-plugin set <s>``     interactively add/configure a server
  ``mcp-plugin remove <s>``  remove a server (takes effect next server start)
  ``mcp-plugin test [s]``    connect + initialize verification

The CLI is a thin front-end over the same library the server uses: reads and
writes mcp-plugin.json5 through :mod:`mcp_plugin.config` and connects through
:class:`mcp_plugin.connection.ConnectionPool`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from mcp_plugin import config as plugin_config
from mcp_plugin.connection import ConnectionPool, ServerStatus

# Snakeable for tests.
_input_fn = input
_getpass_fn = getpass.getpass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-plugin",
        description="Standalone MCP gateway — manage external MCP servers.",
    )
    parser.add_argument(
        "--config",
        help="Path to mcp-plugin.json5 (default: $MCP_PLUGIN_FILE, "
        "then ~/.mcp-plugin/mcp-plugin.json5).",
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List servers and their tools.")
    p_list.add_argument("server", nargs="?", help="Server name (default: all).")

    p_set = sub.add_parser("set", help="Interactively add/configure a server.")
    p_set.add_argument("server", help="Server name.")

    p_remove = sub.add_parser("remove", help="Remove a server from config.")
    p_remove.add_argument("server", help="Server name.")

    p_test = sub.add_parser("test", help="Test server connectivity.")
    p_test.add_argument("server", nargs="?", help="Server name (default: all).")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plugin_config.set_config_path(args.config)

    command = args.command
    if command is None:
        return _overview()
    if command == "list":
        return asyncio.run(_list_cmd(args.server))
    if command == "set":
        return _set_cmd(args.server)
    if command == "remove":
        return _remove_cmd(args.server)
    if command == "test":
        return asyncio.run(_test_cmd(args.server))
    print(f"Unknown command: {command}")
    return 2


# ── Overview ────────────────────────────────────────────────────────────


def _overview() -> int:
    path = plugin_config.current_path()
    raw = plugin_config.load_config(path)
    servers = plugin_config._servers_dict(raw)
    print(f"mcp-plugin config: {path}")
    if not servers:
        print("No servers configured. Use 'mcp-plugin set <server>' to add one.")
        return 0
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        transport = "http" if entry.get("url") else "stdio"
        target = entry.get("url") or entry.get("command") or ""
        flag = "" if entry.get("enabled") is not False else "  (disabled)"
        print(f"  {name:<18} {transport:<5} {target}{flag}")
        if entry.get("description"):
            print(f"      {entry['description']}")
    return 0


# ── Pool helper (shared by list / test) ────────────────────────────────


async def _make_pool(
    server: str | None,
    *,
    force: bool,
) -> tuple[ConnectionPool, dict]:
    """Build a fresh pool with the selected servers' connections.

    *force* (test) connects even disabled servers; the plain *list* respects
    the enabled flag.  Returns ``(pool, {name: MCPServerConnection})``.
    """
    raw = plugin_config.load_config()
    servers = plugin_config._servers_dict(raw)
    if server is not None and server not in servers:
        print(f"Server '{server}' is not configured.")
        raise _NoSuchServer(server)

    pool = ConnectionPool()
    conns: dict = {}
    selected = [(n, e) for n, e in servers.items() if server is None or n == server]
    for name, entry in selected:
        if not isinstance(entry, dict):
            continue
        cfg = plugin_config.resolve_server_config(name, entry)
        if force and not cfg.enabled:
            cfg.enabled = True
        conn = await pool.add_server(cfg)
        conns[name] = conn
    return pool, conns


class _NoSuchServer(Exception):
    pass


# ── list ────────────────────────────────────────────────────────────────


async def _list_cmd(server: str | None) -> int:
    try:
        pool, conns = await _make_pool(server, force=False)
    except _NoSuchServer:
        return 1
    try:
        raw = plugin_config.load_config()
        servers = plugin_config._servers_dict(raw)
        names = [server] if server else list(conns)
        for name in names:
            entry = servers.get(name, {})
            if isinstance(entry, dict) and entry.get("enabled") is False:
                print(f"{name}: disabled (not connected)")
                continue
            conn = conns[name]
            if conn.status == ServerStatus.CONNECTED:
                tools = conn.list_tools()
                print(f"{name}: {len(tools)} tools")
                for tool in tools:
                    print(f"  {tool.get('name', tool)}")
            else:
                detail = f" — {conn.error}" if conn.error else ""
                print(f"{name}: {conn.status.value}{detail}")
        return 0
    finally:
        await pool.shutdown()


# ── test ────────────────────────────────────────────────────────────────


async def _test_cmd(server: str | None) -> int:
    try:
        pool, conns = await _make_pool(server, force=True)
    except _NoSuchServer:
        return 1
    try:
        names = [server] if server else list(conns)
        rc = 0
        for name in names:
            conn = conns[name]
            if conn.status == ServerStatus.CONNECTED:
                tools = conn.list_tools()
                print(f"[OK] {name}: connected, {len(tools)} tools")
            elif conn.status == ServerStatus.FAILED:
                detail = conn.error or "unknown error"
                print(f"[FAIL] {name}: {detail}")
                rc = 1
            else:
                print(f"[?] {name}: {conn.status.value}")
                rc = 1
        return rc
    finally:
        await pool.shutdown()


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
        return asyncio.run(_test_cmd(server))
    return 0


if __name__ == "__main__":
    sys.exit(main())