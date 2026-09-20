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
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_LOADED,
    STATUS_NA,
    STATUS_UNLOADED,
    CatalogStore,
    config_status,
    is_function_category,
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
    (they carry a load state) owned by the plugin named in ``source_id``.
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


def status_error_refusal(name: str, action: str) -> str:
    """``Error: tool 'X' cannot be <action> — its status is error.``

    One line, stating the state and nothing beyond it.

    **An ``error`` has many causes** — a server that never spawned, a tool
    listing that timed out, a plugin whose child died, a skill file that cannot
    be read, something else entirely — and they do not share a remedy.  So the
    refusal must not guess at one: "its owner reconnects on its own", "the
    watchdog restarts it", "nothing to enable, just wait" are all claims about a
    cause this code has not checked, and a wrong one is worse than none — it
    tells the model (or the user reading over its shoulder) that nothing needs
    fixing while the real cause sits there.  Naming a family's diagnostic has
    the same problem from the other side: ``mcp_list`` and ``system_health`` are
    each right for one family and wrong for the next.

    What is true of every one of them is the state, the fact that the row cannot
    be used while it holds, and that ``*_set_enabled`` is not the remedy (it is
    the ``disabled`` remedy — the values are mutually exclusive).
    """
    return f"Error: tool '{name}' cannot be {action} — its status is error."


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
        disabled_builtins: tuple[str, ...] = (),
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
        #: Per-entry ``enabled: false`` names, by their own section: a builtin,
        #: a job in ``job`` (the user's tools), a plugin's own tool in
        #: ``plugin``.  A builtin's disable keeps it out of the REGISTRY (the
        #: factory skips it) but not out of the catalog — its row is mirrored
        #: ``disabled``, so yaml and the db agree on it.
        self._disabled_builtins = frozenset(disabled_builtins)
        self._disabled_jobs = frozenset(disabled_jobs)
        self._disabled_plugin = frozenset(disabled_plugin)
        #: The host's semantic actor (set by AgentService after startup) —
        #: tool_search reads its embedder for hybrid retrieval.
        self.semantic_manager: "SemanticManager | None" = None

    @property
    def store(self) -> CatalogStore:
        return self._store

    def reload_disabled(
        self,
        *,
        builtins: "frozenset[str] | set[str] | None" = None,
        jobs: "frozenset[str] | set[str] | None" = None,
        plugin: "frozenset[str] | set[str] | None" = None,
    ) -> None:
        """Re-read the per-entry ``enabled: false`` sets from a fresh config.

        The sets are captured when the service is built, so a hand-edit to
        ``tools.yaml`` mid-session would otherwise wait for the next boot.
        ``None`` keeps a set as it was (the caller re-read only some sections).
        """
        if builtins is not None:
            self._disabled_builtins = frozenset(builtins)
        if jobs is not None:
            self._disabled_jobs = frozenset(jobs)
        if plugin is not None:
            self._disabled_plugin = frozenset(plugin)

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

    def _row_for(
        self, tool: "Tool", *, disabled_servers: frozenset[str] = frozenset(),
    ) -> dict:
        """The catalog row for one registered tool — the ONE row builder.

        The boot seed and a plugin's (re)connect both come through here, so a
        row's provenance (``category`` + ``source_id``), its schema and its
        ``status`` mirror cannot drift between the two paths.
        """
        name = _tool_name(tool)
        return {
            "name": name,
            "description": _own_description(tool),
            "category": catalog_category(tool),
            "source_id": _source_id(tool),
            "schema": tool_descriptor(tool),
            "status": self._row_status(tool, disabled_servers),
            # Only a brand-new row sees this — reconcile applies status on
            # INSERT alone, which is exactly the keep-existing rule…
            "load_status": self.default_status(name),
            # …unless the entry is marked ``autoload``: that is the config
            # saying "this tool stays loaded", so its status is an authority
            # and may overwrite what the model decided.  (Meta tools are born
            # loaded too, but they are protected by the whitelist, not by a
            # config switch — a row of theirs is never unloaded to begin with.)
            "override_status": name in self.autoload,
        }

    def _row_status(
        self, tool: "Tool", disabled_servers: frozenset[str] = frozenset(),
    ) -> str | None:
        """The row's ``status`` — always tools.yaml's answer, never a verdict.

        There is no per-tool enable anywhere in the system: a family's section
        decides, and for an external server that decision is the SERVER's
        switch (all of its tools move together).  A builtin / job / plugin tool
        mirrors its own section's disable.  ``None`` only for a tool whose owner
        the config does not name — "not known to be off" is not "off".

        The runtime's ``error`` is not this method's business: a mirror of a
        live source says what the config says about the switch, and the
        connectivity marks are what say whether the owner is up.

        *disabled_servers* is passed in (not looked up per tool) because one
        pass can carry a four-figure number of an external server's tools, and
        a per-tool config read would re-parse ``tools.yaml`` per row.
        """
        if _is_external(tool):
            server = _source_id(tool)
            if not server:
                return None
            return config_status(server not in disabled_servers)
        category = catalog_category(tool)
        if category == "builtin" and _tool_name(tool) in self._disabled_builtins:
            return STATUS_DISABLED
        if category == "job":
            return config_status(_tool_name(tool) not in self._disabled_jobs)
        if category == "plugin":
            return config_status(_tool_name(tool) not in self._disabled_plugin)
        return config_status(True)

    @staticmethod
    def _disabled_servers() -> frozenset[str]:
        """Servers switched OFF in ``tools.yaml`` — one read per sync pass."""
        try:
            from slife.plugins.mcp_gateway import config as _cfg
            return frozenset(
                name for name, entry in _cfg.servers().items()
                if isinstance(entry, dict) and entry.get("enabled") is False
            )
        except Exception:
            # An unreadable config is not a reason to call every server off.
            return frozenset()

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
        disabled_servers = self._disabled_servers()
        rows = [
            self._row_for(t, disabled_servers=disabled_servers)
            for t in tools if _tool_name(t)
        ]
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
        section of ``tools.yaml``).  They are rows all the same — that is how
        ``tool_search`` reaches them — but they are not function tools, so
        they carry no load state (``load_status`` stays 'n/a').

        Upsert + purge, the same contract the plugin mirror follows: every
        entry is (re)written, and a name that vanished from the source loses
        its row — a deleted skill must not linger as a hit `tool_search` keeps
        returning.  Returns the purged names.

        ``load_status`` is written as ``STATUS_NA`` — neither family has a
        load state, and "no load state applies" is a value in the column's
        domain, not a NULL.  ``override_status`` is left off for the same
        reason: an ``autoload`` flag on a skill/cli entry has nothing to own.

        ``status`` comes from the source itself: a skill whose SKILL.md cannot
        be read is mirrored ``error`` (the mirror computes that, it is not a
        config switch), a cli entry mirrors its ``enabled`` flag.  That is why
        these rows carry ``status_verdict`` — the mirror IS the verdict's
        author for this family, so it writes what it found and a fixed file
        goes back to ``enabled``.  A server-backed row has no such authority:
        its config mirror may not claim an owner is up.

        **The row name is namespaced** (``skill:browser-harness``).  A name is
        the row's identity — the primary key, the embeddings' foreign key, the
        key every search result is merged by — so two families cannot share
        one, and sharing is not a mistake to prevent: ``browser-harness`` is a
        CLI and the skill that documents it.  Qualifying here keeps ``name``
        unique by construction, and the prefix is self-evident to a reader —
        the tool to call is the part after the colon.
        """
        result = await self._store.reconcile(
            [
                {
                    "name": f"{category}:{name}",
                    "description": spec.get("description", ""),
                    "category": category,
                    "schema": spec.get("schema"),
                    "status": spec.get("status"),
                    "status_verdict": True,
                    "load_status": STATUS_NA,
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

        if not is_function_category(row.get("category", "")):
            return False, (
                f"Error: tool '{name}' has no load/unload state "
                f"(category '{row.get('category')}')."
            )

        eff = await self._store.get_effective(name)
        if eff == STATUS_DISABLED:
            return False, f"Error: tool '{name}' is disabled — enable it first."
        if eff == STATUS_ERROR:
            return False, status_error_refusal(name, "loaded")
        if eff == "loaded":
            return True, f"tool '{name}' is already loaded."

        await self._store.set_load_status(name, "loaded", bump=True)
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
        if not is_function_category(row.get("category", "")):
            return False, (
                f"Error: tool '{name}' has no load/unload state "
                f"(category '{row.get('category')}')."
            )
        if row.get("load_status") != STATUS_LOADED:
            return True, f"tool '{name}' is already unloaded."
        await self._store.set_load_status(name, "unloaded")
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
        enabled: bool | None = None,
    ) -> bool:
        """Upsert a server-backed tool row (schema-change detection → re-embed).

        A newly seen tool lands ``unloaded`` — unless its server entry is
        marked ``autoload: true``, which seeds the whole set loaded — while an
        existing row keeps whatever the model decided.  For everything else
        ``func-tool-load`` is the only way into the injection set.

        An ``autoload`` server owns its tools' status (there is no per-tool
        autoload for mcp/rest-api — the flag is on the server, so it is one
        decision covering the whole set): ``override_status`` lets the row's
        status be rewritten when it differs.

        ``enabled`` is the server's own on/off switch (``None`` = no opinion);
        it moves independently of the load state — see
        :meth:`set_source_enabled`.
        """
        return await self._store.upsert_tool(
            name,
            description=description,
            category=category,
            source_id=server,
            schema=schema,
            status=config_status(enabled),
            load_status=self.default_status(name, server=server),
            override_status=server in self.autoload_servers,
        )

    async def mirror_external_tools(
        self,
        server: str,
        tools: "list[dict]",
        *,
        category: str = "mcp",
        enabled: bool | None = None,
    ) -> list[str]:
        """Mirror ONE server's whole tool set in a single reconcile.

        The batch face of :meth:`upsert_external_tool`, and it exists for cost:
        ``reconcile`` reads the catalog's rows once per call, so the per-tool
        form pays a full-table scan PER TOOL — a server with ~1100 tools paid
        ~1100 of them on every listing, sequentially, which is most of what a
        cold reconcile spends its time on.  Batched, the same server costs one
        read of the table, one write of the columns that moved, and one purge.

        *tools* are the engine's own shape (``name``, ``description``,
        ``inputSchema``, optional ``full_name``) — the row's ``schema`` column
        is built here, so no caller assembles a descriptor by hand.  Rows land
        with :meth:`default_status`; an existing row keeps the load state the
        model chose (``reconcile`` applies ``load_status`` on INSERT alone).

        Returns the names purged — tools this server no longer publishes.  An
        EMPTY *tools* is "not ready yet", never "owns nothing": it mirrors
        nothing and purges nothing, so a transient empty listing cannot wipe a
        server's rows.
        """
        rows: list[dict] = []
        for t in tools:
            tname = t.get("name")
            if not tname:
                continue
            description = t.get("description", "") or ""
            full_name = t.get("full_name") or f"{server}__{tname}"
            rows.append({
                "name": full_name,
                "description": description,
                "category": category,
                "source_id": server,
                "schema": descriptor_json(
                    tname, description,
                    t.get("inputSchema", {"type": "object", "properties": {}}),
                ),
                "status": config_status(enabled),
                "load_status": self.default_status(full_name, server=server),
                # The autoload flag lives on the SERVER entry (mcp/rest-api
                # have no per-tool one), so it is one decision over the whole
                # set: every row of such a server owns its status.
                "override_status": server in self.autoload_servers,
            })
        if not rows:
            return []
        result = await self._store.reconcile(rows)
        # Upsert-then-purge: after the reconcile this server owns exactly its
        # incoming names plus whatever vanished — the same "a tool a server
        # stopped publishing loses its row" contract every other family's
        # mirror keeps.
        gone = await self._store.purge_source_except(
            server, {r["name"] for r in rows},
        )
        if gone:
            logger.info(
                "catalog_external_tools_purged server=%s tools=%d", server, len(gone),
            )
        self.wake_indexer([*result["schema_changed"], *gone])
        return gone

    async def set_source_enabled(self, source: str, enabled: bool) -> int:
        """Mirror a server's on/off switch onto its rows' ``status``.

        The sync's only config write to this column, and a deliberately narrow
        one: it never touches ``load_status`` (the model's loaded/unloaded
        decision survives the round trip) and never deletes rows (a disabled
        server keeps its tools, showing ``disabled`` — a state of its own, so
        the model can tell "switched off" from "down", which is ``error``).
        """
        if not self.write_owner:
            return 0
        return await self._store.set_source_enabled(source, enabled)

    async def mark_source_error(self, source: str) -> int:
        """Flag one owner's tools ``error`` — it is unusable right now.

        The single verdict for every unavailable case: not yet connected at
        startup, disconnected, a failed connect, or a dead gateway child.
        Effective status becomes ``error`` — a state of its own, so the row
        still says the tool belongs to a server that is simply not up — while
        the loaded/unloaded the model chose stays on the row untouched.  A row
        the config switched off is left alone: it is ``disabled``, not down.
        """
        if not self.write_owner:
            return 0
        return await self._store.mark_source_error(source)

    async def mark_all_external_error(self) -> int:
        """Flag EVERY external tool ``error`` — nothing is live yet.

        Used both at catalog init (no server has connected yet) and when the
        gateway child dies (all of its servers are unreachable at once).  It
        flags, never rewrites: the load state a previous session persisted is
        what makes the catalog worth restoring at all.
        """
        if not self.write_owner:
            return 0
        return await self._store.mark_all_external_error()

    async def mark_server_connected(self, source: str) -> int:
        """A server (re)connected — clear its tools' ``error``.

        Nothing else moves: a tool the model had loaded is still ``loaded``,
        because the verdict was never written into the load state.  And a tool
        the config switched off stays ``disabled`` — coming back up is not a
        config decision.
        """
        if not self.write_owner:
            return 0
        return await self._store.mark_source_connected(source)

    async def mark_plugin_connected(self, plugin: str) -> int:
        """A plugin (re)connected — clear its tools' ``error``.

        Identical to :meth:`mark_server_connected`: with the verdict in the
        status column's runtime lane there is no per-tool default to restore,
        so a plugin restart leaves every row saying exactly what it said
        before.
        """
        if not self.write_owner or not plugin:
            return 0
        return await self._store.mark_source_connected(plugin)

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

        The mirror image of the removed ``sync_config_servers``: tools.yaml
        is still the authority, but it is now compared against the servers
        that actually OWN tool rows instead of against a server table.
        """
        if not self.write_owner:
            return set()
        purged = await self._store.purge_missing_sources(configured)
        if purged:
            logger.info("catalog_purged_config_removed servers=%r", sorted(purged))
        return purged

    async def purge_source_except(self, source: str, keep: "set[str]") -> list[str]:
        """Drop one owner's rows for tools it no longer publishes (main-owner only).

        The per-tool counterpart of :meth:`purge_source`: the owner is still
        configured and still connected — it just stopped offering one of its
        tools.  Every other family gets this from its own mirror (a plugin's
        source-scoped ``sync_system_tools``, a skill/cli ``sync_category``);
        this is how the external families get the same "a vanished tool loses
        its row" contract.
        """
        if not self.write_owner:
            return []
        return await self._store.purge_source_except(source, keep)


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