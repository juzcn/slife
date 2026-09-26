"""mcp-gateway wrapper server — FastMCP server with MCP connection management tools.

This is the entry point for the mcp-gateway child process. It:
  1. Starts a FastMCP server on Streamable HTTP transport (auto-assigned port)
  2. Exposes management tools (bare names) to manage external MCP connections
  3. Maintains persistent connections to external MCP servers
  4. Self-hosts its config: loads ``tools.yaml`` on startup and
     persists ``mcp_set`` / ``mcp_remove`` / ``mcp_set_enabled`` through
     ``mcp_gateway.config`` — no host involvement.

Spawned by Slife (or any host) via ``python -m slife.plugins.mcp_gateway.server``.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastmcp.server.context import Context

from slife.plugins.mcp_gateway import config as plugin_config
from slife.plugins.spec import mcp_child_reserved_names
from slife.plugins.mcp_gateway.connection import ConnectionPool, ServerConfig
from slife.logfmt import error_json, ok_json
from slife.server_utils import (
    ToolsChangedNotifier,
    create_plugin_server,
    request_tools_changed,
    tools_changed_bus,
)


#: Set when the boot pass has brought every configured server up (or failed to).
#: Published in ``__check`` because it is the fact that tells a server whose
#: spawn is still in flight from one that failed to come up: both are a pool row
#: with no transport.  The host's sync waits on it rather than judging either.
_spawn_settled = asyncio.Event()

#: Fire-and-forget tasks — the boot connect pass, a post-enable re-read.  Held
#: until they finish: the loop keeps tasks in a WeakSet only, so a bare
#: ``create_task``/``ensure_future`` result awaiting its first I/O can be
#: garbage-collected mid-flight and never complete (asyncio's own caveat).
_background_tasks: set[asyncio.Task] = set()


def _background(coro, *, name: str) -> None:
    """Run *coro* in the background, holding a reference until it is done."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@asynccontextmanager
async def _mcp_lifespan(_app):
    """Self-host config; release all external MCP connections on shutdown.

    The lifespan schedules auto-connect and returns immediately, so the ready
    port signal (fired by the server runtime once the lifespan completes) is
    never blocked by a slow external server.  Runs on the server's event loop
    (uvicorn lifespan), so the pool's async HTTP/SSE clients, stdio processes
    and health-monitor tasks are closed on the same loop that created them —
    otherwise connections leak on exit.
    """
    _background(_auto_connect_configured(), name="mcp-auto-connect")
    try:
        yield
    finally:
        # No cancel of the background work here: there is no session state to
        # lose, and a straggler spawn dies with the process tree anyway (the
        # wrapper puts every child in a kill-on-close job object).  Holding the
        # reference above is about the task not vanishing mid-flight, nothing
        # about teardown ordering.
        try:
            await _pool.shutdown()
        except Exception as e:
            logger.debug("mcp_pool_shutdown_error err=%s", e)


