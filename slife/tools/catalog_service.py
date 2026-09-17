"""Tool catalog orchestration — policy over :class:`CatalogStore`.

The store is a dumb data layer; this service owns the semantics from
DESIGNER_NOTES §8.5: seed/reseed on session start, the effective-status
refusals for ``func-tool-load`` / ``_unload_func_tool``, the per-turn
injection snapshot (loaded ∧ whitelist), and main-agent-only curatorship.
Instances are per-process (main agent + subagent workers open the same
file); ``write_owner`` marks the single process allowed to run session
reseed and threshold eviction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from slife.tools.catalog import (
    STATUS_LOADED,
    STATUS_UNLOADED,
    TYPE_FUNC,
    CatalogStore,
    EFF_DISABLED,
    EFF_ERROR,
)
from slife.tools.whitelist import ALWAYS_LOADED, is_meta_tool

if TYPE_CHECKING:
    from slife.tools.base import Tool
    from slife.tools.semantic import SemanticManager

logger = logging.getLogger(__name__)

#: The job-coding plugin name — the plugin that exposes jobs as tools.
JOB_PLUGIN_NAME = "job-coding"

#: Tool-name prefix a job is exposed under (``translate`` → ``job-translate``).
#: Mirrors ``slife.plugins.job_coding.registry.JOB_TOOL_PREFIX`` — the plugin
#: owns the naming, this side reads it to tell a job row from the plugin's own
#: tool; ``tests/test_job_coding_plugin.py`` pins the two together.
JOB_TOOL_PREFIX = "job-"

#: job-coding's OWN tools.  They share the ``job-`` namespace with the jobs
#: themselves, so the prefix alone cannot separate the two — and a job may never
#: take one of these names (the plugin refuses them at ``job-write``).
JOB_PLUGIN_OWN_TOOLS = frozenset({"job-write", "job-remove", "job-list", "job-run"})


def plugin_category(plugin: str, tool_name: str) -> str:
    """Catalog category for a built-in plugin's tool.

    ``job`` = one function from the jobs directory (exposed by job-coding
    under the ``job-`` prefix); ``plugin`` = a built-in plugin's own tool,
    job-coding's four management tools included.  Both are function tools
    (``type='func'``) owned by the plugin named in ``source_id``.
    """
    if (plugin == JOB_PLUGIN_NAME
            and tool_name.startswith(JOB_TOOL_PREFIX)
            and tool_name not in JOB_PLUGIN_OWN_TOOLS):
        return "job"
    return "plugin"


def descriptor_json(name: str, description: str, input_schema) -> str:
    """Compact JSON of a tool descriptor — the catalog ``schema`` column.

    Strictly the tool def: ``name``, ``description`` and ``inputSchema`` (the
    arguments as a plain JSON Schema).  No other keys — the shape the loop
    re-serializes into an OpenAI function definition, keeping the stored
    schema and the injected one the same thing.
    """
    return json.dumps(
        {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def tool_descriptor(tool: "Tool") -> str:
    """Compact JSON of a tool's full descriptor (the catalog ``schema`` column)."""
    return descriptor_json(
        getattr(tool, "name", ""),
        getattr(tool, "description", ""),
        getattr(tool, "parameters", {"type": "object", "properties": {}}),
    )


def catalog_category(tool: "Tool") -> str:
    """Catalog category of a registered tool instance.

    - builtin tools → ``builtin``;
    - built-in plugin proxies (DIRECT/WRAPPER, bare names) → :func:`plugin_category`
      (``job`` for a job file's function, ``plugin`` for the plugin's own tool);
    - external MCP proxies → ``mcp`` (the reconcile corrects ``rest-api``
      from the server config's ``source.type``).
    """
    from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

    if isinstance(tool, MCPProxyTool):
        if getattr(tool, "_route", None) == ProxyRoute.EXTERNAL:
            return "mcp"
        return plugin_category(
            getattr(tool, "server", "") or "", getattr(tool, "name", "") or "",
        )
    return "builtin"


