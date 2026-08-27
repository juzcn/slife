"""mcp-plugin CLI — configure and test external MCP servers.

Commands:
  ``mcp-plugin``               overview of configured servers
  ``mcp-plugin set <s>``       interactively add/configure a server
  ``mcp-plugin remove <s>``    remove a server (takes effect next server start)
  ``mcp-plugin test [--port N]``  start the plugin server and verify it serves MCP
  ``mcp-plugin test mcp <s>``     bare-connect to one server (no framework) and list its tools

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
from mcp_plugin.client import MCPClient
from mcp_plugin.config import _is_env_ref, _resolve_embedded_refs, _resolve_secret
from mcp_plugin.connection import ServerConfig
from mcp_plugin.platform import kill_process_tree, resolve_command

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

    p_test = sub.add_parser(
        "test", help="Start the plugin server and verify it, or bare-check one MCP server."
    )
    p_test.add_argument(
        "--port", type=int, default=0,
        help="Port for the plugin server (default: auto-assign a free port).",
    )
    test_sub = p_test.add_subparsers(dest="test_command")
    p_test_mcp = test_sub.add_parser(
        "mcp", help="Bare-connect to a server (no framework) and list its tools."
    )
    p_test_mcp.add_argument("server", help="MCP server name.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    command = args.command
    if command is None:
        return _overview()
    if command == "set":
        return _set_cmd(args.server)
    if command == "remove":
        return _remove_cmd(args.server)
    if command == "test":
        return asyncio.run(_test_cmd(args))
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


# ── test ────────────────────────────────────────────────────────────────


async def _test_cmd(args: argparse.Namespace) -> int:
    if args.test_command == "mcp":
        return await _test_mcp_cmd(args.server)
    return await _test_plugin_cmd(port=args.port)


async def _test_plugin_cmd(*, port: int = 0) -> int:
    """Start the REAL plugin server and verify it serves MCP end-to-end.

    Spawns ``python -m mcp_plugin.server`` (the same entry Slife launches),
    reads its ``{"port": N}`` ready signal, connects over Streamable HTTP,
    and reports the external servers the plugin auto-connected.  This drives
    the plugin's actual startup path — not a re-implementation of it.
    """
    import json

    cmd = [sys.executable, "-m", "mcp_plugin.server"]
    if port:
        cmd += ["--port", str(port)]
    print(f"plugin startup: spawning {' '.join(cmd)} ...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    client: MCPClient | None = None
    try:
        stdout = proc.stdout
        if stdout is None:
            print("[FAIL] plugin server produced no stdout stream")
            return 1
        try:
            line = await asyncio.wait_for(stdout.readline(), timeout=30.0)
            serving_port = int(json.loads(line.decode("utf-8").strip())["port"])
        except Exception as e:  # noqa: BLE001 - any failure to read the port
            print(f"[FAIL] plugin server did not signal a port: {e}")
            return 1

        url = f"http://127.0.0.1:{serving_port}/mcp"
        print(f"plugin server: ready on port {serving_port}")
        client = MCPClient()
        try:
            await client.connect(url)
        except Exception as e:  # noqa: BLE001 - report connect failure
            print(f"[FAIL] plugin MCP connect failed: {type(e).__name__}: {e}")
            return 1

        tools = await client.list_tools()
        mgmt = sorted({t.get("name", "") for t in tools} & _MANAGEMENT_TOOLS)
        print(f"[OK] plugin connected: {len(tools)} tools, "
              f"{len(mgmt)} management tools ({', '.join(mgmt)})")

        servers = await _plugin_servers_report(client)
        if not servers:
            print("  (no external servers reported)")
        for name, info in servers.items():
            if info.get("state") == "running":
                print(f"  [OK] {name}: running, {info.get('tool_count', 0)} tools")
            else:
                err = f" — {info.get('error')}" if info.get("error") else ""
                print(f"  [--] {name}: {info.get('status', 'stopped')}{err}")
        return 0
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass
        # Kill the plugin AND its spawned external servers (orphan-free).
        await kill_process_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()


_MANAGEMENT_TOOLS = frozenset({
    "mcp_list", "mcp_set", "mcp_remove", "mcp_set_enabled",
    "mcp_list_tools", "__mcp_call_tool", "__mcp_connection_status",
})


async def _plugin_servers_report(client: MCPClient, timeout: float = 25.0) -> dict:
    """Poll the plugin's live connection status until servers settle (≤*timeout*).

    The plugin's lifespan auto-connects enabled servers concurrently and
    fire-and-forget (so the ready signal is never blocked), so the test waits
    up to *timeout* for them to reach a terminal state, mirroring how the
    agent discovers the plugin's connections.
    """
    import json

    deadline = asyncio.get_running_loop().time() + timeout
    report: dict = {}
    while True:
        raw = await client.call_tool("__mcp_connection_status", {})
        try:
            report = {s["name"]: s for s in json.loads(raw)}
        except json.JSONDecodeError:  # status not ready yet — tool returned "Error: …"
            pass
        pending = [
            s for s in report.values()
            if s.get("status") in ("connecting", "disconnected")
        ]
        if not pending or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.5)
    return report


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