async def _auto_connect_configured() -> None:
    """Register every configured server, CONNECTING the enabled ones.

    ``tools.yaml`` is the whole decision: enabled ⇒ bring it up now, disabled
    ⇒ register it (so ``mcp_list`` lists the same set as the config) and leave
    it down.  Nothing is remembered between sessions — no db, no snapshot: a
    server that was down when you quit is retried at the next boot like any
    other, which is exactly what the pre-catalog gateway did.

    Best-effort and fire-and-forget from the lifespan — a slow server must
    never delay the ready port signal.
    """
    try:
        try:
            raw = plugin_config.load_config()
        except Exception as e:
            logger.warning("mcp_config_load_failed err=%s", e)
            return
        # servers live in the mcp.servers / rest-api sections (tools.yaml) —
        # use the merged view with the legacy top-level fallback, never the
        # raw ``servers`` key (gone since the section restructure).
        servers = plugin_config._servers_dict(raw)
        configured = [
            (name, entry) for name, entry in servers.items()
            if isinstance(entry, dict)
        ]
        logger.info("mcp_configured count=%d (connecting the enabled ones)", len(configured))

        # Which of these are REST APIs — the SECTION decides (one read, not one
        # per server), and it rides into the pool so ``__check`` can report the
        # category without any tag being written into the entry itself.
        rest_api_names = plugin_config.rest_api_names()

        async def _register_one(name: str, entry: dict) -> None:
            try:
                cfg = plugin_config.resolve_server_config(
                    name, entry, rest_api=name in rest_api_names,
                )
                if not cfg.enabled:
                    await _pool.add_server(cfg, connect=False)
                    return
                # Spawn-only: boot brings the transport up and leaves the tool LIST
                # to the first reader (the host's reconcile asks every server for
                # one as it mirrors the catalog).  Reading it here made boot the
                # sum of every server's tools/list — a slow peer delayed the whole
                # set, for a list no one had asked for yet.
                await _pool.add_server(cfg, read_tools=False)
                # NO usability verdict here.  "Usable" means "its ``tools/list``
                # succeeds" — that is this module's whole premise (health is a tool
                # list, not a connection) — and this pass deliberately does not
                # read one: the list belongs to the first reader, the host's
                # reconcile.  A spawn-only boot therefore has nothing to judge
                # with, and the old ``if not conn.tools_ok`` warned for EVERY
                # configured server (nobody had asked yet) with a message about a
                # connect the modern era does not have.  A transport that really
                # failed says so where it failed (``mcp_connect_failed``, carrying
                # the error), and the verdict the host acts on arrives from
                # ``__check`` once a read has run.
                #
                # Nor does it nudge per server: the host's sync starts when the
                # WHOLE boot pass is done (one nudge after the gather), because a
                # pass woken mid-spawn would judge servers whose transports are
                # still being established.
            except Exception as e:
                logger.warning("mcp_server_setup_failed server=%s err=%s", name, e)

        await asyncio.gather(*(_register_one(n, e) for n, e in configured))
    finally:
        # The boot pass is over on EVERY exit, the failed ones included — a
        # tools.yaml that cannot be read made its (zero) transport attempts and
        # is done just the same.  Settling only on the happy path left every
        # configured server reading "pending" until the host's
        # ready.tool_sync_wait budget ran out, for a pass that had already
        # finished.  Published first, then nudged — the host's sync must see the
        # settled fact when it wakes.
        _spawn_settled.set()
        _request_tools_changed()


mcp, _log_path, logger = create_plugin_server(
    "mcp-gateway",
    instructions=(
        "mcp-gateway is a gateway that manages connections to external MCP "
        "servers. Use the management tools to add/remove servers, discover "
        "tools, and call tools on connected servers."
    ),
    lifespan=_mcp_lifespan,
)


# ── Global state ─────────────────────────────────────────────────────

#: Fan-out of ``tools/list_changed`` to this server's listen subscribers
#: (:class:`slife.server_utils.ToolsChangedNotifier`).  No session
#: bookkeeping: the modern era delivers change events only to streams a
#: client opened with ``subscriptions/listen``, so publishing on the
#: server's bus reaches all of them — from a request handler or a
#: background task alike.  ``tools_changed_bus`` also registers the listen
#: handler (fastmcp does not).
_notifier = ToolsChangedNotifier(tools_changed_bus(mcp))


def _request_tools_changed() -> None:
    """Publish ``tools/list_changed`` to the listen subscribers.

    Fire-and-forget — see :func:`slife.server_utils.request_tools_changed`.
    """
    request_tools_changed(_notifier)


# ── Connection → host notification ──────────────────────────────────────
# The wrapper owns the CONNECTION, not a catalog: unified tool discovery /
# search / load lives in the shared host ``tools.db``
# (``slife.tools.catalog`` — the single catalog per DESIGN.md §4.3).
# Whenever a server's tool surface may have changed — a list read, a
# ``tools/list_changed`` event, a dead transport — the host is told to
# re-list, and reconciles server runtime + tool rows from ``__mcp_list_tools`` /
# ``__check`` itself.

async def _on_tools_changed(server_name: str) -> None:
    """A server's tool list may have changed — tell the host to re-sync."""
    _request_tools_changed()


_pool = ConnectionPool(on_tools_changed=_on_tools_changed)

# Built-in Slife plugin server names — reserved: an external MCP server must
# not take one of these, or its tools would collide / misroute in the host's
# namespace.  Derived from the central plugin contract so every built-in
# plugin (job-coding included) is always covered.
_RESERVED_SERVER_NAMES = mcp_child_reserved_names()