class ToolCatalogService:
    """Session-facing orchestration over the shared catalog store."""

    def __init__(
        self,
        store: CatalogStore,
        *,
        threshold: int = 100,
        write_owner: bool = True,
        autoload: tuple[str, ...] = (),
        autoload_servers: tuple[str, ...] = (),
        disabled_jobs: tuple[str, ...] = (),
        disabled_plugin: tuple[str, ...] = (),
    ):
        self._store = store
        self.threshold = threshold
        self.write_owner = write_owner
        #: Names marked ``autoload: true`` in their section entry — born
        #: loaded, never evicted.
        self.autoload = frozenset(autoload)
        #: Servers marked ``autoload: true`` — the same contract for tools
        #: whose names are unknown until their server connects.
        self.autoload_servers = frozenset(autoload_servers)
        #: Per-entry ``enabled: false`` names, by their own section: a job in
        #: ``job`` (the user's tools), a plugin's own tool in ``plugin``.  Both
        #: are registered whatever the config says, so the disable is mirrored
        #: onto the row instead.
        self._disabled_jobs = frozenset(disabled_jobs)
        self._disabled_plugin = frozenset(disabled_plugin)
        #: The host's semantic actor (set by AgentService after startup) —
        #: tool_search reads its embedder for hybrid retrieval.
        self.semantic_manager: "SemanticManager | None" = None

    @property
    def store(self) -> CatalogStore:
        return self._store

    def wake_indexer(self, changed: list[str]) -> None:
        """A reconcile invalidated these rows' vectors — wake the drainer.

        Without this the catalog's semantic index only catches up on the MCP
        paths (which call ``on_saved`` themselves) or at the next boot: a
        skill / job / builtin schema change deletes the vectors and leaves the
        drainer parked, so ``tool_search`` reports ``semantic_ready`` while
        silently missing the tool.  A no-op before the manager exists (the
        boot seed), where the drainer's first pass covers it anyway.
        """
        if changed and self.semantic_manager is not None:
            self.semantic_manager.on_saved()

    # ── Session lifecycle ──────────────────────────────────────────

    def _row_for(self, tool: "Tool") -> dict:
        """The catalog row for one registered tool — the ONE row builder.

        The boot seed and a plugin's (re)connect both come through here, so a
        row's provenance (``category`` + ``source_id``), its schema and its
        ``enabled`` mirror cannot drift between the two paths.
        """
        name = _tool_name(tool)
        return {
            "name": name,
            "description": _own_description(tool),
            "category": catalog_category(tool),
            "source_id": _source_id(tool),
            "schema": tool_descriptor(tool),
            "enabled": self._row_enabled(tool),
            # Only a brand-new row sees this — reconcile applies status on
            # INSERT alone, which is exactly the keep-existing rule.
            "status": self.default_status(name),
        }

    def _row_enabled(self, tool: "Tool") -> bool | None:
        """The row's ``enabled``: config for a local tool, ``None`` for external.

        An external tool's availability is its own ``status`` (a down server),
        never a config flag.  A job and a plugin's own tool are registered
        whatever the config says, so their section's disable is mirrored here;
        a BUILTIN tool disabled in the config is never registered at all, so
        whatever reaches this method is enabled by definition.
        """
        if _is_external(tool):
            return None
        category = catalog_category(tool)
        if category == "job":
            return _tool_name(tool) not in self._disabled_jobs
        if category == "plugin":
            return _tool_name(tool) not in self._disabled_plugin
        return True

    async def sync_system_tools(
        self, tools: Sequence["Tool"], *, source: str = "",
    ) -> list[str]:
        """Mirror registered system tools into the catalog. Returns schema movers.

        One entry point for both writers: the boot seed (no ``source`` — the
        whole registry) and a plugin's (re)connect / rescan (``source=<plugin>``
        — that plugin's tools only).  A NEW row gets the session default:
        ``loaded`` for the always-loaded set (the whitelist — harness pair, meta
        surface, pinned — plus anything marked ``autoload``), ``unloaded`` for
        everything else: discovery alone never puts a tool into the injection
        set.  An EXISTING row keeps its state — this mirrors WHICH tools exist,
        never what the model decided to load.

        ``source`` scopes the call, and that scoping is the only difference: a
        row of that source whose tool is gone is REMOVED (the plugin dropped a
        job), so ``tool_search`` cannot return a tool that no longer exists.
        The unscoped call purges nothing — a name missing from the registry may
        be a builtin disabled in the config, or an external row whose server has
        not connected yet.

        Main-owner only (a subagent worker never syncs).
        """
        if not self.write_owner:
            return []
        rows = [self._row_for(t) for t in tools if _tool_name(t)]
        result = await self._store.reconcile(rows)
        changed = list(result["schema_changed"])
        if source:
            # Upsert-then-purge: after the upsert this source owns exactly its
            # incoming names plus whatever vanished.
            vanished = sorted(
                await self._store.names_for_sources([source])
                - {r["name"] for r in rows}
            )
            for name in vanished:
                await self._store.remove_tool(name)
            if vanished:
                logger.info(
                    "catalog_system_tools_purged source=%s tools=%d",
                    source, len(vanished),
                )
            changed += vanished
        self.wake_indexer(changed)
        return changed

    def default_status(self, name: str, *, server: str = "") -> str:
        """What a NEW catalog row starts as: autoload → loaded, else unloaded.

        ``server`` is the owning external server for a server-backed row — a
        server marked ``autoload: true`` has its whole tool set born loaded
        (its tool names are not knowable in the config).
        """
        if is_meta_tool(name) or name in self.autoload or server in self.autoload_servers:
            return STATUS_LOADED
        return STATUS_UNLOADED

    # ── Injection snapshot ─────────────────────────────────────────

    async def snapshot_loaded(self) -> frozenset[str]:
        """Injectable tool names this turn: effective-loaded ∪ always-loaded."""
        names = await self._store.loaded_names()
        return frozenset(names) | ALWAYS_LOADED

    async def effective_status(self, name: str) -> str | None:
        """Effective status for one name (None if unknown)."""
        return await self._store.get_effective(name)

    # ── Non-function row mirror (skill / cli) ──────────────────────

    async def sync_category(self, category: str, rows: dict[str, dict]) -> list[str]:
        """Mirror a live source into one row-per-entry category.

        ``skill`` and ``cli`` are the two categories no registry feeds: their
        rows come from their own live sources (the skills dir; the ``cli``
        section of ``tools.json5``).  They are rows all the same — that is how
        ``tool_search`` reaches them — but they are not function tools, so
        they carry no load state (``status`` stays NULL).

        Upsert + purge, the same contract the plugin mirror follows: every
        entry is (re)written, and a name that vanished from the source loses
        its row — a deleted skill must not linger as a hit `tool_search` keeps
        returning.  Returns the purged names.
        """
        result = await self._store.reconcile(
            [
                {
                    "name": name,
                    "description": spec.get("description", ""),
                    "category": category,
                    "schema": spec.get("schema"),
                    "enabled": spec.get("enabled"),
                    "status": None,
                }
                for name, spec in rows.items()
            ],
            category=category,
            purge=True,
        )
        if result["purged"]:
            logger.info(
                "catalog_category_mirrored category=%s rows=%d purged=%d",
                category, len(rows), len(result["purged"]),
            )
        self.wake_indexer(result["schema_changed"])
        return result["purged"]

    # ── Load / unload (shared across main agent and workers) ───────

    async def load_tool(self, name: str) -> tuple[bool, str]:
        """Flip a function tool to ``loaded``; returns (ok, message).

        Refusal matrix uses the derived effective status — DISABLED
        (config) and ERROR (the tool's server is not up) each get their own
        hint.  The caller materializes an execution instance for
        server-backed tools (the proxy) after a successful flip.
        """
        row = await self._store.get_tool(name)
        if row is None:
            return False, (f"Error: tool '{name}' is unknown — see tool_search.")

        if row.get("type") != TYPE_FUNC:
            return False, (
                f"Error: tool '{name}' has no load/unload state "
                f"(type '{row.get('type')}')."
            )

        eff = await self._store.get_effective(name)
        if eff == EFF_DISABLED:
            return False, f"Error: tool '{name}' is disabled — enable it first."
        if eff == EFF_ERROR:
            server = row.get("source_id") or "?"
            return False, (
                f"Error: tool '{name}' is unavailable — server '{server}' is "
                f"not up right now (its tools are marked error). Check it with "
                f"mcp_list (or rest_api_list): it reconnects on its own, or "
                f"re-enable it with the matching *_set_enabled."
            )
        if eff == "loaded":
            return True, f"tool '{name}' is already loaded."

        await self._store.set_status(name, "loaded", bump=True)
        logger.info("catalog_tool_loaded name=%s", name)
        return True, f"[OK] Loaded '{name}'."

    async def touch(self, name: str) -> None:
        """Record a tool use — the LRU recency the eviction policy orders by.

        Write-owner only: the worker/seeding subagent never evicts, so the
        main agent alone feeds the order.  The registry calls this after a
        successful ``execute`` so a just-used tool is never the first eviction
        victim (the pre-fix behavior evicted the alphabetically-first builtin tools).
        """
        if not self.write_owner:
            return
        await self._store.touch(name)

    async def unload_tool(self, name: str) -> tuple[bool, str]:
        """Flip a function tool to ``unloaded``; refuses whitelisted/skill/cli."""
        if is_meta_tool(name):
            return False, (
                f"Error: cannot unload '{name}' — it is whitelisted "
                f"(always loaded, not configurable)."
            )
        row = await self._store.get_tool(name)
        if row is None:
            return False, f"Error: tool '{name}' is unknown."
        if row.get("type") != TYPE_FUNC:
            return False, (
                f"Error: tool '{name}' has no load/unload state "
                f"(type '{row.get('type')}')."
            )
        if row.get("status") != "loaded":
            return True, f"tool '{name}' is already unloaded."
        await self._store.set_status(name, "unloaded")
        logger.info("catalog_tool_unloaded name=%s", name)
        return True, f"[OK] Unloaded '{name}'."

    # ── Threshold eviction (main-owner policy) ─────────────────────

    async def evict_to_threshold(self) -> list[str]:
        """Evict oldest-by-``last_loaded`` loaded tools down to the threshold.

        Never evicts the always-loaded carve-outs (``ALWAYS_LOADED``: harness
        pairs + meta surface + pinned) or non-function (skill/cli) rows.
        Main-owner only — a subagent worker inherits the curator's budget
        and never squeezes it (a worker's turn stays within what the agent
        left loaded).
        """
        if not self.write_owner:
            return []
        count = await self._store.count_loaded()
        excess = count - self.threshold
        if excess <= 0:
            return []
        # Protected = the always-loaded carve-outs ∪ the autoload set: the
        # names marked ``autoload: true`` in their section entry, plus every
        # tool of a server marked the same way.
        protected = set(ALWAYS_LOADED) | set(self.autoload)
        if self.autoload_servers:
            protected |= await self._store.names_for_sources(self.autoload_servers)
        evicted = await self._store.evict_lru(excess, protected=protected)
        if evicted:
            logger.info(
                "catalog_evict_to_threshold threshold=%d evicted=%d/%d",
                self.threshold, len(evicted), excess,
            )
        return evicted

    # ── Reconcile support (per-server/plugin sync helpers) ─────────

    async def upsert_external_tool(
        self,
        name: str,
        *,
        server: str,
        description: str,
        schema: str,
        category: str = "mcp",
    ) -> bool:
        """Upsert a server-backed tool row (schema-change detection → re-embed).

        A newly seen tool lands ``unloaded`` — unless its server is marked
        ``preload: true``, which seeds the whole set loaded — while an existing
        row keeps whatever the model decided.  For everything else
        ``func-tool-load`` is the only way into the injection set.
        """
        return await self._store.upsert_tool(
            name,
            description=description,
            category=category,
            source_id=server,
            schema=schema,
            enabled=None,
            status=self.default_status(name, server=server),
        )

    async def mark_source_error(self, source: str) -> int:
        """Mark one server's tools ``error`` — the server is unusable.

        The single verdict for every unavailable case: not yet connected at
        startup, disconnected, a failed connect, or a dead gateway child.
        ``error`` is a state of its own, so the row still says the tool
        belongs to a server that is simply not up.
        """
        if not self.write_owner:
            return 0
        return await self._store.mark_source_error(source)

    async def mark_all_external_error(self) -> int:
        """Mark EVERY external tool ``error`` — nothing is live right now.

        Used both at catalog init (no server has connected yet) and when the
        gateway child dies (all of its servers are unreachable at once).
        """
        if not self.write_owner:
            return 0
        return await self._store.mark_all_external_error()

    async def mark_server_connected(self, source: str) -> int:
        """A server (re)connected — its ``error`` tools become ``unloaded``.

        Only ``error`` rows move: a tool the user had loaded keeps that state
        across a reconnect, because the reconnect clears the error mark
        rather than resetting the whole server.
        """
        if not self.write_owner:
            return 0
        return await self._store.reset_source_status(source, STATUS_UNLOADED)

    async def mark_plugin_connected(self, plugin: str) -> int:
        """A plugin (re)connected — its ``error`` rows return to their default.

        The per-row default is the one difference from
        :meth:`mark_server_connected`: a server's tools share one status, while
        each plugin tool carries its own (whitelisted / ``autoload`` → loaded,
        else unloaded).  Only ``error`` rows move, so a tool the model had
        loaded keeps that state across a plugin restart.
        """
        if not self.write_owner or not plugin:
            return 0
        names = await self._store.names_for_sources([plugin])
        if not names:
            return 0
        return await self._store.reset_source_error_rows(
            plugin, {n: self.default_status(n) for n in names},
        )

    async def purge_source(self, source: str) -> int:
        """Drop every row of a server or plugin that is gone (main-owner only).

        ``source`` is the owner name — the server for ``mcp``/``rest-api``, the
        plugin for ``plugin``/``job`` rows.  A plugin that never started owns no
        rows, so this is how a skipped or removed plugin's tools stop being
        offered by ``tool_search``.
        """
        if not self.write_owner:
            return 0
        return await self._store.purge_source(source)

    async def purge_unconfigured_sources(self, configured: "set[str]") -> "set[str]":
        """Purge the rows of every server NOT in *configured* (main-owner only).

        The mirror image of the removed ``sync_config_servers``: tools.json5
        is still the authority, but it is now compared against the servers
        that actually OWN tool rows instead of against a server table.
        """
        if not self.write_owner:
            return set()
        purged = await self._store.purge_missing_sources(configured)
        if purged:
            logger.info("catalog_purged_config_removed servers=%r", sorted(purged))
        return purged