# ═══════════════════════════════════════════════════════════════════════
# Management tools
# ═══════════════════════════════════════════════════════════════════════


# ── Config comparison for idempotency ──────────────────────────────

def _server_config_equal(a: ServerConfig, b: ServerConfig) -> bool:
    """Compare two ServerConfigs for equality.

    ``description``/``source`` are deliberately ignored — metadata, not part
    of the connection definition — so a change must not trigger a spurious
    restart.
    """
    return (
        a.name == b.name
        and a.command == b.command
        and a.args == b.args
        and a.env == b.env
        and a.url == b.url
        and a.headers == b.headers
        and a.enabled == b.enabled
        and a.auth == b.auth
        and a.os_paths == b.os_paths
    )


def _persist_entry(
    name: str,
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
    url: str,
    headers: dict[str, str] | None,
    description: str,
    source: dict | None,
    auth: dict | None,
    enabled: bool = True,
) -> None:
    """Persist a server entry to tools.yaml (merge semantics).

    ``enabled=True`` (the default) leaves the flag untouched — only
    ``mcp_set_enabled`` flips enable/disable; ``enabled=False`` is written
    so the server stays disconnected on the next wrapper start.

    Empty fields are not written: a stdio server has no ``url``, an http one
    may have no ``args``, and a field that says nothing is noise in a file
    people read and hand-edit (``url: ""`` also read as a claim that there is
    a URL).  ``add_server_entry`` already drops ``None``; this drops the
    empty containers and strings around it.

    The entry goes back into the section it already lives in
    (``server_section``): an upsert of a REST API belongs to ``rest-api``,
    and defaulting to ``mcp`` used to scatter a second copy under
    ``mcp.servers`` — where the enable/disable path would then find it first
    and flip the copy nothing reads.
    """
    entry: dict = {
        "command": command,
        "args": args,
        "env": env,
        "url": url,
        "headers": headers,
        "description": description,
        "source": source,
        "auth": auth,
    }
    if not enabled:
        entry["enabled"] = False
    entry = {
        key: value for key, value in entry.items()
        if value is not None and value != "" and value != [] and value != {}
    }
    plugin_config.add_server_entry(
        name, entry, section=plugin_config.server_section(name) or "mcp",
    )


# ═══════════════════════════════════════════════════════════════════════
# Family gate — the mcp_* tools own the MCP family, and only it
# ═══════════════════════════════════════════════════════════════════════

def _family_refusal(name: str, category: str, twin: str) -> str | None:
    """Refuse a call that speaks for *category* but names the other family.

    A REST API and an MCP server are different things with different tool
    sets, and the ``mcp_*`` tools own the MCP one — the pool holding both does
    not make ``github`` an MCP server any more than it makes ``playwright`` an
    API.  *twin* is the ``rest_api_*`` tool that does own the server, named in
    the refusal so the caller is sent somewhere real.

    Only the ``mcp`` direction is gated.  The internal registrations declare
    ``category="rest-api"`` because that is who calls them; they stay
    family-blind so the ``rest_api_*`` family can drive its own servers'
    lifecycle through them.
    """
    if category == "mcp" and plugin_config.is_rest_api(name):
        return error_json(
            f"'{name}' is a REST API, not an MCP server. Use {twin} instead.",
            server=name,
        )
    return None


def _disabled_refusal(server: str) -> str:
    """The one sentence refusing a switched-off server's surface.

    ``enabled: false`` means "stays configured but is not connected"
    (tools.yaml), and the tool that connects it again belongs to its own
    family — a REST API's caller is not sent to ``mcp_set_enabled``.  Both
    refusals (``__mcp_list_tools`` and ``__mcp_call_tool``) say this, so a
    switched-off server reads the same however it is approached.
    """
    enable_tool = (
        "rest_api_set_enabled" if plugin_config.is_rest_api(server)
        else "mcp_set_enabled"
    )
    return f"Server '{server}' is disabled — enable it with {enable_tool}."


async def _set_server(
    name: str,
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
    url: str,
    headers: dict[str, str] | None,
    description: str,
    enabled: bool,
    source: dict | None,
    auth: dict | None,
    category: str,
    ctx: Context | None,
) -> str:
    """The shared upsert behind ``mcp_set`` and ``__mcp_set``.

    ``category`` is the family the CALLER speaks for — the gate reads it, so
    the one rule lives here instead of once per registration (see
    :func:`_family_refusal`).

    The ENTRY's config section decides which family the connection is built
    as, which is why ``rest_api_set`` writes the config before calling: a name
    that resolves to ``rest-api`` connects as a REST API.
    """
    refusal = _family_refusal(name, category, "rest_api_set")
    if refusal:
        return refusal

    if not command and not url:
        return error_json(
            "Either 'command' (for stdio) or 'url' (for HTTP) must be provided.",
            server=name,
        )

    if name in _RESERVED_SERVER_NAMES:
        return error_json(
            f"Server name '{name}' is reserved by a built-in plugin. "
            f"Choose a different name.",
            server=name,
        )

    # os_paths / auto_load / source are config-file fields (not mcp_set
    # params) — preserve them across an upsert so a hand-edited flag isn't
    # silently reset on the running connection.
    existing = _pool.get_server(name)
    # Resolve ${VAR} refs in env/auth the SAME way resolve_server_config
    # does, so _server_config_equal compares resolved-vs-resolved.  The tool
    # takes raw refs (secrets must not come back through tool args); without
    # resolution every re-run of mcp_set with identical args compared
    # {"FOO": "${FOO}"} against the pool's resolved {"FOO": "secret"} and
    # never matched — tearing down + reconnecting (and re-running OAuth).
    resolved_env = None
    if env:
        resolved_env = {k: plugin_config._resolve_secret(str(v)) for k, v in env.items()}
    resolved_auth = None
    if auth:
        resolved_auth = dict(auth)
        for auth_key in ("client_id", "client_secret"):
            if auth_key in resolved_auth and isinstance(resolved_auth[auth_key], str):
                resolved_auth[auth_key] = plugin_config._resolve_secret(resolved_auth[auth_key])
    config = ServerConfig(
        name=name,
        command=command,
        args=args or [],
        env=resolved_env,
        url=url,
        headers=headers,
        description=description,
        enabled=enabled,
        auth=resolved_auth,
        os_paths=existing.config.os_paths if existing else False,
        auto_load=existing.config.auto_load if existing else False,
        source=(source if source is not None
                else (existing.config.source if existing else None)),
        # Same rule as the other derived fields: preserve what the entry's
        # placement says.  A server already in the pool keeps its answer; a new
        # one takes the section its name resolves to (mcp unless rest_api_set
        # wrote it there first, which it does before calling us).
        rest_api=(existing.config.rest_api if existing
                  else plugin_config.server_section(name) == "rest-api"),
    )

    try:
        if existing is not None and _server_config_equal(existing.config, config):
            if existing.tools_ok:
                # description/source are deliberately outside the comparison — a
                # metadata edit must not restart the transport — but that also
                # meant a metadata-ONLY edit was never written anywhere: mcp_set
                # answered "already_connected" and tools.yaml kept the old text
                # while the caller was told nothing had changed.  Persist it
                # here instead, leaving the live connection alone.
                #
                # An empty description is the tool's default, so it reads as
                # "not supplied" rather than "clear it" — otherwise every
                # idempotent re-run would blank a description it never mentioned.
                # source needs no such rule: the built config already resolved a
                # missing one to the existing value, so a difference is real.
                meta_changed = (
                    (description != "" and description != existing.config.description)
                    or config.source != existing.config.source
                )
                if meta_changed:
                    _persist_entry(
                        name, command, args, env, url, headers,
                        description, config.source, auth, enabled,
                    )
                tools = existing.list_tools()
                return ok_json(
                    status="already_connected",
                    server=name,
                    transport=config.transport,
                    tool_count=len(tools),
                    tools=[t["name"] for t in tools],
                    note=("Server config unchanged — metadata updated, no restart needed."
                          if meta_changed else
                          "Server config unchanged — no restart needed."),
                )

        conn = await _pool.add_server(config)
        _persist_entry(
            name, command, args, env, url, headers,
            description, source, auth, enabled,
        )

        if conn.tools_ok:
            tools = conn.list_tools()
            return ok_json(
                status="connected",
                server=name,
                transport=config.transport,
                tool_count=len(tools),
                tools=[t["name"] for t in tools],
            )
        elif not config.enabled:
            return ok_json(
                status="disabled",
                server=name,
                note="Server added to pool but not connected (enabled=false).",
            )
        else:
            return error_json(
                conn.error or "Unknown error",
                status="failed",
                server=name,
            )
    except Exception as e:
        logger.exception("mcp_set_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_set",
    description=(
        "Add or update an external MCP server connection (upsert; stdio via "
        "`command`/`args`, or http via `url`)."
    ),
)
async def mcp_set(
    name: str,
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str = "",
    headers: dict[str, str] | None = None,
    description: str = "",
    enabled: bool = True,
    source: dict | None = None,
    auth: dict | None = None,
    ctx: Context | None = None,
) -> str:
    """Add or update an MCP server (upsert — idempotent).

    The MCP family's own tool.  Identical config → ``already_connected``, no
    restart.  Changed config → restart.  ``enabled`` sets the initial state;
    use ``mcp_set_enabled`` to toggle enable/disable at runtime.  Persisted to
    tools.yaml.

    Args:
        name: Unique server name (not a reserved plugin name).
        command: For stdio servers — the binary (npx, uvx, python).
        args: For stdio servers — command-line arguments (list).
        env: Environment overrides. Use ${VAR} refs for secrets, never plaintext.
        url: For http servers — the SSE or streamable endpoint (auto-detected).
        headers: HTTP headers. Use ${VAR} refs for secrets, never plaintext.
        description: What the server does, in its own language — don't translate.
        enabled: Initial state: true connects now, false stays disconnected.
        source: Provenance (e.g. registry) for future updates.
        auth: OAuth config for device code flow (auth type 'oauth').
    """
    return await _set_server(
        name, command, args, env, url, headers, description, enabled,
        source, auth, category="mcp", ctx=ctx,
    )