def _tool_name(tool: "Tool") -> str:
    return getattr(tool, "name", "") or ""


def _is_external(tool: "Tool") -> bool:
    from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

    return isinstance(tool, MCPProxyTool) and getattr(tool, "_route", None) == ProxyRoute.EXTERNAL


def _own_description(tool: "Tool") -> str:
    """A tool's own description, without the ``[<owner>] `` prefix a proxy stamps.

    Provenance is already the row's ``source_id`` and ``category``; leaving the
    prefix on would put it in front of the model (the injected schema *is* this
    column) and in every search hit.
    """
    desc = getattr(tool, "description", "") or ""
    source = _source_id(tool)
    if source:
        prefix = f"[{source}] "
        if desc.startswith(prefix):
            return desc[len(prefix):]
    return desc


def _source_id(tool: "Tool") -> str | None:
    """The owning source of a tool row — a server name, a plugin name, or None.

    Every ``MCPProxyTool`` carries the name it came from in ``server``: an
    external server for ``mcp``/``rest-api`` rows, the plugin itself for a
    built-in plugin's bare-named tool.  ``None`` = the row is the harness's
    own (a builtin module tool, or a skill/cli mirror row).
    """
    from slife.mcp.tool_adapter import MCPProxyTool

    if not isinstance(tool, MCPProxyTool):
        return None
    return getattr(tool, "server", "") or None


async def mirror_source_rows(ctx, category: str, rows: dict) -> None:
    """Mirror a live source row-set into one catalog category (no catalog → no-op).

    The single wrapper the skill and cli mirrors both used to spell out: pull
    ``catalog`` from ``ctx``, no-op when absent, ``sync_category`` wrapped in
    a never-raise debug log.  Best-effort by contract — a failing mirror
    never breaks the tool that called us.
    """
    catalog = getattr(ctx, "catalog", None) if ctx is not None else None
    if catalog is None:
        return
    try:
        await catalog.sync_category(category, rows)
    except Exception as e:
        logger.debug("catalog_mirror_failed category=%s err=%s", category, e)