@mcp.tool(
    name="__mcp_set",
    description=(
        "Add or update ANY server connection — REST APIs included. Internal: "
        "the rest_api_* family's warm-up calls this; the model gets the "
        "family-gated mcp_set."
    ),
)
async def __mcp_set(
    name: str,
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str = "",
    headers: dict[str, str] | None = None,
    description: str = "",
    enabled: bool = True,
    source: dict | None = None,
    auth: dict | None = None,
    ctx: Context | None = None,
) -> str:
    """The ``rest_api_*`` family's registration of the shared upsert."""
    return await _set_server(
        name, command, args, env, url, headers, description, enabled,
        source, auth, category="rest-api", ctx=ctx,
    )


@mcp.tool(
    name="mcp_set_enabled",
    description=(
        "Enable or disable an MCP server (enable connects it, disable "
        "disconnects it)."
    ),
)
async def mcp_set_enabled(name: str, enabled: bool, ctx: Context | None = None) -> str:
    """Toggle enable/disable on an existing MCP server.

    The MCP family's own tool.

    Args:
        name: Server name (from mcp_list).
        enabled: true = reconnect and load tools; false = disconnect and unload.
    """
    return await _set_server_enabled(name, enabled, category="mcp", ctx=ctx)


@mcp.tool(
    name="__mcp_set_enabled",
    description=(
        "Enable or disable ANY server — REST APIs included. Internal: the "
        "rest_api_* family calls this; the model gets the family-gated "
        "mcp_set_enabled."
    ),
)
async def __mcp_set_enabled(
    name: str, enabled: bool, ctx: Context | None = None,
) -> str:
    """The ``rest_api_*`` family's registration of the shared toggle."""
    return await _set_server_enabled(name, enabled, category="rest-api", ctx=ctx)


async def _set_server_enabled(
    name: str, enabled: bool, category: str, ctx: Context | None,
) -> str:
    """The shared toggle behind ``mcp_set_enabled`` and ``__mcp_set_enabled``.

    ``category`` is the family the caller speaks for (see
    :func:`_family_refusal`).
    """
    refusal = _family_refusal(name, category, "rest_api_set_enabled")
    if refusal:
        return refusal
    existing = _pool.get_server(name)
    if existing is None:
        return error_json(
            f"Server '{name}' not found. Use mcp_set to add it first.",
            server=name,
        )
    existing.config.enabled = enabled
    if enabled:
        # Persist the flag: enabled is the default, so this removes the
        # ``enabled: false`` a prior disable wrote — otherwise the re-enable
        # would be lost on the next restart (the server would load disabled).
        plugin_config.set_server_enabled(name, True)
        if existing.tools_ok:
            tools = existing.list_tools()
            return ok_json(
                status="connected",
                server=name,
                transport=existing.config.transport,
                tool_count=len(tools),
                tools=[t["name"] for t in tools],
                note="Server enabled.",
            )
        # No working tool list — fan the (re)read out behind a fast response.
        # refresh_tools() owns its per-connection lock and, on success, fires
        # ``_on_tools_changed`` -> catalog sync + one coalesced list_changed
        # the host re-lists from.  Returning immediately keeps this control
        # call off the slow path that previously held the request open for
        # seconds while notification bursts interleaved with its cancel scope
        # (the mcp 2.1.1 dispatcher crash this module guards against).
        async def _connect_async() -> None:
            try:
                await existing.refresh_tools()
            except Exception as e:
                logger.warning("mcp_enable_connect_failed server=%s err=%s", name, e)

        _background(_connect_async(), name=f"mcp-connect:{name}")
        return ok_json(
            status="enabling",
            server=name,
            transport=existing.config.transport,
            tool_count=0,
            tools=[],
            note="Server enabling — tools register when it connects.",
        )
    await _pool.disconnect_server(name)
    plugin_config.set_server_enabled(name, False)
    # Notify so the host reconcile drops this server's loaded proxies.
    _request_tools_changed()
    return ok_json(
        status="disabled",
        server=name,
        note="Server disabled. Re-enable with mcp_set_enabled(name=..., enabled=true).",
    )


@mcp.tool(
    name="mcp_remove",
    description=(
        "Remove an MCP server: stops it and deletes its config entry."
    ),
)
async def mcp_remove(name: str, ctx: Context | None = None) -> str:
    """Stop and remove an MCP server.

    The MCP family's own tool.

    Args:
        name: Server name to remove.
    """
    return await _remove_server(name, category="mcp", ctx=ctx)


@mcp.tool(
    name="__mcp_remove",
    description=(
        "Stop and remove ANY server — REST APIs included. Internal: the "
        "rest_api_* family calls this; the model gets the family-gated "
        "mcp_remove."
    ),
)
async def __mcp_remove(name: str, ctx: Context | None = None) -> str:
    """The ``rest_api_*`` family's registration of the shared removal."""
    return await _remove_server(name, category="rest-api", ctx=ctx)


async def _remove_server(name: str, category: str, ctx: Context | None) -> str:
    """The shared removal behind ``mcp_remove`` and ``__mcp_remove``.

    ``category`` is the family the caller speaks for (see
    :func:`_family_refusal`).
    """
    refusal = _family_refusal(name, category, "rest_api_remove")
    if refusal:
        return refusal
    try:
        await _pool.remove_server(name)
        plugin_config.remove_server_entry(name)
        _request_tools_changed()
        return ok_json(status="removed", server=name)
    except Exception as e:
        logger.exception("mcp_remove_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_list",
    description=(
        "List configured MCP servers (transport, command/url, enabled). "
        "REST APIs are a separate family — rest_api_list."
    ),
)
async def mcp_list(ctx: Context | None = None) -> str:
    """List configured external MCP servers (static config view).

    Its OWN family only.  A REST API is a different thing from an MCP server
    — an API described by an OpenAPI document, which this gateway happens to
    serve through an ``mcp-openapi-proxy`` process — and it is managed by the
    ``rest_api_*`` tools.  ``rest_api_list`` is its listing; returning it here
    reported servers the ``mcp_*`` tools do not own, indistinguishable from
    the ones they do.
    """
    servers = [s for s in _pool.list_configured() if not s.get("rest_api")]
    return json.dumps(servers, ensure_ascii=False, indent=2)


@mcp.tool(
    name="__mcp_list",
    description=(
        "List configured servers of BOTH families — the host's catalog "
        "reconcile enumerates with this. Internal: the model gets "
        "mcp_list / rest_api_list, each scoped to its own family."
    ),
)
async def __mcp_list(ctx: Context | None = None) -> str:
    """The unfiltered config view — both families, one list.

    ``mcp_list`` and ``rest_api_list`` are each scoped to their own family for
    the model.  The host's reconcile is not a family: it mirrors every
    configured server's rows into the catalog, so pointing it at the filtered
    listing silently stopped it mirroring the REST APIs — their rows vanished
    from the catalog and neither ``tool_search`` nor ``func_tool_load`` could
    reach them.
    """
    return json.dumps(_pool.list_configured(), ensure_ascii=False, indent=2)


@mcp.tool(
    name="__check",
    description=(
        "Per-server tool-list facts: tools_ok, tool_count, tools_age_s, "
        "last_error, reachable; plus spawn_settled for the boot pass. "
        "Internal — probed by the harness's system_health."
    ),
)
async def __check(ctx: Context | None = None) -> str:
    """Report per-server tool-list facts.

    Returns ``{"servers": [...], "spawn_settled": bool}``.  Authoritative for
    server health, and deliberately fact-only — no state word, no level: the
    harness interprets (DESIGN.md §9.3).
    ``tools_ok`` is the verdict to read: a ``tools/list`` succeeded and its
    result is still held, which is also what makes this server's tools usable
    in the catalog.  ``tools_age_s`` is the age of the list being served and
    ``last_error`` the reason the last read failed, if it did.
    ``reachable`` is whether that peer's transport is up — the fact that
    separates "never listed yet" from "could not come up" while both are
    ``tools_ok`` false.  Catalog semantic-index status is reported host-side
    (the host owns the shared catalog's SemanticManager).

    ``spawn_settled`` says the boot pass has finished bringing every configured
    server up (or failed to): until it is true, a server with no transport is
    one nobody has asked yet, not one that is down.

    Never connects: probing must not be what brings a server up, so a
    re-read is the background repair's job and the numbers here are whatever
    the last real read left behind."""
    servers = _pool.list_servers()
    return json.dumps(
        {"servers": servers, "spawn_settled": _spawn_settled.is_set()},
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="__mcp_list_tools",
    description=(
        "A server's tool list (uncapped by default). Internal — the host's "
        "catalog sync writes a row per tool."
    ),
)
async def __mcp_list_tools(
    server: str, limit: int = 0, ctx: Context | None = None,
) -> str:
    """The listing itself, uncapped unless asked otherwise — host-side only.

    The ONE implementation.  ``mcp_list_tools`` and the REST-API family's
    ``rest_api_list_tools`` call this with the configured cap; the catalog
    sync calls it with none, because it needs every tool (a trimmed listing
    would silently drop the rest from the catalog).  One read, one cap, one
    place that can arm the background repair.

    Reading IS the liveness check: no tool list means the server is absent
    from the catalog, so this is the place worth paying a ``tools/list`` for.
    A list already held and still inside the peer's cache window is served
    as-is — the peer's own change events are what mark it stale, so an
    unchanged server costs nothing here.

    ``limit`` is how many tools are shown (0 = every one).  ``tool_count``
    always reports the server's real total, so a capped answer still says how
    much is behind it, and the trimmed tail is replaced by the one instruction
    that finds a specific tool.  Internal: the ``__`` prefix keeps this out of
    the model's tool set (``is_internal_tool``).

    Args:
        server: Server name (from mcp_list).
        limit: Max tools to list (0 = every tool).
    """
    # 0 (the tool default) means "no cap" — None here, so the trim below is a
    # single comparison rather than a second sentinel to keep in step.
    cap: int | None = limit if limit > 0 else None
    conn = _pool.get_server(server)
    if conn is None:
        return ok_json(
            server=server,
            connected=False,
            tools=[],
            tool_count=0,
            note=(
                f"Server '{server}' is not configured — use mcp_list to see "
                "the configured servers."
            ),
        )
    # A switched-off server is never read, because reading IS connecting
    # (``refresh_tools`` → ``ensure_session``).  The gate sits ahead of the
    # first read so no caller can spawn one by asking — the host's reconcile
    # included, which is what used to bring every disabled server up just to
    # mirror its rows.
    if not conn.config.enabled:
        return error_json(_disabled_refusal(server), server=server)
    if not conn.has_tools():
        await conn.refresh_tools()
    if not conn.has_tools():
        return ok_json(
            server=server,
            connected=False,
            tools=[],
            tool_count=0,
            note=(
                f"Server '{server}' returned no tool list — "
                f"{conn.error or 'unreachable'}. The wrapper retries in the "
                "background. Use mcp_list to see configured servers."
            ),
        )

    try:
        live = _pool.list_all_tools(server_name=server)
    except Exception as e:
        logger.warning("mcp_list_tools_live_failed server=%s err=%s", server, e)
        return error_json(
            f"MCP unavailable for server '{server}' — live tool read failed: {e}",
            server=server,
        )

    truncated = cap is not None and cap < len(live)
    shown = live[:cap] if truncated else live
    return ok_json(
        server=server,
        connected=True,
        source="live",
        tools=shown,
        tool_count=len(live),
        truncated=truncated,
        note=(
            f"Showing {len(shown)} of {len(live)} tools from the live MCP "
            "tools/list — use tool_search to find a specific one."
            if truncated else
            "Live tool list from the connected server (built-in MCP tools/list)."
        ),
    )


@mcp.tool(
    name="mcp_list_tools",
    description=(
        "List a server's tools (full_name server__tool). Capped at "
        "mcp.tool_list_limit — use tool_search to find a specific one. "
        "Use mcp_list to discover server names."
    ),
)
async def mcp_list_tools(server: str, limit: int = 0, ctx: Context | None = None) -> str:
    """List a server's tools, capped for the caller's context.

    ``__mcp_list_tools`` with the configured cap — the SAME read and the SAME
    cap code; this tool differs only in what it passes and who it is for.  The
    shared catalog is fed by the HOST (the agent's reconcile reads the
    internal twin), so there is no wrapper-side catalog branch here.

    The cap is the point of this tool: a published server can carry four
    figures of tools (github: 1239), and printing them all spends the model's
    context on names it never asked for.

    Args:
        server: Server name (from mcp_list).
        limit: Max tools to list (0 = the configured cap, ``mcp.tool_list_limit``).
    """
    # No twin registration needed: ``rest_api_list_tools`` already reads the
    # uncapped ``__mcp_list_tools`` (its own cap, its own family), so this tool
    # has no internal caller to keep working past the gate.
    refusal = _family_refusal(server, "mcp", "rest_api_list_tools")
    if refusal:
        return refusal
    cap = plugin_config.tool_list_limit() if limit <= 0 else limit
    return await __mcp_list_tools(server, limit=cap)


@mcp.tool(
    name="__mcp_call_tool",
    description=(
        "Call a tool on a connected MCP server (internal — invoked by the "
        "server__tool proxies, not directly by the agent). "
        "arguments = JSON object string."
    ),
)
async def __mcp_call_tool(
    server: str,
    tool_name: str,
    arguments: str = "{}",
    ctx: Context | None = None,
) -> str:
    """Call a tool on a connected MCP server.

    Args:
        server: Server name.
        tool_name: Tool name (without server prefix).
        arguments: JSON string of tool arguments (e.g. '{"path": "/tmp"}').
    """
    try:
        args_dict = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args_dict, dict):
            args_dict = {}
    except json.JSONDecodeError:
        return f"Error: arguments must be valid JSON. Got: {arguments}"

    # Call-time enforcement (per-mcp): a disabled server refuses the call.
    conn = _pool.get_server(server)
    if conn is not None and not conn.config.enabled:
        return error_json(
            _disabled_refusal(server), server=server, tool=tool_name,
        )

    result = await _pool.call_tool(server, tool_name, args_dict)
    return result


# ═══════════════════════════════════════════════════════════════════════
# Server lifecycle + discovery tools
# ═══════════════════════════════════════════════════════════════════════


# ── Entry point ──────────────────────────────────────────────────────


def main():
    """Run the mcp-gateway wrapper server on Streamable HTTP transport."""
    import argparse

    from slife.server_utils import run_plugin_server, shutdown_server_logging

    parser = argparse.ArgumentParser(prog="mcp-gateway-server")
    parser.add_argument(
        "--port", type=int, default=0,
        help="Port to serve on (default: auto-assign a free port).",
    )
    args = parser.parse_args()

    logger.info("mcp_start log=%s pid=%s", _log_path, os.getpid())
    try:
        run_plugin_server(mcp, port=args.port)
    finally:
        logger.info("mcp_stop log=%s pid=%s", _log_path, os.getpid())
        shutdown_server_logging()


if __name__ == "__main__":
    main()