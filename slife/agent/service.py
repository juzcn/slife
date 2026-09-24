"""Agent service layer — wires together LLM, tools, message history, and loop.

Owns the agent's runtime state. The TUI delegates to this service
rather than directly managing agent internals.

If MCP is enabled in config, also manages the MCP wrapper connection
and registers MCP proxy tools.

If A2A is enabled in config, manages the P2P agent mesh: connects to
the MQTT broker, publishes presence, discovers peers, and routes tasks
through a unified Inbox.
"""

import asyncio
import json
import logging
import os
import sys
import time as _time
from collections import deque
from datetime import datetime
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from slife.agent.system_prompt import build as build_system_prompt
from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.message_history import MessageHistory, turn_header
from slife.agent.loop import AgentLoop, AgentEventHandler, AgentResult
from slife.agent.inbox import Inbox, MemorySaveError, MessageHistoryStore, WorkerHistoryStore
from slife.agent.roles import Role, caps_for
from slife.agent.plugins import (
    PluginBehavior,
    PluginLifecycle,
    PluginRegistry,
    PluginStartStatus,
    plugin_port_env,
)
from slife.plugins.spec import PLUGIN_SPECS
from slife.a2a.identity import HUMAN

if TYPE_CHECKING:
    from slife.tools.catalog import CatalogOpDelta
    from slife.tools.catalog_service import ToolCatalogService
from slife.tools.factory import create_tools_from_config, disabled_tool_instances
from slife.tools._config_io import config_read_modify_write
from slife.mcp.tool_adapter import create_proxy_tools
from slife.platform import terminate_process_sync
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe
from slife.server_utils import is_internal_tool
# The harness composes the TUI's user-facing text, so it localizes it too
# (same as slife/tools/system.py) — the TUI only renders what it is handed.
from slife.ui.i18n import t

logger = logging.getLogger(__name__)


class MemoryDatabaseError(Exception):
    """The memory database is present but unreadable/unwritable (broken
    schema, corruption, disk error).

    Memory is core — the agent must not run without it.  Restore treats
    this as fatal (startup abort); turn-save treats it as a hard stop.
    """


# Module-level callbacks invoked when the active model is switched at
# runtime (e.g. by the model_switch tool).  Each callback receives the
# new model ref string (e.g. "deepseek/deepseek-v4-flash").
_on_model_switched: list[Callable[[str], None]] = []

# ── Sharefile tunnel readiness watch ─────────────────────────────────────
# The sharefile plugin eager-starts its tunnel on a background task, so the
# harness's one-time probe must not race a still-running attempt: a failed
# start retries up to 3× with 2s/4s backoff (~9s before it concludes).  The
# harness probes __check until the plugin reports a terminal state, bounded
# by the registry's ready.tunnel_settle (dev-owned), and surfaces "tunnel
# down" only once.
_TUNNEL_PROBE_INTERVAL = 1.0  # seconds — cadence between __check probes (not a budget)


def _server_category(name: str) -> str:
    """An external server's catalog category (``mcp`` vs ``rest-api``).

    tools.yaml is the only source, and inside it the ``rest-api`` SECTION is
    the whole answer — an entry there is a REST API, wherever its file may
    have been hand-moved since.  Nothing is tagged for it: ``source`` records
    where a definition was DOWNLOADED from (github / registry / hand), which
    is a different question, so the category is read off the placement.
    """
    try:
        from slife.plugins.mcp_gateway import config as _cfg

        return "rest-api" if _cfg.is_rest_api(name) else "mcp"
    except Exception:
        logger.debug("catalog_server_category_lookup_failed server=%s", name, exc_info=True)
        return "mcp"


def _health_component(name: str) -> str:
    """The health component an external server belongs to.

    ``system_health`` reports the two server families separately (an operator
    manages them with different tool sets), so a startup record has to name the
    same component its live check does or the merge layer cannot supersede it.
    """
    return "rest-api" if _server_category(name) == "rest-api" else "mcp_servers"


def _short_reason(reason: str, limit: int = 140) -> str:
    """Condense a provider's failure reason to one readable line.

    A provider's reason is written for the log — it can embed an install URL
    or a multi-line output tail.  The TUI warning gets the first sentence, or
    a truncated single line when there is no sentence break.
    """
    text = " ".join(reason.split())
    if not text:
        return ""
    cut = text.find(". ")
    if 0 < cut < limit:
        return text[: cut + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── Tool result compaction for permanent memory ─────────────────────────
#
# The live context keeps oversized tool results whole — the model
# reasons over them during the turn (the 20% tool_result_ceiling is the
# only live cap, a hard window-safety constraint).  Permanent memory does
# NOT: tool output is reproducible (re-run the tool), so the Turns DB stores
# a head+tail digest.  This keeps saved turns small enough that session
# restore can fill the context floor, and keeps a recall call cheap.
# Truncation is always announced to the model via an explicit marker.


def compact_tool_results(turn_messages: list[dict], budget_chars: int) -> int:
    """Compress oversized tool results in a turn to head+tail digests.

    A ``tool`` message whose content exceeds *budget_chars* is replaced
    with ``head + marker + tail`` (the budget split evenly between head
    and tail).  The marker tells a future reader the output was compacted
    at save time, how large it originally was, and which tool to re-run to
    retrieve the full version.  Results that already fit are left
    untouched.  Returns the number of messages compacted.

    The replacement is a *copy* — the caller's ``turn_messages`` list
    entries are swapped for new dicts, never mutated in place, so the
    live history keeps the full output.
    """
    if budget_chars <= 0:
        return 0
    head_chars = max(budget_chars // 2, 1)
    tail_chars = max(budget_chars - head_chars, 1)

    # Map tool_call_id → tool name so the marker can name the tool to
    # re-run (a future reader doesn't have the live assistant message).
    name_by_id: dict[str, str] = {}
    for m in turn_messages:
        for tc in m.get("tool_calls") or []:
            cid = tc.get("id")
            if cid:
                name_by_id[cid] = (tc.get("function") or {}).get("name", "")

    compacted = 0
    for i, m in enumerate(turn_messages):
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str) or len(content) <= budget_chars:
            continue
        if "[compacted at save:" in content:
            # Already compacted — re-applying would double-wrap and write an
            # inflated original-size claim.  Skip so compaction is idempotent.
            continue
        tcid = m.get("tool_call_id")
        tool_name = name_by_id.get(str(tcid), "") if tcid else ""
        name_note = f"by re-running {tool_name}" if tool_name else "by re-running the tool"
        marker = (
            f"\n… [compacted at save: original {len(content)} chars — "
            f"full output retrievable {name_note}]\n"
        )
        turn_messages[i] = {
            **m,
            "content": content[:head_chars] + marker + content[-tail_chars:],
        }
        compacted += 1
    return compacted


def _extract_turn_annotation(
    turn_messages: list[dict],
) -> tuple[str | None, str | None]:
    """Extract a rowid-less ``turn_summarize`` call (the current turn).

    The model annotates the in-flight turn by calling ``turn_summarize``
    without a rowid; the tool returns "captured" without writing.  The
    summary/tags are applied here, at the single save point, so the annotation
    lands on exactly the turn being saved — no ``latest_rowid()`` race, no
    cross-process pending state, and a rolled-back turn simply never applies.
    Explicit-rowid calls are ignored (the tool already wrote them).
    """
    summary: str | None = None
    tags: str | None = None
    for msg in turn_messages:
        for tc in msg.get("tool_calls") or []:
            name = tc.get("function", {}).get("name", "")
            if name.split("__")[-1] != "turn_summarize":
                continue
            try:
                args = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if "rowid" in args and args.get("rowid") is not None:
                continue  # explicit rowid — the tool handled it already
            if args.get("summary"):
                summary = args["summary"]
            if args.get("tags"):
                tags = args["tags"]
    return summary, tags


class AgentService:
    """Wires together LLM client, tools, message history, and agent loop.

    Owns the agent's runtime state. The TUI delegates to this service
    rather than directly managing agent internals.

    If MCP is enabled in config, also manages the MCP wrapper connection
    and registers MCP proxy tools.

    If A2A is enabled, manages the P2P mesh: Inbox, A2AClient, and
    per-source histories.
    """

    def __init__(self, config: Config, role: Role = Role.MAIN):
        self.config = config
        # __post_init__ guarantees these are never None at runtime
        assert self.config.a2a_config is not None
        assert self.config.subagent_config is not None
        #: Which agent this process is, and the harness grants that follow from
        #: it.  Every role-dependent decision reads ``self.caps`` — the table in
        #: :mod:`slife.agent.roles` is the ONE place the two roles differ, and
        #: ``tests/test_subagent_parity.py`` holds it to that.
        self.role = role
        self.caps = caps_for(role)
        #: The parent's cloned history, when the transport delivered one at
        #: spawn.  A worker's per-task history is seeded from it; the main
        #: agent never sets it (its context is its own continuity).
        self.inherited_context: list[dict] | None = None

        # Build the shared ToolContext, which carries the config + registry
        # that tools need at runtime.
        from slife.tools.context import ToolContext
        self._tool_ctx = ToolContext(config=config)
        self.tool_registry = create_tools_from_config(
            config.tools, config=config, is_subagent=role.is_worker,
            ctx=self._tool_ctx,
        )
        # Backfill the registry reference (created by the factory)
        self._tool_ctx.registry = self.tool_registry
        #: Shared tool catalog (SE tools.db) — opened async in
        #: :meth:`_init_catalog` (start_inbox).  Best-effort: None on failure
        #: degrades the loop to today's all-registered injection.
        self._catalog: "ToolCatalogService | None" = None
        #: Host-side semantic drainer over the catalog (main agent only).
        self._catalog_semantic = None
        self._catalog_semantic_task: asyncio.Task | None = None
        #: The reader's boot warmup (a worker owns no drainer, so its semantic
        #: capability comes up on a background task instead of at enable()).
        self._catalog_reader_warmup: asyncio.Task | None = None
        self._tool_load_threshold = getattr(config, "tool_load_threshold", 100) or 100
        self.llm_client = LLMClient(config.active_model)
        # Max tool result = tool_result_ceiling × context_window × 3 chars/token
        max_tool_result_chars = int(
            config.tool_result_ceiling
            * config.active_model.context_window
            * 3
        )
        # Pending A2A peer presence events (epoch, TUI line), drained into
        # the turn prompt on the next turn.  Unbounded by design — events
        # are consumed on every turn, so the steady-state size is "events
        # since last turn"; the guard below only protects against a
        # pathological long-idle + heavy-flapping session.
        self._presence_events: deque[tuple[float, str]] = deque()
        # Open failed/missed scheduled runs, surfaced in the ``_turn_prompt``
        # until the user backfills or skips them.  Refreshed by the
        # schedule loop and the startup sweep (see schedules.py).
        self._schedule_pending: list[dict] = []
        # Inbound A2A tasks orphaned by a restart, surfaced in the
        # ``_turn_prompt`` until their peers are answered.  Replaced wholesale
        # from each a2a drain (the plugin owns the set) — it is state, not a
        # queue, so it is never cleared here.
        self._a2a_stale: list[dict] = []
        # Subagents fail fast on LLM errors and cap a single stream call:
        # they have no user to wait on, so a raised provider error / timeout
        # must surface as a pushed-back result rather than retrying a flaky
        # provider or hanging on a silent stall.  The main agent keeps the
        # module defaults (retry transient transport errors, no stream cap).
        # ``stream_retries`` is that grant; the timeout is its companion.
        subagent_stream_timeout = (
            _timeouts.timeouts.work.task_budget
            if not self.caps.stream_retries else None
        )
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
            max_tool_result_chars=max_tool_result_chars,
            tool_timeout=config.tool_timeout,
            cutin_enabled=config.cutin_enabled,
            context_window=config.active_model.context_window,
            context_ceiling=config.context_ceiling,
            context_floor=config.context_floor,
            supports_vision=config.active_model.supports_vision,
            model_name=config.active_model.display_name,
            input_modalities=", ".join(config.active_model.input_modalities),
            presence_provider=self._drain_presence_events,
            schedule_provider=self._schedule_pending_provider,
            a2a_stale_provider=self._a2a_stale_provider,
            drop_context_turns=self.drop_context_turns,
            set_context_turns=self.set_context_turns,
            clear_context_turns=self.clear_context_turns,
            recall_turns=self.recall_turns,
            recall_available=lambda: self.memdb_enabled,
            turns_by_ids=self.turns_by_ids,
            # A worker's history is one-shot per task, so recall has nothing to
            # select from — and it would cost a discriminator call per task.
            # The role decides, not the config.
            rebuild_message=(
                self.config.rebuild_message and not self.role.is_worker
            ),
            stream_timeout=subagent_stream_timeout,
            stream_max_retries=None if self.caps.stream_retries else 0,
            tool_catalog=self._catalog,
            load_threshold=self._tool_load_threshold,
        )
        self.message_history = MessageHistory(
            system_prompt=build_system_prompt(self.config),
        )
        self._tool_ctx.message_history = self.message_history
        # Runtime iteration-cap hook for the set_max_iterations tool.
        self._tool_ctx.set_max_iterations = self.agent_loop.set_max_iterations
        # Runtime mid-turn preemption hook for the set_midturn_input tool.
        self._tool_ctx.set_midturn_input = self.set_midturn_input
        # USER.md write hook for the add_user_pref tool — re-render the
        # system prompt (re-reads USER.md) so the new preference is live
        # from the next call.  Populated for the main agent and subagents.
        self._tool_ctx.refresh_system_prompt = self.refresh_system_prompt
        # Scheduled-task manual-fire hook for the run_schedule_now tool.  One
        # timer per task belongs to the session the user is in, so a worker
        # leaves the hooks None.
        if self.caps.schedules:
            self._tool_ctx.fire_schedule_now = self.fire_schedule_now
            self._tool_ctx.schedule_wakeup = self.schedule_wakeup
        self.session_usage = TokenUsage()

        # Autonomous heartbeat — background idle task + TUI surfacing hooks.
        self._on_autonomous = None
        self._on_heartbeat = None
        # Scheduler-driven output (cron fires / backfill).
        self._on_schedule = None
        # Timer-driven output (wait_minutes wake).
        self._on_timer = None
        # In-flight timer wakes — cancelled on stop, discarded on completion.
        self._timer_tasks: set[asyncio.Task] = set()
        self._heartbeat_task: asyncio.Task | None = None

        # Scheduled-task trigger loop — times tasks and injects triggers.
        self._schedule_task: asyncio.Task | None = None
        #: One-shot startup sweep (``schedules.schedule_startup_sweep``):
        #: reaps runs a previous lifetime left unfinished to failed, then
        #: exits.  Kept separately from ``_schedule_task`` so the timed loop
        #: stays purely a firing loop.
        self._schedule_startup_task: asyncio.Task | None = None

        # ── Plugin startup convergence ──────────────────────────────
        # The service opens for user input only after every attempted
        # plugin spawn has converged (ready / skipped / failed — the
        # connect either negotiated its era or the spawn failed).
        # The TUI input and the inbox consumer gate on this.
        # Event-driven: set by the last spawn's ``finally``, never polled.
        self._startup_plugins: set[str] = set()
        self._startup_settled = asyncio.Event()

        # ── Unified message queue (always active) ──────────────────
        # Every input — human keyboard, A2A MQTT, WeChat — flows
        # through the same inbox queue.  Processed serially.
        histories = self._build_histories()
        histories._by_source[HUMAN] = self.message_history

        self.inbox = Inbox(
            agent_loop=self.agent_loop,
            histories=histories,
            on_activity=self._notify_activity,  # always active for WeChat etc.
            # Turns are the main agent's to persist; a worker's are ephemeral.
            on_turn_complete=(
                self.save_to_memory if self.caps.turn_persistence else None
            ),
            # Startup gate: no turn runs until every plugin spawn converged.
            # Only the process that SPAWNS plugins can converge them, so a
            # worker's inbox must not gate on an event nothing would ever set.
            ready=(
                self.wait_startup_settled if self.caps.startup_gate else None
            ),
        )
        # Cut-in mode wiring: the loop gates the boundary check on the inbox's
        # has_injectable; the auto-invoked _check_new_input tool pulls the
        # message itself via the shared tool context's extract_injectable.
        # Preempting one's own turn is the main agent's — a worker runs one
        # task at a time and never cuts into it (the hooks stay None).
        if self.caps.cutin:
            self.agent_loop.pending_input_has = self.inbox.has_injectable
            self._tool_ctx.extract_injectable = self.inbox.extract_injectable
        self._inbox_task: asyncio.Task | None = None
        # slife-as-plugin in-process MCP server (server, task, stop) — started
        # lazily in start_inbox for the main agent only; None for subagents.
        self._host_server: tuple | None = None

        # ── Memory health ──────────────────────────────────────────
        # Memory is core.  A fatal turn-save failure (broken schema,
        # corruption, disk error) sets _memory_broken and freezes the
        # inbox — the agent must not keep running turns it can't persist.
        self._memory_broken = False
        self._memory_error = ""
        # TUI callback (set by the app) → persistent red banner.
        self._on_memory_broken: "Callable[[str], None] | None" = None
        # TUI callback (set by the app) → file-sharing tunnel unavailable.
        # The harness owns the surfacing: the memfiles plugin never talks
        # to the TUI — the main process probes __check after the
        # plugin is ready and reports a terminal failure here.
        self._on_tunnel_down: "Callable[[str], None] | None" = None

        # ── Plugin registry — the single source of truth ────────────
        # Built once from the central plugin contract (PLUGIN_SPECS): every
        # declared plugin has a PluginLifecycle before any start/connect.
        # self._plugins aliases registry.lifecycles — never rebind it after
        # here; a new (auto-discovered) plugin registers via self._registry.
        self._registry = PluginRegistry(self)
        self._plugins: dict[str, PluginLifecycle] = self._registry.lifecycles
        # Per-plugin behavior (enable gate / after-ready glue), bound from the
        # spec's declared method-names.  A spec naming a missing method is a
        # programming error — _resolve_plugin_behaviors asserts at startup.
        self._plugin_behaviors: dict[str, PluginBehavior] = self._resolve_plugin_behaviors()
        # MCP enrichment guard: coalesces per-server tool discovery between
        # startup glue and mcp_set callbacks.
        self._mcp_syncing: set[str] = set()
        # On-demand reconcile guard: prevents concurrent func_tool_load /
        # tools/list_changed reconciliation from racing.  A token plus when it
        # was taken — a pass still holding it after ``ready.tool_sync_wait`` is
        # not slow (every await in it is bounded by that same value), it is
        # wedged, and the next pass takes it over instead of queueing behind it
        # for the rest of the process.
        self._mcp_reconcile_owner: object | None = None
        self._mcp_reconcile_started_at: float | None = None
        #: Whether the TUI has been told the tool set is ready yet.  The first
        #: reconcile is the slow one the user waits through; once it has
        #: converged the line is sent, and never again for this process.
        self._tool_sync_reported: bool = False
        #: When this process's first reconcile pass started — the wait the
        #: tool-set line reports, and the anchor for how long it may wait for
        #: servers that are still starting (``ready.tool_sync_wait``).
        self._tool_sync_started_at: float | None = None
        #: Last WeChat login state seen by the poll loop — ``None`` until the
        #: first drain.  Only TRANSITIONS are announced, so a steady session is
        #: not re-reported every poll, and a session that was never logged in
        #: seeds silently rather than announcing the startup default.
        self._wechat_logged_in: bool | None = None
        # Last-seen mtimes of the registry-less families' sources (tools.yaml,
        # the skills dir) — the reconcile re-mirrors them when one moves, so a
        # hand-edit lands without a restart.
        self._local_rows_mtimes: tuple[float, float] | None = None

        # A2A integration state
        self._subagent_manager = None
        self._on_activity_callbacks: list = []  # callbacks for TUI notification

        # Register for runtime model-switch notifications so the
        # LLM client and agent loop stay in sync with the active model.
        _on_model_switched.append(self.reload_active_model)

    # ── Plugin contract binding ─────────────────────────────────────────

    def _resolve_plugin_behaviors(self) -> dict[str, PluginBehavior]:
        """Bind each spec's declared enable/after-ready method-name onto this
        service instance.

        Behavior lives here (not in the spec) because gates and after-ready
        glue touch service internals; the spec stays import-safe for
        tool_adapter / system.py / the mcp child.  A spec that names a method
        this class does not define is a programming error — surface it at
        construction, not mid-startup.
        """
        behaviors: dict[str, PluginBehavior] = {}
        for spec in PLUGIN_SPECS.values():
            enable: Callable[[], Awaitable[bool]] | None = None
            after_ready: Callable[[PluginLifecycle], Awaitable[None]] | None = None
            if spec.enable_method:
                bound = getattr(self, spec.enable_method, None)
                assert callable(bound), (
                    f"AgentService.{spec.enable_method} (spec {spec.name}) missing"
                )
                enable = cast(Callable[[], Awaitable[bool]], bound)
            if spec.after_ready_method:
                bound = getattr(self, spec.after_ready_method, None)
                assert callable(bound), (
                    f"AgentService.{spec.after_ready_method} (spec {spec.name}) missing"
                )
                after_ready = cast(
                    Callable[[PluginLifecycle], Awaitable[None]], bound,
                )
            if enable is not None or after_ready is not None:
                behaviors[spec.name] = PluginBehavior(
                    enable=enable, after_ready=after_ready,
                )
        return behaviors

    def _gateway_lifecycle(self) -> PluginLifecycle | None:
        """The gateway plugin's lifecycle (the spec with ``gateway=True``),
        or None — used by the mcp enrichment glue, name-free."""
        for lc in self._plugins.values():
            spec = self._registry.specs.get(lc.name)
            if spec is not None and spec.gateway:
                return lc
        return None

    @property
    def model_display_name(self) -> str:
        """Active model as ``provider/model`` — the canonical ref the LLM sees.

        Multiple providers can serve the same model name, so the status bar
        shows the full ref (``deepseek/deepseek-v4-flash``) to disambiguate.
        """
        return self.config.active_model.ref

    def _build_histories(self) -> MessageHistoryStore:
        """The inbox's history store for this role — built here, not by a boot.

        The main agent has ONE context that persists for the session.  A worker
        gets a fresh one-shot history per task, seeded from the clone its parent
        sent at spawn — and a worker's system prompt is the subagent one, so the
        store is built from the role rather than replaced afterwards.  (The
        worker's boot used to swap ``inbox._histories`` and clear
        ``_on_turn_complete`` after the service had wired itself; the
        differences belong where the role is known.)
        """
        if self.role.is_worker:
            return WorkerHistoryStore(
                build_system_prompt(self.config, is_subagent=True),
                context_provider=lambda: self.inherited_context,
            )
        return MessageHistoryStore(system_prompt=build_system_prompt(self.config))

    @property
    def context_window(self) -> int:
        """Context window size (tokens) of the active model."""
        return self.config.active_model.context_window

    @property
    def current_context_tokens(self) -> int:
        """Context tokens the next API call would send — same single source
        as ``_turn_prompt`` (see :meth:`AgentLoop.context_tokens_for`):
        last API call's actual prompt tokens, else the restore-time
        estimate, else a live history estimate."""
        return self.agent_loop.context_tokens_for(self.message_history)

    @property
    def thinking_enabled(self) -> bool:
        """Whether thinking/reasoning mode is active."""
        return self.config.active_model.thinking_enabled

    def switch_model(self, ref: str) -> str:
        """Switch the active model directly from the UI — no LLM round-trip.

        Unlike ``model_switch`` (which requires the LLM to call it), this
        works even when the current model is unavailable: it validates the
        ref against the in-memory registry, persists ``active_model`` to
        the config file, and rebuilds the runtime (LLM client, agent loop,
        system prompt).  Returns a human-readable confirmation.

        Raises ``ValueError`` for an unknown ref.
        """
        if not any(m.ref == ref for m in self.config.models):
            raise ValueError(
                f"Unknown model ref '{ref}'. "
                f"Available: {[m.ref for m in self.config.models]}"
            )
        # Cross-process read→mutate→write lock: the memdb child RMWs the same
        # slife.yaml under config_read_modify_write — without the same lock a
        # concurrent embeddings-config write clobbers this change (and vice
        # versa).  The in-process _write_lock is not enough across processes.
        if self.config._path is not None:
            with config_read_modify_write(self.config._path):
                raw = self.config._read_config("switch_model", ref)
                if raw is not None:
                    raw["active_model"] = ref
                    self.config._write_config(raw)
        self.reload_active_model(ref)
        return f"Switched to {self.config.active_model.display_name}"

    def set_midturn_input(self, enabled: bool) -> str:
        """Toggle mid-turn input preemption at runtime (persisted to config).

        True = a new inbound message may cut into the running turn at the next
        safe iteration boundary (the default); False = messages queue until
        the running turn ends (the original ``queue`` behavior).  Mirrors
        ``switch_model``: updates the live loop and persists the config file.
        """
        enabled = bool(enabled)
        self.config.cutin_enabled = enabled
        self.agent_loop.cutin_enabled = enabled
        if self.config._path is not None:
            with config_read_modify_write(self.config._path):
                raw = self.config._read_config(
                    "set_midturn_input", self.config.agent_name,
                )
                if raw is not None:
                    raw.setdefault("agent", {})["cutin_enabled"] = enabled
                    self.config._write_config(raw)
        state = "on" if enabled else "off"
        return (
            f"Mid-turn input preemption turned {state} — inbound messages "
            f"{'can now cut into the running turn at the next safe point' if enabled else 'now queue until the running turn ends (the original behavior)'}."
        )

    def reload_active_model(self, new_ref: str) -> None:
        """Reload runtime state after the active model is switched.

        Rebuilds the LLM client, updates the agent loop's model-specific
        settings, and refreshes the history system prompt so the
        next turn uses the new model.
        """
        old_ref = self.config.active_model_ref
        self.config.active_model_ref = new_ref
        model = self.config.active_model

        logger.info(
            "model_reloaded from=%s to=%s display=%s",
            old_ref, new_ref, model.display_name,
        )

        # Rebuild LLM client with the new model config
        self.llm_client = LLMClient(model)

        # Update agent loop with new model's capabilities
        self.agent_loop.llm_client = self.llm_client
        self.agent_loop.supports_vision = model.supports_vision
        self.agent_loop.model_name = model.display_name
        self.agent_loop.input_modalities = ", ".join(model.input_modalities)
        self.agent_loop.context_window = model.context_window
        # The live tool-result cap is computed from the model's window — it
        # must track the switch or a 128K→32K switch leaves the old cap in
        # place (240% of the new window) and oversized results overflow.
        self.agent_loop.max_tool_result_chars = int(
            self.config.tool_result_ceiling * model.context_window * 3
        )
        # Context-usage state is deliberately left untouched by a model
        # switch: context_tokens_for always reports the last API call's
        # real prompt+completion (or, on a freshly restored session, the
        # previous turn's persisted context_tokens that restore_session
        # primed into _last_usage).  Wiping either on a switch made the
        # first _turn_prompt after a restart-with-model-restore (cc-switch
        # restoring the recorded active model before the first turn)
        # report "Context usage: 0" even though the exit context WAS
        # restored.  The reading self-corrects on the next API call
        # anyway, so the switch stays a no-op here.

        # Rebuild system prompt with updated model info — for the human
        # history AND every persistent one (WeChat) and future ones.
        self.refresh_system_prompt()

        # Re-record the health fact: the startup record described the model
        # this session began with, and the report's ``model`` line is the
        # only place the model describes itself.  ``replace=True`` inside the
        # recorder keeps it a single entry.
        from slife.health import record_active_model
        record_active_model(model)

    @property
    def mcp_enabled(self) -> bool:
        """Whether the MCP gateway plugin is connected (its tools active)."""
        lc = self._gateway_lifecycle()
        return lc is not None and lc.client is not None and lc.client.is_connected

    @property
    def a2a_enabled(self) -> bool:
        """Whether the A2A P2P mesh is active (a2a plugin connected)."""
        client = self._plugins["a2a"].client
        return client is not None and client.is_connected

    @property
    def subagent_manager(self):
        """The SubagentManager, if A2A is enabled and subagent support is active."""
        return self._subagent_manager

    def clear(self) -> None:
        """Reset the message history and session usage."""
        self.message_history.clear()
        self.session_usage = TokenUsage()

    # ── Plugin auto-discovery & lifecycle ───────────────────────────────

    async def start_plugin_server(
        self, name: str, module: str,
    ) -> PluginStartStatus:
        """Spawn a plugin child process, connect, and register its tools.

        Single entry point for ALL plugins — built-in (mcp, memdb,
        wechat) and third-party.  The plugin's ``server.py`` must expose
        a ``main()`` that calls ``run_plugin_server(mcp)``.

        Dispatches internally for MCP (configurable wrapper command) and
        WeChat (poll loop); everything else uses the generic spawn path.
        Returns ``PluginStartStatus.STARTED`` on success, ``SKIPPED`` for
        expected no-ops (not configured / dependency absent — e.g. a2a
        without a running MQTT broker), ``FAILED`` on controlled failure,
        and raises on unexpected errors.

        Tracks startup convergence: this call is one of the plugin-start
        batch; when every attempted spawn has returned (ready, skipped, or
        failed) the ``_startup_settled`` event fires — the service is then
        open for user input.
        """
        self._startup_plugins.add(name)
        try:
            # Hang guard on the spawn await — convergence must fire even for
            # a stuck child, so the service can still open.  Not a
            # readiness deadline: the normal path settles as fast as the
            # real spawn, no timing guess involved.
            try:
                async with asyncio.timeout(_timeouts.timeouts.ready.plugin_start):
                    return await self._start_plugin_server_impl(name, module)
            except TimeoutError:
                # Log BEFORE it propagates: the guard sits outside the impl's
                # own try/except, and the UI is what catches the TimeoutError,
                # so without this line a child stuck past the guard leaves no
                # trace in the session log at all (only "… ({name}): " in the
                # TUI) — the one failure that most needs a trail.
                logger.warning(
                    "plugin_start_timeout name=%s after=%.0fs",
                    name, _timeouts.timeouts.ready.plugin_start,
                )
                raise
        finally:
            self._startup_plugins.discard(name)
            if not self._startup_plugins:
                self._startup_settled.set()

    async def _start_plugin_server_impl(
        self, name: str, module: str | None = None, *, allow_gate: bool = True,
    ) -> PluginStartStatus:
        """Uniformly start a child plugin by registry name.

        The per-plugin contract — module, enable gate, ToolContext client
        re-point, after-ready glue — lives in the spec and is resolved through
        the registry.  There is no per-plugin name branch here: every plugin
        (gateway, wechat, a2a, memdb, an auto-discovered package) runs the
        same path.  ``allow_gate=False`` is the watchdog restart path — a
        plugin that already passed its gate once is respawned without
        re-probing its gate.
        """
        spec = self._registry.spec(name, module)
        lc = self._registry.ensure(name, module)
        if lc.process is not None:
            return PluginStartStatus.STARTED  # idempotent — already running

        bhv = self._plugin_behaviors.get(name)
        if allow_gate and bhv is not None and bhv.enable is not None:
            if not await bhv.enable():
                logger.info("plugin_skipped name=%s", name)
                # A plugin that does not start owns no tools: drop its rows, or
                # tool_search keeps offering tools that cannot run until the
                # last session's file is replaced.
                if self._catalog is not None:
                    try:
                        await self._catalog.purge_source(name)
                    except Exception as e:
                        logger.debug("catalog_skipped_purge_failed name=%s err=%s", name, e)
                return PluginStartStatus.SKIPPED

        try:
            return await self._start_plugin_uniform(spec, lc)
        except Exception as e:
            logger.warning("plugin_start_failed name=%s err=%s", name, e)
            return PluginStartStatus.FAILED

    async def _start_plugin_uniform(
        self, spec, lc,
    ) -> PluginStartStatus:
        """Spawn *spec*'s child, register its tools, apply the ctx re-point,
        run the after-ready glue and arm the watchdog — the single path every
        plugin's initial start and watchdog restart go through."""
        lc.cancel_tasks()  # a restart never stacks a stale poll/drain loop
        started = await self._spawn_plugin_generic(spec.name, spec.module)
        if not started:
            return PluginStartStatus.FAILED
        if spec.ctx_field is not None:
            # Expose the plugin's live client where its health checks and
            # checks read it.
            setattr(self._tool_ctx, spec.ctx_field, lc.client)
        bhv = self._plugin_behaviors.get(spec.name)
        if bhv is not None and bhv.after_ready is not None:
            await bhv.after_ready(lc)
        if not spec.gateway:
            # Second edge of the gateway↔jobs port handshake (the gateway's
            # own after-ready glue is the first): push on every OTHER
            # plugin's ready, so whichever side readies last completes it.
            # Spawn order used to decide this — job-coding readied ~0.5s
            # after the gateway and silently lost the port, and a job-coding
            # watchdog restart re-opened the same hole.  Guarded no-op all
            # the way down (no gateway port / no live jobs client / an
            # unchanged port), so the extra calls cost one round trip.
            await self._push_gateway_port_to_jobs(self._gateway_lifecycle())
        self._arm_watchdog(spec, lc)
        return PluginStartStatus.STARTED

    def _arm_watchdog(self, spec, lc) -> None:
        """Attach the uniform crash watchdog to *lc* (replaces the old
        name-keyed ``_start_generic_watchdog``).

        Restart re-runs the whole uniform start — spawn, ctx re-point,
        after-ready glue — then tells every subagent sharing this plugin its
        new port.  ``_arm_watchdog`` itself is only called from the owning
        (main-agent) start path; subagents connect over HTTP and never own a
        child process.
        """
        async def _restart() -> None:
            await self._start_plugin_server_impl(
                spec.name, spec.module, allow_gate=False,
            )
            await self._notify_subagents_plugin_restart(spec.name, lc.port)

        lc.start_watchdog(restart_cb=_restart)

    # ── Plugin behavior methods (bound from the spec) ──────────────────
    # Enable gates and after-ready glue for plugins whose start needs more
    # than spawn+register.  Each is named by its plugin's PluginSpec and bound
    # in __init__ — the engine above never switches on a plugin name.

    async def _gate_wechat(self) -> bool:
        """WeChat starts only when enabled in config (else SKIPPED)."""
        wcfg = self.config.wechat_config
        if wcfg is None or not wcfg.enabled:
            logger.debug("wechat_not_enabled")
            return False
        return True

    async def _after_ready_wechat(self, lc) -> None:
        """After a wechat child is ready: run its best-effort session restore
        and start the inbound-message poll loop."""
        lc.restore_task = asyncio.create_task(self._wechat_restore_session())
        lc.poll_task = asyncio.create_task(self._wechat_poll_loop())
        # Component name == the live check's component (``check_wechat``) so the
        # merge layer can supersede this record by (component, key) — one
        # component name per subsystem, no alias table.
        from slife.health import record
        record("wechat", "ok", key="status", value="plugin started")

    async def _wechat_restore_session(self) -> None:
        """Best-effort session restore (triggers the server-side poll loop).

        Runs as a supervised background task — never awaited in the startup
        path — so a slow or hung iLink endpoint degrades (the restore is
        re-triggerable any time via the wechat_check_status tool) instead of
        stalling startup.  Reads the client at call time so the same helper
        serves both startup and watchdog-restart paths.
        """
        client = self._plugins["wechat"].client
        if client is None:
            return
        try:
            await client.call_tool("wechat_check_status", {})
            logger.debug("wechat_auto_restore_triggered")
        except Exception as e:
            logger.debug("wechat_restore_failed err=%r", e)

    async def _gate_a2a(self) -> bool:
        """A2A starts only when configured AND the Mosquitto broker answers a
        TCP probe (else SKIPPED — expected when the broker isn't running).

        On success the a2a config is serialized into the env for the child.
        Runs only on the initial start (allow_gate=False on restarts).
        """
        a2a_cfg = self.config.a2a_config
        if a2a_cfg is None or not a2a_cfg.enabled:
            logger.debug("a2a_disabled")
            return False

        # Only the MQTT transport binding is implemented.  A config that
        # somehow carries a different transport must not silently run MQTT.
        if a2a_cfg.transport != "mqtt":
            logger.warning(
                "a2a_transport_unsupported transport=%s action=a2a_disabled "
                "supported=('mqtt',)",
                a2a_cfg.transport,
            )
            return False

        from slife.a2a.broker import probe_broker
        if not await probe_broker(a2a_cfg.broker_host, a2a_cfg.broker_port):
            logger.info(
                "a2a_broker_not_found host=%s port=%d action=a2a_disabled",
                a2a_cfg.broker_host, a2a_cfg.broker_port,
            )
            a2a_cfg.enabled = False
            return False
        a2a_cfg.enabled = True

        # Pass the a2a config to the plugin process via env.
        from dataclasses import asdict
        os.environ["SLIFE_A2A_CONFIG"] = json.dumps(
            asdict(a2a_cfg), ensure_ascii=False,
        )
        return True

    async def _after_ready_a2a(self, lc) -> None:
        """After an a2a child is ready: start the drain loop that feeds
        inbound tasks/presence into the inbox."""
        lc.poll_task = asyncio.create_task(self._a2a_poll_loop())
        # The live ``check_a2a`` reports the same (component, key) on every
        # ``system_health`` call, so this record is the startup evidence that
        # the plugin did start; the merge layer drops it once the live entry
        # covers it.
        from slife.health import record
        record("a2a", "ok", key="status", value="plugin started")
        logger.info("a2a_plugin_started")

    async def _after_ready_sharefile(self, lc) -> None:
        """After a sharefile child is ready: watch its eager ngrok tunnel
        attempt and surface "tunnel down" on a terminal failure.  (The child's
        port env is published by the generic spawn on start AND every restart,
        so subagents always inherit the live port — nothing to do here.)"""
        self._watch_sharefile_tunnel(lc)

    async def _after_ready_mcp(self, lc) -> None:
        """After the gateway child is ready: wire the mcp enrichment (expose
        the wrapper client, register external-server tool proxies).

        The WIRING is immediate; the SYNC IS DISPATCHED, never awaited here.
        The gateway is ready when its own MCP handshake completes — its
        external servers are the gateway's business, and they come up in the
        background (its lifespan schedules auto-connect and returns for
        exactly this reason).  ``_wire_mcp_glue`` reads ``mcp_list`` /
        ``__check`` and then lists every configured server's tools, which for
        a peer the pool holds nothing for is itself the spawn.  Awaiting that
        put the whole external mirror inside the spawn await, which the spawn
        hang guard wraps: a slow server then turned a READY gateway into
        "⚠ plugin start failed" once the guard expired, a message that says
        nothing about why.
        """
        client = lc.client
        # Ordering-sensitive and cheap: expose the client before a health
        # check can read a stale one, and arm the notification handler before
        # the first server's ``tools/list_changed`` can arrive (the gateway
        # starts connecting the moment it serves).
        self._tool_ctx.mcp_client = client
        if client is not None:
            client.on_notification = self._on_mcp_tools_changed
        # Reaped by ``cancel_tasks()``: a restart cancels the stale sync
        # before re-arming, and shutdown takes it down with the lifecycle.
        lc.extra_task = asyncio.create_task(self._wire_mcp_glue())

    # ══ Runtime tool-set resync ══════════════════════════════════════
    # Unified mechanism for plugins that mutate their own tool set at
    # runtime (job-coding registers/removes job tools on the fly): the
    # plugin pushes the standard MCP ``notifications/tools/list_changed``
    # after a mutation; this re-lists that plugin's tools and diff-registers
    # the proxy set.  A plugin that never emits the notification is never
    # resynced — a pure superset of the spawn-time registration.

    def _plugin_tools_changed_handler(
        self, name: str,
    ):
        """Return the ``on_notification`` handler for *name*'s client."""
        async def _handler(method: str = "", _params=None, name=name) -> None:
            if method and not method.endswith("tools/list_changed"):
                return
            try:
                await self._rescan_plugin_tools(name)
            except Exception:
                logger.debug(
                    "plugin_tools_changed_rescan_failed name=%s", name, exc_info=True,
                )
        return _handler

    def _register_plugin_proxies(self, proxy_tools: list) -> set[str]:
        """Register a plugin's proxy tools, minus the ones the config switched off.

        The switch is enforced HERE, at registration, for the reason the builtin
        factory enforces it there (``factory.create_tools_from_config`` skips
        them): a tool the config turned off must have no execution instance.
        Execution is gated on having an instance, never on load state, so an
        instance is the only thing that can make a switched-off tool
        uncallable — without this, a row saying ``disabled`` would be a status
        column lying about what the model can run.

        The ROW is still written, because the caller hands the whole list to
        ``sync_system_tools``: the tool stays searchable, and the model can
        recommend switching it on.  (``tools.yaml``'s ``plugin:`` section names
        bare tool names, which is what ``t.name`` is here.  Tool names are
        unique across plugins — the registry is name-keyed — so a skip logged
        by name needs no plugin to identify it.)

        Returns the names actually registered — ``registered_tools``, the set
        the watchdog unregisters from, so it holds what exists and nothing
        else.  A name that flipped to ``disabled`` between two passes therefore
        falls out of it, and the caller's stale diff takes its instance back.

        Args:
            proxy_tools: The plugin's full proxy list, switched-off included.
        """
        disabled = self.config.disabled_plugin
        registered = {t.name for t in proxy_tools if t.name not in disabled}
        skipped = {t.name for t in proxy_tools} - registered
        if skipped:
            logger.debug("plugin_tools_switched_off names=%s", sorted(skipped))
        for tool in proxy_tools:
            if tool.name in registered:
                self.tool_registry.register(tool)
        return registered

    async def _rescan_plugin_tools(self, name: str) -> None:
        """Re-list plugin *name*'s tools and diff the registry.

        Registers newly-appeared tools and unregisters vanished ones —
        the same full-diff contract as the mcp wrapper's reconcile, but
        for a plugin's bare-name tools (e.g. job-coding's per-job tools).
        Idempotent: tracking ``registered_tools`` makes repeats no-ops.
        """
        from slife.mcp.tool_adapter import create_proxy_tools

        lifecycle = self._plugins.get(name)
        if lifecycle is None or lifecycle.client is None or not lifecycle.client.is_connected:
            return
        client = lifecycle.client
        plugin_tools = await client.list_tools()
        tagged = [
            {**t, "server": name}
            for t in plugin_tools
            if not is_internal_tool(t.get("name", ""))
        ]
        proxy_tools = create_proxy_tools(client, tagged)
        old_names = set(lifecycle.registered_tools)
        new_names = {t.name for t in proxy_tools}
        registered = self._register_plugin_proxies(proxy_tools)
        for stale in old_names - registered:
            self.tool_registry.unregister(stale)
        lifecycle.registered_tools = registered
        if self.caps.catalog_owner and self._catalog is not None:
            # One sync for the plugin's whole tool set: rows go in, and a tool
            # it dropped loses its row (source-scoped, so only this plugin's).
            await self._catalog.sync_system_tools(proxy_tools, source=name)
            await self._catalog.mark_plugin_connected(name)
        logger.debug(
            "plugin_tools_resync name=%s added=%d removed=%d total=%d",
            name, len(registered - old_names), len(old_names - registered),
            len(new_names),
        )

    def _watch_sharefile_tunnel(self, lc) -> None:
        """After sharefile loads, watch its eager tunnel attempt to settle and
        surface a TUI message when the tunnel is down.

        The harness owns the surfacing (main-process side); the plugin never
        talks to the TUI.  The tunnel is reported at most once, when it
        reaches a terminal ``failed`` state — a live tunnel stays silent.
        """
        client = self._plugins["sharefile"].client
        if client is None:
            return
        # Supervise the probe on the lifecycle's extra_task slot so a stop /
        # watchdog restart reaps it with the other background tasks — a
        # fire-and-forget create_task would keep sleeping past shutdown.
        if lc.extra_task is not None and not lc.extra_task.done():
            lc.extra_task.cancel()
        lc.extra_task = asyncio.create_task(self._check_sharefile_tunnel(client))

    async def _check_sharefile_tunnel(self, client) -> None:
        """Probe ``__check`` until the eager attempt concludes, then
        report once if the tunnel failed.

        The plugin eager-starts the tunnel on a background task, so a single
        probe at ready-time would race and misread ``starting`` as down.  We
        follow the attempt to its terminal state (``active`` / ``failed``),
        bounded by the registry's ``ready.tunnel_settle`` — an unresolved
        state within the window stays silent rather than guessing.
        """
        deadline = _time.monotonic() + _timeouts.timeouts.ready.tunnel_settle
        while True:
            try:
                raw = await client.call_tool("__check")
                data = json.loads(raw)
            except Exception as e:
                logger.warning("tunnel_status_probe_failed err=%s", e)
                return  # plugin gone/unreachable — nothing to surface
            if data.get("active"):
                return  # tunnel is up — nothing to surface
            if data.get("state") == "failed":
                self._report_tunnel_down(
                    data.get("reason") or "", data.get("provider") or ""
                )
                return
            if _time.monotonic() >= deadline:
                return  # never reached a terminal state — stay silent
            await asyncio.sleep(_TUNNEL_PROBE_INTERVAL)

    def _report_tunnel_down(self, detail: str, provider: str = "") -> None:
        """Log the failure and surface a one-line warning to the TUI.

        The warning names the active provider and carries its own reason: the
        cause is nearly always actionable and provider-specific (a missing
        ``NGROK_AUTHTOKEN``, an absent ``ssh``/``cloudflared`` binary), so a
        bare "tunnel unavailable" would send the user to ``system_health`` for
        something the harness already knows.  ``detail`` is condensed by
        :func:`_short_reason` — provider reasons are written for the log and
        can embed an install URL — and the line stops there: the consequence
        ("share_file stops working") is what "tunnel unavailable" already
        means, and the full reason is one ``system_health`` away.

        The text is localized here, not in the TUI — the harness owns the
        user-facing message, and the TUI only renders what it is handed.
        """
        logger.warning("tunnel_unavailable provider=%s detail=%s", provider, detail)
        cb = self._on_tunnel_down
        if cb is not None:
            try:
                # Drop the sentence's own terminator — the line ends with it.
                reason = _short_reason(detail).rstrip("。.")
                if reason:
                    cb(t("tunnel_down", provider=provider, reason=reason))
                else:
                    cb(t("tunnel_down_bare", provider=provider))
            except Exception:
                logger.debug("surface_tunnel_down_error", exc_info=True)

    async def _spawn_plugin_generic(self, name: str, module: str) -> bool:
        """Spawn a plugin child, connect, and register its ``<name>__*`` tools."""
        from slife.plugins.mcp_gateway.process import MCPWrapperProcess

        logger.info("plugin_spawn name=%s module=%s", name, module)

        # Auto-discovered third-party plugins get a PluginLifecycle too, so the
        # crash watchdog and shutdown manage them exactly like the built-ins
        if name not in self._plugins:
            from slife.agent.plugins import PluginLifecycle
            self._plugins[name] = PluginLifecycle(name, self)

        # The external-plugin log contract: the child inherits this env var
        # (alongside SLIFE_LOG_DIR / SLIFE_SESSION_ID / SLIFE_AGENT_NAME) and
        # names its per-session log file with it — so logs follow slife's
        # convention even for plugins that cannot import slife.  Spawns are
        # sequential, so the shared env var is safe.
        os.environ["SLIFE_PLUGIN_NAME"] = name

        process = MCPWrapperProcess(
            command=sys.executable,
            args=["-m", module],
        )
        try:
            await process.start()
            from slife.agent.plugins import client_info_extra_for
            client = await process.create_client(
                tool_timeout=self.config.tool_timeout,
                client_info_extra=client_info_extra_for(name),
            )

            # Discover tools — retry once on a timeout.  A Streamable HTTP
            # session established in the plugin's "signalled but not yet
            # serving" window can hang on Windows/Proactor (memdb's slow
            # lifespan makes this the likeliest).  By the retry the plugin is
            # definitely serving, so a fresh session succeeds — the race
            # self-heals instead of failing the load.
            try:
                plugin_tools = await client.list_tools()
            except TimeoutError:
                logger.warning("plugin_tools_timeout_retry name=%s", name)
                await client.disconnect()
                client = await process.create_client(
                    tool_timeout=self.config.tool_timeout,
                    client_info_extra=client_info_extra_for(name),
                )
                plugin_tools = await client.list_tools()
            logger.debug("plugin_tools name=%s count=%d names=%s",
                         name, len(plugin_tools),
                         [t["name"] for t in plugin_tools])

            # Register as proxy tools — filter out plugin internal tools.
            # Canonical marker: a plugin tool named ``__*`` (double underscore)
            # is internal — called programmatically via call_tool(), never
            # exposed to the LLM.  (Single ``_`` = harness but LLM-visible,
            # e.g. the builtin `_turn_prompt`.)
            tagged = [
                {**t, "server": name}
                for t in plugin_tools
                if not is_internal_tool(t.get("name", ""))
            ]
            if len(tagged) < len(plugin_tools):
                logger.debug(
                    "plugin_tools_filtered name=%s kept=%d dropped=%d",
                    name, len(tagged), len(plugin_tools) - len(tagged),
                )
            proxy_tools = create_proxy_tools(client, tagged)
            # Record exact registered names for dead-process cleanup / stop
            # (bare names — no {name}__ prefix to unregister by).
            self._plugins[name].registered_tools = self._register_plugin_proxies(
                proxy_tools,
            )
            # …and mirror them into the shared catalog.  Without this the
            # plugin's tools have no row, and a row is what makes a tool
            # SEARCHABLE and injectable (the per-turn set is the catalog's
            # loaded set, not the registry) — the spawn path is the one every
            # plugin actually takes, so a mirror confined to the rescan and
            # HTTP-connect twins left them invisible to the model.
            if self.caps.catalog_owner and self._catalog is not None:
                # Best-effort by contract, and it MUST NOT escape: this block
                # sits inside the spawn's ``except BaseException`` handler,
                # which stops the child — a catalog hiccup would otherwise kill
                # a plugin that just came up healthy.
                try:
                    # One sync for the plugin's whole tool set (rows in, a
                    # dropped tool's row out), then clear the ``error`` mark its
                    # exit left — the child is serving again.
                    await self._catalog.sync_system_tools(proxy_tools, source=name)
                    await self._catalog.mark_plugin_connected(name)
                except Exception as e:
                    logger.debug("plugin_catalog_sync_failed name=%s err=%s", name, e)

            logger.info("plugin_ready name=%s tools=%d",
                         name, len(self.tool_registry.list_tools()))

            # Store for cleanup/watchdog — PluginLifecycle for all plugins
            # (built-in or auto-discovered third-party).
            self._plugins[name].client = client
            self._plugins[name].process = process
            self._plugins[name].port = process.port
            # Live tool-set changes (job-coding registers/removes job tools
            # at runtime): subscribe the generic rescan to the standard MCP
            # ``notifications/tools/list_changed`` the plugin pushes after a
            # mutation.  The mcp wrapper overrides this handler in
            # ``_wire_mcp_glue`` with its own reconcile — every other
            # plugin uses the generic diff.
            client.on_notification = self._plugin_tools_changed_handler(name)
            # Save the module for the watchdog's fallback restart path (no
            # restart_cb — same contract as PluginLifecycle.spawn).
            self._plugins[name]._module = module
            os.environ[plugin_port_env(name)] = str(process.port)

            # Readiness (MCP plugin contract): create_client() negotiated
            # the peer's era — completing that exchange is the plugin's ready
            # declaration; record it.
            self._plugins[name].mark_initialized()

            return True
        except BaseException:
            # A failed spawn must not leave the lifecycle pointing at a
            # live-but-unconnected child (watchdog stall) or an orphaned
            # process (leak) — reset and stop it before re-raising.
            # BaseException (not just Exception) so a cancellation from the
            # app's required-plugin timeout also stops the child instead of
            # orphaning it.
            self._plugins[name].process = None
            self._plugins[name].client = None
            self._plugins[name].port = 0
            try:
                await process.stop()
            except Exception:
                pass
            raise

    # ── MCP enrichment adapter ─────────────────────────────────────────
    # The mcp gateway self-hosts its config (tools.yaml) and self-connects
    # servers on startup.  This is the ONE bounded, mcp-aware integration
    # left in the harness: expose
    # the wrapper client to slife tools and register the external servers'
    # ``{server}__{tool}`` proxies as system tools.  Persistence, auto-connect
    # and reconciliation happen inside the plugin, not here.  The lifecycle
    # itself (spawn / connect / watchdog) is the generic one all plugins use.

    async def _wire_mcp_glue(self) -> None:
        """Wire the gateway enrichment after (re)connect: client + tool proxies.

        Idempotent — re-arming after a watchdog respawn re-points the tool
        context and re-registers the external servers' tools.

        Runs as the gateway lifecycle's background task (see
        :meth:`_after_ready_mcp`), so it never holds the plugin start open.
        The body is best-effort end to end and must never raise: an escaping
        error would surface only as an unretrieved task exception.
        """
        try:
            lc = self._gateway_lifecycle()
            if lc is None:
                return
            client = lc.client
            self._tool_ctx.mcp_client = client
            if client is not None:
                client.on_notification = self._on_mcp_tools_changed
            await self._sync_mcp_proxies()
            # Jobs reach external MCP servers via the gateway's persistent
            # connections; keep their lazy ``mcp`` handle pointed at the live
            # port (the push lands on every gateway connect, restart included).
            await self._push_gateway_port_to_jobs(lc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("mcp_glue_failed", exc_info=True)

    async def _push_gateway_port_to_jobs(self, lc) -> None:
        """Best-effort push of the gateway's port to the job-coding plugin.

        job-coding's ``mcp`` handle resolves the gateway port lazily, but a
        child's spawn-time env snapshot never changes — the gateway's port is
        not known when its siblings spawn, and a gateway restarted on a new
        port would leave live jobs pointing at the old endpoint.  Pushing
        through the internal tool is the only live path, so it is evaluated
        from BOTH edges of the handshake (``_wire_mcp_glue`` on the gateway's
        ready, ``_start_plugin_uniform`` on every other plugin's) and is
        idempotent at the far end (``set_port`` no-ops on an unchanged port).
        Non-fatal by design: a dead client just logs.
        """
        jobs = self._plugins.get("job-coding")
        client = getattr(jobs, "client", None)
        if client is None or not getattr(client, "is_connected", False):
            return
        port = getattr(lc, "port", 0)
        if not port:
            return
        try:
            raw = await client.call_tool("__set_mcp_gateway_port", {"port": port})
            logger.debug("job_coding_gateway_port_pushed port=%s raw=%s", port, raw)
        except Exception:
            logger.debug("job_coding_gateway_push_failed", exc_info=True)

    async def _on_mcp_tools_changed(self, method: str = "", _params=None) -> None:
        """A ``notifications/tools/list_changed`` from the wrapper: re-sync.

        Preserves the mid-session reconnect tool-sync (no periodic poll) —
        the wrapper fires this whenever a server (re)connects; full-diff
        registration on the agent side keeps it idempotent.
        """
        if method and not method.endswith("tools/list_changed"):
            return
        try:
            await self._sync_mcp_proxies()
        except Exception:
            logger.debug("mcp_tools_changed_sync_failed", exc_info=True)

    async def _sync_mcp_proxies(self) -> None:
        """Reconcile external MCP proxies + the catalog's tool rows.

        Reads the configured server list LIVE from the wrapper
        (``__mcp_list`` — both families; the model's ``mcp_list`` is scoped to
        one), so neither slife nor a subagent needs tools.yaml:

        1. **Connectivity verdict** (main agent): ``__check`` says which servers
           are up; each one's tools are marked ``error`` or cleared —
           ``_mark_server_connectivity``.  This is the only place liveness
           reaches the injection set (there is no server table to join).
        2. **enabled servers**: their proxies are (re)registered and their
           tool rows mirrored — ``_discover_and_register_external_tools``.
           ``auto_load`` does not enter here: it decides which tools the catalog
           seeds as *loaded* (and so what the turn snapshot injects), not which
           ones can execute.  A **disabled** server is not mirrored at all: its
           rows persist from when it was enabled (the switch, projected onto
           them by step 1, is what reports them ``disabled``), and mirroring it
           would mean reading its tool list — which IS connecting it.
        3. A proxy whose server left the CONFIG is unregistered; a merely
           disconnected/disabled server keeps its proxies — its rows carry the
           ``error``/``disabled`` mark, which is what keeps them out of
           injection.
        3b. **Rows of a server that left tools.yaml are purged** (the §8.5
           "remove 清理干净" contract), compared against the config rather than
           the pool so a gateway restart never wipes a configured server.

        New rows land ``unloaded`` (the register-never-loads rule); an existing
        row keeps whatever the model decided.
        """
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

        lc = self._gateway_lifecycle()
        client = lc.client if lc is not None else None
        if client is None or not client.is_connected:
            return
        # One pass at a time — but not "one pass forever".  Every await below
        # is bounded by ``ready.tool_sync_wait``, so a pass still holding the
        # guard after that budget is not slow, it is wedged (a bound the loop
        # never got to run — the gateway froze, or a cancellation was swallowed
        # under it).  Abandoning it is what keeps one stuck server from
        # disabling the mid-session sync for the rest of the process, which is
        # exactly what a bare latch did.
        owner = object()
        if self._mcp_reconcile_owner is not None:
            held = _time.monotonic() - (self._mcp_reconcile_started_at or 0.0)
            if held < _timeouts.timeouts.ready.tool_sync_wait:
                return
            logger.warning(
                "mcp_reconcile_stale_pass held=%.0fs — taking over",
                held,
            )
        self._mcp_reconcile_owner = owner
        # Timing: this pass is what decides when the agent can actually call
        # things, and on a cold start it is the long wait.  The op window is
        # NOT armed here — it opened with the catalog (``_init_catalog``) so
        # that it spans the startup rather than this pass's slice of it, and
        # ``_report_tool_sync`` reads it out, whether or not this is the pass
        # that converges.
        started = _time.monotonic()
        self._mcp_reconcile_started_at = started
        if self._tool_sync_started_at is None:
            self._tool_sync_started_at = started
        #: Enabled servers that have not answered a ``tools/list`` yet —
        #: filled in by the post-mirror projection below, the first look that
        #: can tell a slow server from an absent one.
        pending: set[str] = set()
        failure = ""
        try:
            try:
                # __mcp_list, not mcp_list: the model's listing is scoped to
                # its own family, and the reconcile mirrors EVERY configured
                # server.  Using the filtered one dropped the REST APIs from
                # the pass — no catalog rows, so tool_search and func_tool_load
                # could not reach 1271 endpoints that were connected all along.
                #
                # Bounded at the call site, where the await is: the client
                # deliberately sets no timer of its own (its contract pushes
                # that to whoever awaits), and an unbounded read here is what
                # used to hold this pass open forever.
                async with asyncio.timeout(_timeouts.timeouts.ready.tool_sync_wait):
                    raw = await client.call_tool("__mcp_list")
                servers = json.loads(raw)
            except TimeoutError:
                logger.warning(
                    "mcp_reconcile_list_timeout budget=%.0fs",
                    _timeouts.timeouts.ready.tool_sync_wait,
                )
                servers = []
            except Exception as e:
                logger.debug("mcp_reconcile_list_failed err=%s", e)
                servers = []

            configured: set[str] = set()
            #: The config's on/off switch per server — the config arm of the
            #: ``status`` column, which moves independently of the liveness
            #: verdict (the same column's runtime arm).
            enabled_servers: dict[str, bool] = {}
            if isinstance(servers, list):
                for s in servers:
                    if not (isinstance(s, dict) and s.get("name")):
                        continue
                    configured.add(s["name"])
                    enabled_servers[s["name"]] = s.get("enabled") is not False

            # 1 — the state of every configured server, projected onto its tool
            # rows: the on/off switch and the liveness verdict, each into its
            # own lane of the ``status`` column.  There is no server table to
            # carry either — the row itself is where "switched off" and "not
            # up" live now.
            if self.caps.catalog_owner and self._catalog is not None:
                await self._mark_server_connectivity(
                    client, configured, enabled_servers,
                )

            # 2/2b — every configured server's rows, off ONE pass over the
            # servers.  Every ENABLED server also gets its proxies registered:
            # the registry is the execution pool, so a tool the catalog calls
            # ``loaded`` must have an instance behind it.  ``auto_load`` governs
            # what the catalog SEEDS as loaded — and so what the turn snapshot
            # injects — never whether an execution route exists.  Registering an
            # on-demand server's proxies does not inject them: injection is the
            # snapshot's job, and it is driven by ``load_status`` alone.
            #
            # A disabled server is NOT mirrored: ``enabled: false`` means
            # "stays configured but is not connected" (tools.yaml), and asking
            # for its tool list is what connects it.  Its rows persist from
            # whenever it was last enabled — step 1's switch projection puts
            # ``disabled`` on them — so ``mcp_set_enabled`` still restores a
            # catalog it can inject from, without the process ever running.
            #
            # CONCURRENTLY: each of these awaits a ``__mcp_list_tools``, i.e.
            # a real ``tools/list`` on the far side — and, for a peer the pool
            # holds nothing for, the SPAWN that makes one possible.  Sequential,
            # every server waited on all the servers before it; twenty of them
            # made a cold reconcile a minute-plus of pure queueing.  One
            # server's failure still never sinks the pass.
            async def _mirror(name: str, coro, tag: str) -> None:
                """One server's mirror, bounded.

                A mirror is a real ``tools/list`` on the far side — and, for a
                peer the pool holds nothing for, the SPAWN that makes one
                possible — so it can outlive any patience worth having.  The
                bound is what makes the pass end (it is ``gather``ed, so the
                slowest branch is the pass's duration), and the pass ending is
                what makes the tool-set line reachable at all.
                """
                try:
                    async with asyncio.timeout(
                        _timeouts.timeouts.ready.tool_sync_wait,
                    ):
                        await coro
                except TimeoutError:
                    # Its own line: "one server never answered" is the case the
                    # next diagnosis should read off the log rather than infer
                    # from a response that never shows up.  The server keeps
                    # whatever rows it had; a later pass re-reads it.
                    logger.warning("mcp_sync_timeout server=%s", name)
                except Exception:
                    logger.debug("%s server=%s", tag, name, exc_info=True)

            enabled_configured = [
                n for n in sorted(configured) if enabled_servers.get(n, True)
            ]
            await asyncio.gather(
                *(
                    _mirror(n, self._discover_and_register_external_tools(server_name=n),
                            "mcp_sync_failed")
                    for n in enabled_configured
                ),
            )

            # Re-project the verdict: the mirrors above are what ASK for the
            # tool lists (a boot that only spawns holds none yet), so every
            # peer was still `error` when step 1 looked.  This second pass sees
            # the lists the mirrors just produced — one `__check` plus the
            # guarded per-server write, which now costs nothing where nothing
            # moved.  Best-effort: a failed probe is not a verdict.
            if self.caps.catalog_owner and self._catalog is not None:
                try:
                    pending = await self._mark_server_connectivity(
                        client, configured, enabled_servers,
                    )
                except Exception as e:
                    logger.debug("mcp_connectivity_recheck_failed err=%s", e)
                    pending = set()

            # 3 — a proxy whose server left the CONFIG is dropped; a merely
            # disconnected/disabled server keeps it (its rows are `error`).
            for tool in list(self.tool_registry.list_tools()):
                if not (isinstance(tool, MCPProxyTool) and tool._route == ProxyRoute.EXTERNAL):
                    continue
                if getattr(tool, "_server", "") in configured:
                    continue
                if self.tool_registry.unregister(tool.name):
                    logger.debug("mcp_proxy_server_removed full_name=%s", tool.name)

            # 3b — live catalog cleanup: a server that left tools.yaml loses
            # its rows NOW (the §8.5 "remove 清理干净" contract — otherwise
            # rest_api_remove / mcp_remove leave stale rows until the next
            # startup purge).  Comparing against tools.yaml (the authority)
            # rather than the pool keeps a transient empty pool or a gateway
            # restart from wiping configured servers' rows.
            if self.caps.catalog_owner and self._catalog is not None:
                try:
                    from slife.plugins.mcp_gateway import config as _gw_cfg
                    await self._catalog.purge_unconfigured_sources(
                        set(_gw_cfg.servers()),
                    )
                except Exception as e:
                    logger.debug("catalog_live_purge_failed err=%s", e)
                # 3c — the same "tools.yaml is the authority" rule for the
                # families the registry cannot see: a hand-edited cli entry or
                # SKILL.md lands here, not at the next restart.  mtime-gated,
                # so an unchanged file costs two stats.
                await self._refresh_local_rows_if_changed(self._catalog)
        except Exception as e:
            failure = str(e)
            raise
        finally:
            # Only the pass that still owns the guard reports — an abandoned
            # one says nothing, because the pass that took over is the one that
            # speaks for the set now.  The guard is held THROUGH the report: a
            # pass is not done until its line is out.
            if self._mcp_reconcile_owner is owner:
                self._mcp_reconcile_owner = None
                self._mcp_reconcile_started_at = None
                await self._report_tool_sync(
                    started, pending=pending, failure=failure,
                )

    async def _report_tool_sync(
        self, started: float, *, pending: set[str], failure: str = "",
    ) -> None:
        """Tell the TUI the tool set is ready — once, and only once it has converged.

        Emitted on the first pass that has converged and NEVER again for this
        process: later passes ride the gateway's own ``tools/list_changed``
        cadence, so reporting each one would be a heartbeat rather than news.
        A failure always reports, converged or not — silence has to keep
        meaning "still syncing", or a dead sync reads exactly like a slow one.

        Convergence is "no enabled server is still starting".  ``pending``
        (from ``_mark_server_connectivity``) names the configured servers that
        have not answered a ``tools/list`` yet; while one remains the set is
        still arriving, and announcing it would both lie and make the pass
        that finally carries those tools look like a change to the tool set.

        Once ``ready.tool_sync_wait`` is spent, what the set HAS is the answer:
        the budget outlasts the gateway's own establishment and listing bounds,
        so a server still missing after it is not still arriving — it is down
        for now, and its rows already say so.  What makes that a real bound
        rather than a hope is that every await in the pass is bounded by the
        same value: the pass always ENDS, so this check is always reached.  A
        pass that never ended (an unbounded await on a gateway that stopped
        answering) is what used to leave this line unwritten for a whole
        session, while silence kept claiming the sync was merely slow.

        ``total`` is every USABLE catalog row — not the registry, which cannot
        see the two registry-less families: ``skill`` and ``cli`` are rows
        rather than instances (a skill IS its SKILL.md text), so a registry
        count under-reported what the user can reach.  What the model SEES in
        a turn is the ``load_status`` snapshot, a narrower set — so the line
        promises availability, never injection.

        The delta is what this process WROTE to the catalog since the catalog
        opened (``CatalogOpDelta`` — insert/update/delete), never a registry
        before/after: a cold start's registry begins empty, so a registry diff
        reads every tool as "added" and turns a restart into a change to the
        tool set.  The window belongs to the STARTUP, not to this pass — it is
        armed by ``_init_catalog`` before the boot seed, and a pass that stays
        quiet leaves it open so the counts keep accumulating.
        """
        if not failure and self._tool_sync_reported:
            return
        if not failure and pending and not self._tool_sync_wait_over():
            return
        self._tool_sync_reported = True
        # Take the window: it closes with the line that reports it, so nothing
        # a later pass writes can feed a line already sent.
        delta: "CatalogOpDelta | None" = (
            self._catalog.store.end_ops() if self._catalog is not None else None
        )
        # The wait the user was actually in: measured from this process's first
        # pass, not from this pass's start — the late pass is the short one.
        anchor = self._tool_sync_started_at or started
        # The catalog is the authority on what is usable, and the registry is
        # not: skill / cli rows are usable tools with no load state and no
        # instance to register, so counting instances under-reported them.  The
        # registry count stays as the degraded answer for a process whose
        # catalog failed to open (the line then says what it can).
        total = len(self.tool_registry.list_tools())
        if self._catalog is not None:
            try:
                total = await self._catalog.store.count_usable()
            except Exception:
                logger.debug("catalog_usable_count_failed", exc_info=True)
        try:
            await self._notify_activity(
                "tools_synced",
                seconds=round(_time.monotonic() - anchor, 1),
                total=total,
                added=delta.added if delta else 0,
                updated=delta.updated if delta else 0,
                removed=delta.removed if delta else 0,
                error=failure,
            )
        except Exception:
            pass  # a notification must never break the reconcile

    def _tool_sync_wait_over(self) -> bool:
        """Whether the startup sync's budget is spent.

        Two clocks a still-missing server could be running on, and this one
        budget covers both (see ``ready.tool_sync_wait``): the gateway's
        re-list backoff — a first listing that timed out can still succeed on
        the retry — and its establishment bound, which is what a server that
        has answered nothing at all is really inside.  Spent, a server still
        missing is not "still starting" but down for now, and the line reports
        what the set has rather than waiting on a server the gateway is done
        pacing.

        ``>=``, not ``>``: the pass's own bounds expire on exactly this value,
        so which side of a clock tick the pass lands on must not decide
        whether the line is written.
        """
        if self._tool_sync_started_at is None:
            return True
        budget = _timeouts.timeouts.ready.tool_sync_wait
        return (_time.monotonic() - self._tool_sync_started_at) >= budget

    async def on_plugin_child_exit(self, name: str) -> None:
        """A plugin child exited (its watchdog is about to restart it).

        If it was the mcp gateway, every external server it managed is now
        unreachable — mark ALL external tools ``error`` so they leave the
        injection set immediately (the restart's reconcile clears the mark as
        each server comes back).  Either way the plugin's OWN tools are
        unreachable, so mark them too, by source; ``mark_plugin_connected``
        clears that mark when the child is back.  Best-effort.
        """
        catalog = self._catalog
        if catalog is None or not self.caps.catalog_owner:
            return
        try:
            if name == "mcp-gateway":
                await catalog.mark_all_external_error()
            await catalog.mark_source_error(name)
        except Exception as e:
            logger.debug("catalog_plugin_down_mark_failed name=%s err=%s", name, e)

    async def _mark_server_connectivity(
        self, client, configured: set[str], enabled: dict[str, bool] | None = None,
    ) -> set[str]:
        """Project each configured server's state onto its tool rows.

        Two independent facts land here, each in its own lane of one column
        (``status``) — which is why neither can erase the other:

        - **the config switch** — the server's own on/off in tools.yaml.  A
          server switched off keeps its rows and reports ``disabled``; the
          write only crosses the switch line, so the model's loaded/unloaded
          decision survives the round trip (see ``set_source_enabled``).
        - **the liveness verdict** — from the wrapper's ``__check``:
          ``tools_ok`` means the server answered a ``tools/list`` and its
          result is still held.  A server that is not — down, never listed —
          has its tools marked ``error``; a working one has that mark cleared.

        A switched-off server gets no verdict at all: it is not down, so
        marking its tools ``error`` would be a lie the model could not tell
        from the real thing.

        Returns the **pending** set — the configured servers that are switched
        on, reachable, and have not answered a ``tools/list`` yet.  "No verdict"
        is not "no tools": a server that is still starting (a REST proxy
        installing its environment takes tens of seconds, and its first listing
        can even time out and succeed on the retry) has nothing to mirror yet,
        so the startup tool-set line waits for it rather than announce a set
        that is still arriving.

        Two shapes are NOT pending, because they are settled rather than
        undecided: a server whose transport never came up after the boot pass
        finished is ``error`` (a failed spawn is the verdict — waiting on it
        would hold the line for a server that is simply down, and the
        gateway's own retry re-syncs it if it ever does come up), and a server
        waiting on USER AUTH cannot come up without a human.  The first needs
        ``spawn_settled``: until the boot pass reports it, a row with no
        transport is a spawn still in flight, which is pending like any other
        server nobody has read yet.
        """
        catalog = self._catalog
        if catalog is None:
            return set()
        try:
            # Bounded like every other gateway await in the pass — this one is
            # not a mirror, but a probe that never answers holds the pass open
            # exactly the same way.
            async with asyncio.timeout(_timeouts.timeouts.ready.tool_sync_wait):
                raw = await client.call_tool("__check")
            data = json.loads(raw)
        except TimeoutError:
            logger.warning(
                "mcp_reconcile_check_timeout budget=%.0fs",
                _timeouts.timeouts.ready.tool_sync_wait,
            )
            return set()
        except Exception as e:
            # A failed probe is NOT a verdict — leave the rows alone rather
            # than marking every server broken on a transient error.  It is
            # also not something to wait on: no pending set, so the report
            # goes out rather than hanging on a probe that never answers.
            logger.debug("mcp_reconcile_check_failed err=%s", e)
            return set()
        live: set[str] = set()
        awaiting_auth: set[str] = set()
        unreachable: set[str] = set()
        rows = (data.get("servers") or []) if isinstance(data, dict) else []
        #: The boot pass has brought every configured server up (or failed to).
        #: Until it has, a server with no transport is one nobody has asked yet
        #: — the spawn is still in flight — so NOTHING may be called settled.
        spawn_settled = bool(
            data.get("spawn_settled") if isinstance(data, dict) else False
        )
        for s in rows:
            if not (isinstance(s, dict) and s.get("name")):
                continue
            if s.get("tools_ok"):
                live.add(s["name"])
            elif s.get("needs_user_auth"):
                awaiting_auth.add(s["name"])
            elif not s.get("reachable"):
                # No transport: the spawn failed or timed out.  Settled, not
                # pending — its tools are marked ``error`` below, and the
                # gateway's armed retry re-syncs it if it comes up later.
                unreachable.add(s["name"])
        switches = enabled or {}
        pending = {
            name for name in configured
            if switches.get(name, True) is not False
            and name not in live
            and name not in awaiting_auth
            # Only a finished spawn pass makes "no transport" a verdict.  A
            # server simply ABSENT from the payload stays pending either way.
            and not (spawn_settled and name in unreachable)
        }
        for name in sorted(configured):
            try:
                if switches.get(name, True) is False:
                    await catalog.set_source_enabled(name, False)
                    continue
                await catalog.set_source_enabled(name, True)
                if name in live:
                    await catalog.mark_server_connected(name)
                else:
                    await catalog.mark_source_error(name)
            except Exception as se:
                # One server's write failing (a transient lock) must never
                # crash the reconcile loop — the next pass retries.
                logger.debug("catalog_connectivity_mark_failed server=%s err=%s", name, se)
        return pending

    async def _upsert_external_catalog_rows(
        self, server_name: str, tools: list[dict], *, category: str,
    ) -> None:
        """Upsert a server's tool rows into the shared catalog.

        ``category`` (``mcp`` vs ``rest-api``) is decided by the CALLER from
        the server's ``tools.yaml`` entry — the provenance used to live in
        the server row, which no longer exists.  A schema text change drops
        the stale embedding (the drainer re-embeds); ``on_saved`` is the
        caller's job so a batch wakes the drainer once.  New rows land
        ``unloaded``: registering a tool never loads it.

        Each row also carries its server's on/off switch — tools.yaml is the
        authority for that column, and a mirrored row that omitted it would
        read as merely ``down`` when its server is in fact switched off.

        Upsert-then-purge over the server's WHOLE tool set, the same contract
        every other family's mirror follows: a tool this server no longer
        publishes loses its row (it already lost its registry proxy), or
        ``tool_search`` keeps offering one that cannot run and
        ``func_tool_load`` materializes a proxy with nothing behind it.  Both
        callers return early on an empty listing, which means "not ready yet"
        — never "owns nothing" — so a transient empty list cannot wipe a
        server's rows.

        The rows go over as ONE delta (:meth:`mirror_external_tools`): a
        per-tool upsert re-read the catalog's whole table for every tool, so a
        thousand-tool server cost a thousand scans per listing.
        """
        catalog = self._catalog
        if catalog is None:
            return
        try:
            from slife.plugins.mcp_gateway import config as _gw_cfg
            _entry = _gw_cfg.get_server(server_name) or {}
            server_enabled = _entry.get("enabled") is not False
        except Exception:
            # Unreadable config is not a reason to call a server disabled.
            server_enabled = True

        await catalog.mirror_external_tools(
            server_name, tools, category=category, enabled=server_enabled,
        )

    async def _register_external_server_tools(self, name: str = "", **kwargs) -> None:
        """mcp_set / mcp_set_enabled connected a server — reconcile proxies.

        (Persistence happens inside mcp-gateway; this only touches the registry.)
        """
        if name:
            await self._sync_mcp_proxies()

    async def _unregister_external_server_tools(self, name: str = "", **kwargs) -> None:
        """mcp_remove removed a server from config — drop its proxies AND its
        catalog rows.

        This is the ONLY catalog-deletion path for a server (disable keeps
        rows — the effective-status join expresses it; DESIGNER NOTES §8.5).
        """
        if not name:
            return
        removed = self.tool_registry.unregister_by_prefix(f"{name}__")
        if removed:
            logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)
        if self.caps.catalog_owner and self._catalog is not None:
            try:
                await self._catalog.purge_source(name)
            except Exception as e:
                logger.debug("catalog_server_remove_failed server=%s err=%s", name, e)

    async def _notify_subagents_plugin_restart(self, name: str, port: int) -> None:
        """Tell every live subagent a plugin restarted on a new port.

        Subagents share the main agent's plugins instead of spawning their
        own.  When the watchdog respawns a plugin, it lands on a fresh
        auto-assigned port; workers still holding the old session are dead
        until they reconnect.  This broadcasts the new port so they rebuild
        their client (``worker/plugin_restart`` in headless.py).  Best-effort.
        """
        if not port:
            return
        from slife.subagent.process import get_manager
        manager = get_manager()
        if manager is None:
            return
        logger.info("plugin_restart_notify_subagents plugin=%s port=%d", name, port)
        await manager.broadcast(
            "worker/plugin_restart", {"plugin": name, "port": port},
        )

    # ── HTTP-connect helpers (subagents share the main agent's plugins) ──

    async def _connect_plugin_http(self, name: str, port: int) -> None:
        """Connect to an already-running plugin via Streamable HTTP.

        Shared by :meth:`connect_plugin_http` (the single subagent connect
        path).  Wires the plugin's ``notifications/tools/list_changed`` into
        the runtime rescan, so a shared plugin's dynamic tool set (e.g.
        job-coding's per-job tools) stays live under a subagent — the same
        handler the spawn path uses.  The gateway's handler reconciles
        external ``{server}__{tool}`` proxies instead; every other plugin uses
        the generic rescan (the split is spec.gateway, not a name branch).
        """
        logger.info("%s_http_connect port=%s", name, port)
        spec = self._registry.spec(name)
        lc = self._plugins[name]
        await lc.connect_http(port)
        client = lc.client
        if client is not None:
            if spec.gateway:
                client.on_notification = self._on_mcp_tools_changed
            else:
                client.on_notification = self._plugin_tools_changed_handler(name)

    async def connect_plugin_http(self, name: str, port: int) -> None:
        """Connect to an already-running plugin via Streamable HTTP.

        The single generic path a subagent uses to share the main agent's
        plugins instead of spawning its own.  Registers the plugin's
        LLM-visible tools as bare-name proxy tools and re-points the
        ToolContext client the plugin's spec declares; a gateway worker also
        reconciles the external-server proxies.
        """
        spec = self._registry.spec(name)
        await self._connect_plugin_http(name, port)
        await self._register_plugin_tools(name)
        if spec.gateway:
            # Reconcile external {server}__{tool} proxies — registers auto_load
            # tools and mirrors on-demand servers' catalog rows (the
            # tool_search / func_tool_load surface).  Same network the main
            # agent's _wire_mcp_glue uses, mirrored for a worker sharing the
            # gateway.
            await self._sync_mcp_proxies()
        # NOT an ``elif``.  The gateway declares a ctx_field like every other
        # plugin, and a worker that skipped this kept ``mcp_client`` None while
        # holding a perfectly good connection — so ``system_health`` reported
        # "client not connected" for a gateway the worker had just connected,
        # and the parent disagreed from its own report of the same live state.
        # The docstring always said "also"; the branch said otherwise.
        if spec.ctx_field is not None:
            setattr(self._tool_ctx, spec.ctx_field, self._plugins[name].client)
        logger.info("%s_http_connect_done tools=%d", name, len(self.tool_registry.list_tools()))

    async def connect_shared_plugins(self) -> None:
        """Connect to every plugin another process started, by inherited port.

        The client half of plugin ownership: the parent publishes
        ``SLIFE_<NAME>_PORT`` for each plugin it spawned (``plugin_port_env``),
        and this process connects to each one as an MCP client — a manifest
        loop over ``discover_plugins()``, never a hard-coded subset.  A plugin
        the parent skipped (a2a with no broker, say) published no port and is
        skipped here too, so the two processes agree on which plugins exist
        without either one enumerating them.

        This is the counterpart to spawning: a worker owns no plugin child, so
        it also drains nothing inside them (the A2A inbound queue, the WeChat
        poll, the gateway's watchdog all live in the parent's processes).
        Failures are logged and skipped — one unreachable plugin must not cost
        the worker its other tools.
        """
        from slife.plugins import discover_plugins
        from slife.agent.plugins import plugin_port_env

        for name, _module in discover_plugins():
            port = os.environ.get(plugin_port_env(name), "")
            if not port:
                continue
            try:
                await self.connect_plugin_http(name, int(port))
            except Exception as e:
                logger.warning("%s_http_failed port=%s err=%s", name, port, e)

    async def _register_plugin_tools(self, name: str) -> None:
        """Discover and register a connected plugin's tools as proxy tools.

        Filters out internal tools (names starting with ``__``), creates
        proxy tools, and registers them under their bare semantic names
        (built-in plugin tools are first-class, like builtin tools — no
        ``server__tool`` prefix; only external MCP server tools keep it).  The
        ToolContext client re-point is the caller's job (via spec.ctx_field),
        not this function's.

        Args:
            name: Plugin short name (``"memdb"``, ``"wechat"``, …).
        """
        client = self._plugins[name].client
        assert client is not None
        # Names registered on the PREVIOUS connection — a plugin restart can
        # drop tools, and unregistered ones must not linger in the registry
        # bound to the old, disconnected client (B4).
        old_names = set(self._plugins[name].registered_tools)
        tools = await client.list_tools()
        logger.debug(
            "%s_tools names=%s", name,
            [t["name"] for t in tools],
        )

        # Internal: ``__`` (double-underscore) plugin tools are not exposed
        # to the LLM — filtered out for all agents.
        tagged = [
            {**t, "server": name}
            for t in tools
            if not is_internal_tool(t["name"])
        ]

        proxy_tools = create_proxy_tools(self._plugins[name].client, tagged)
        new_names = {t.name for t in proxy_tools}
        registered = self._register_plugin_proxies(proxy_tools)
        for stale in old_names - registered:
            self.tool_registry.unregister(stale)
        # Record the exact registered names so dead-process cleanup and stop
        # can unregister this plugin's bare-name tools without a prefix.
        self._plugins[name].registered_tools = registered
        if self.caps.catalog_owner and self._catalog is not None:
            # One sync for the plugin's whole tool set: rows go in, and a tool
            # it dropped loses its row (source-scoped, so only this plugin's).
            await self._catalog.sync_system_tools(proxy_tools, source=name)
            await self._catalog.mark_plugin_connected(name)
        logger.debug(
            "%s_tools_registered count=%d removed=%d", name, len(proxy_tools),
            len(old_names - new_names),
        )

    # ── MCP tool discovery & registration ────────────────────────────

    async def _discover_and_register_external_tools(self, server_name: str) -> None:
        """Discover tools from a specific MCP server and register as proxy tools.

        Idempotent full diff: registers tools the server offers that aren't
        registered yet, and unregisters tools it no longer offers.  Safe to
        call concurrently from several triggers (startup glue, reconnect
        notification, mcp_set callbacks) — a per-server in-flight guard
        coalesces races, and an empty tool list leaves the registry untouched
        so a half-connected server can't flicker its tools out.
        """
        lc = self._gateway_lifecycle()
        client = lc.client if lc is not None else None
        assert client is not None

        if server_name in self._mcp_syncing:
            return  # already syncing — the in-flight pass does the full diff
        self._mcp_syncing.add(server_name)
        try:
            tools_json = await client.call_tool(
                # __mcp_list_tools: registration is a full diff over every tool
                # the server publishes — a capped listing would drop the rest.
                "__mcp_list_tools", {"server": server_name}
            )
            tools_data = json.loads(tools_json)
            external = tools_data.get("tools", [])

            if not external:
                # Not connected/ready yet (list_all_tools returns [] when the
                # server isn't CONNECTED) — leave existing tools untouched so
                # a transient status blip can't tear them down.
                logger.debug("mcp_no_tools server=%s", server_name)
                return

            proxy_tools = create_proxy_tools(
                client, external,
                on_server_added=self._register_external_server_tools,
                on_server_removed=self._unregister_external_server_tools,
                on_server_updated=self._register_external_server_tools,
            )
            new_names = {t.name for t in proxy_tools}
            old_names = {
                t.name for t in self.tool_registry.list_tools()
                if t.name.startswith(f"{server_name}__")
            }
            for tool in proxy_tools:
                if tool.name not in old_names:
                    self.tool_registry.register(tool)
            for stale in old_names - new_names:
                self.tool_registry.unregister(stale)
            # Tools are now live in the registry — supersede any earlier
            # "enabled but not yet connected" startup warning with replace=True,
            # so the health store itself reflects the recovery (not just the
            # live check_mcp diff).  The wrapper's reconnect hook records the
            # same entry when a server comes up while the agent is otherwise
            # idle; recording here too keeps the store consistent regardless
            # of which path actually re-synced the tools.
            # Component name == the live check's (``check_mcp_gateway`` reports
            # the two server families separately) so the merge layer supersedes
            # this record once the live entry covers the same server;
            # ``replace=True`` keeps the store itself consistent when a server
            # recovers mid-session.
            from slife.health import record
            record(
                _health_component(server_name), "ok",
                key=server_name, value="tools registered",
                replace=True,
            )

            # Upsert this server's tool rows into the shared catalog (the
            # unified search/load surface).  Schema change → re-embed; the
            # host semantic drainer is woken below.
            if self.caps.catalog_owner and self._catalog is not None:
                await self._upsert_external_catalog_rows(
                    server_name, external, category=_server_category(server_name),
                )
                if self._catalog_semantic is not None:
                    self._catalog_semantic.on_saved()

            logger.debug(
                "mcp_tools_registered server=%s count=%d",
                server_name, len(proxy_tools),
            )
        except Exception as e:
            logger.error("mcp_discover_failed server=%s err=%s", server_name, e)
            from slife.health import record
            record(
                _health_component(server_name), "warning",
                key=server_name, value="tool discovery failed",
                hint=f"{e} — the server connected but its tool list is empty; "
                     f"re-run system_health to retry discovery.",
            )
        finally:
            self._mcp_syncing.discard(server_name)

    # ── Stop helpers ────────────────────────────────────────────────────

    async def stop_plugin(self, name: str) -> None:
        """Shut down a plugin by name.

        Disconnects its client, cancels its supervised background tasks
        (poll / restore), and stops its child process when this agent owns
        one.  A plugin that was never started is a no-op.
        """
        lc = self._registry.ensure(name)
        await lc.stop()

    async def stop_all_plugins(self) -> None:
        """Stop every registered plugin.

        Used by the app's shutdown path and by subagents tearing down their
        shared HTTP clients (a worker never owns a child process, so this
        only disconnects its clients).
        """
        await asyncio.gather(
            *(lc.stop() for lc in list(self._plugins.values())),
            return_exceptions=True,
        )
        # Close the shared catalog AFTER the plugin clients (a late reconcile
        # must never write a closed db).  Mandatory: an unclosed aiosqlite
        # connection keeps its non-daemon worker thread alive and BLOCKS
        # interpreter exit (pytest hangs after a green run).
        await self.close_catalog()

    async def close_catalog(self) -> None:
        """Teardown the shared tool catalog: drop the semantic drainer + close
        the db connection.  Idempotent; no-op when never opened."""
        # The reader's boot warmup first: it holds the store, and cancelling it
        # must not leave a task awaiting a closed db.
        if (self._catalog_reader_warmup is not None
                and not self._catalog_reader_warmup.done()):
            self._catalog_reader_warmup.cancel()
            try:
                await self._catalog_reader_warmup
            except (asyncio.CancelledError, Exception):
                pass
        self._catalog_reader_warmup = None
        if self._catalog_semantic_task is not None and not self._catalog_semantic_task.done():
            self._catalog_semantic_task.cancel()
            try:
                await self._catalog_semantic_task
            except (asyncio.CancelledError, Exception):
                pass
            self._catalog_semantic_task = None
        if self._catalog_semantic is not None:
            try:
                await self._catalog_semantic.close()
            except Exception as e:
                logger.debug("catalog_semantic_close_error err=%s", e)
            self._catalog_semantic = None
        if self._catalog is not None:
            try:
                await self._catalog.store.close()
            except Exception as e:
                logger.debug("catalog_close_error err=%s", e)
            self._catalog = None
            self._tool_ctx.catalog = None
            if self.agent_loop is not None:
                self.agent_loop.tool_catalog = None

    def kill_child_processes(self) -> None:
        """Synchronous best-effort child process cleanup.

        Called from the finally block in main() — no event loop required.
        Directly terminates known subprocesses so they don't become
        orphans holding log file handles on Windows.
        """
        # Kill every plugin's child process (all children live under
        # self._plugins[name].process — the plugin refactor removed the old
        # ``self._<name>_process`` dynamic attributes this method once scanned).
        for plugin in self._plugins.values():
            plugin.kill()

        # Subagent manager cleanup
        mgr = self._subagent_manager
        if mgr is not None:
            for name in list(mgr._subagents.keys()):
                proc = mgr._subagents.get(name)
                if proc is not None and proc._process is not None:
                    terminate_process_sync(
                        proc._process,
                        timeout=_timeouts.timeouts.grace.cleanup,
                        label=f"subagent-{name}",
                    )

    # ── Memory lifecycle ──────────────────────────────────────────────

    @property
    def memdb_enabled(self) -> bool:
        """Whether the memdb service is connected."""
        return self._plugins["memdb"].client is not None and self._plugins["memdb"].client.is_connected

    @property
    def wechat_enabled(self) -> bool:
        """Whether the WeChat plugin is connected."""
        return self._plugins["wechat"].client is not None and self._plugins["wechat"].client.is_connected

    # ── WeChat lifecycle ───────────────────────────────────────────────

    # WeChat lifecycle: enable gate + poll/restore glue live in the plugin
    # behavior methods (_gate_wechat / _after_ready_wechat / the wechat poll
    # loop below) — the uniform engine drives them, there is no start_wechat.

    async def _wechat_poll_loop(self, interval: float = 5.0) -> None:
        """Poll the wechat plugin for new messages and inject them into the inbox.

        Uses the internal wechat_drain_incoming tool so all wechat-specific
        logic — typing indicators, message format — stays inside the plugin
        process.  The main process only sees generic WeChat-channel messages.
        Replying to the peer is the model's job (wechat_send_message); the
        harness no longer auto-dispatches the assistant's text back out.
        """
        import json as _json
        from slife.a2a.identity import AgentMessage, Channel, WECHAT
        from slife.agent.message_history import wechat_marker

        logger.info("wechat_poll_loop_start interval=%.1fs", interval)

        while self.wechat_enabled:
            try:
                assert self._plugins["wechat"].client is not None

                result = await self._plugins["wechat"].client.call_tool(
                    "__wechat_drain_incoming", {},
                )
                data = _json.loads(result)
                msgs = data.get("messages", [])

                # The drain reports the session alongside the messages
                # ("ok" / "not_logged_in"), so login state costs no extra
                # call: diff it against what the TUI was last told.  A poll
                # that RAISED never gets here, so a transient error cannot
                # masquerade as a logout.
                #
                # Announced on a real transition only, by the same rule A2A
                # presence uses (``mesh.py``'s cold-card check): a first
                # "logged in" IS news, but a session that was never logged in
                # is the startup default — announcing it would print a fake
                # ⚠ every start, which is exactly the fake ✗ offline the
                # presence feed already refuses to emit.
                status = data.get("status", "")
                if status in ("ok", "not_logged_in"):
                    logged_in = status == "ok"
                    prev = self._wechat_logged_in
                    if prev is None and not logged_in:
                        self._wechat_logged_in = False   # cold: seed silently
                    elif prev != logged_in:
                        self._wechat_logged_in = logged_in
                        await self._notify_activity(
                            "wechat_status", logged_in=logged_in,
                        )

                for m in msgs:
                    peer_id = m.get("to_user_id", "")
                    text = m.get("text", "")
                    context_token = m.get("context_token", "")

                    if not text.strip():
                        continue

                    msg = AgentMessage(
                        source=WECHAT,
                        # The [Wechat:{…}] marker carries the peer + thread so
                        # the model can reply via wechat_send_message; the TUI
                        # drops it for display (channel already shows Wechat>).
                        content=(
                            f"{wechat_marker(peer_id, context_token or None)}"
                            f"{text}"
                        ),
                        metadata={"channel": "wechat"},
                        channel=Channel.wechat(),
                    )
                    await self.inbox.post(msg)
                    logger.debug("wechat_in from=%s text=%.100s", peer_id, text)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("wechat_poll_error err=%s", e)

            await asyncio.sleep(interval)

        logger.info("wechat_poll_loop_stop")

    async def save_to_memory(
        self,
        user_message: str = "",
        token_count: int | None = None,
        context_tokens: int | None = None,
        history: "MessageHistory | None" = None,
        channel: str = "",
        channel_data: str = "{}",
        created_at: "datetime | str | None" = None,
        handler: "object | None" = None,
        stop_reason: str = "",
    ) -> None:
        """Save the just-completed turn as a new row in memory.

        Args:
            user_message: The user's input text.
            token_count: Cumulative token usage for the turn (billing).
            context_tokens: The LAST LLM call's prompt + completion tokens —
                the exact token count of the persisted history as the next
                request would re-send it.  Persisted so restore primes the
                _turn_prompt with the real exit-time occupancy.
            history: The history to extract messages from.
                Defaults to self.message_history (the TUI history).
            channel: Source channel identity — 'human', 'wechat', or remote agent id.
            channel_data: JSON payload for the channel's own fields (A2A
                peer name, subagent name/task, …).  Written to the sibling
                ``turn_channel`` row so restore renders ``A2A(<name>)> ``.
            created_at: The user-input timestamp (Enter-press moment, aware
                datetime or ISO-8601 str).  Written as the turn row's
                ``created_at`` so restore matches the live [HH:MM].
                ``None`` lets the store use its own now().
            handler: The turn's UI handler, if any.  Receives the captured
                completion time via ``set_completed_at`` so the live
                assistant message shows when the turn actually finished.
            stop_reason: Why the turn ended early (``esc``, ``max_iterations``,
                ``error (<Type>)``), or empty on a turn that ended by itself.
                Labels the synthesized closing line when the repair below has
                to close an interrupted turn.
        """
        # Accumulate turn's billed tokens into the session total.
        if token_count:
            self.session_usage.total_tokens += token_count

        conv = history if history is not None else self.message_history

        # Invariant: never persist an inconsistent turn.  Repair orphaned
        # tool_calls and close the turn if needed BEFORE extracting — the
        # same ensure used on load and before each user message.  The reason
        # the turn stopped early rides along, so the synthesized closing line
        # says why (see message_history.interrupted_note).
        conv._ensure_turn_consistent(stop_reason)

        # Completion time — captured AFTER the final ensure (the turn is
        # now definitively done) and BEFORE the (potentially slow) MCP
        # save call, so completed_at reflects when the assistant finished,
        # not when the write landed.
        now = datetime.now().astimezone()

        # Push the completion time to the live UI so the assistant message
        # shows [HH:MM] at turn end — the same value restore will read.
        if handler is not None:
            set_completed = getattr(handler, "set_completed_at", None)
            if set_completed is not None:
                try:
                    set_completed(now)
                except Exception:
                    pass

        # Extract turn messages: everything after the matching user message.
        # Must handle both plain text (content is a str) and multimodal
        # messages (content is a list of {type, text/image_url} parts).
        #
        # Compare against the sanitized form of the input: add_user_message
        # stores sanitize_secrets(content), so a raw match would miss when
        # the user pasted an API key — and the turn would be saved with
        # empty messages (silent data loss).  If no user message matches at
        # all (the turn was rolled back on a content-filter reject), there is
        # nothing to persist.
        from slife.logfmt import sanitize_secrets
        target = sanitize_secrets(user_message)
        all_messages = list(conv.messages)
        turn_messages: list[dict] | None = None
        user_idx = -1  # index of the matched user message (for the footnote)
        for i in range(len(all_messages) - 1, -1, -1):
            msg = all_messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content == target:
                turn_messages = all_messages[i + 1:]
                user_idx = i
                break
            if isinstance(content, list):
                text = "".join(
                    p.get("text", "") for p in content
                    if p.get("type") == "text"
                )
                if text == target:
                    turn_messages = all_messages[i + 1:]
                    user_idx = i
                    break

        if turn_messages is None:
            return

        # Context trimming happens after this save, in
        # AgentLoop._trim_after_save (invoked below once the row is written)
        # — it uses this turn's real API usage and appends a runtime
        # trim note.  Each turn is saved here via
        # memory_save_turn, so trimmed turns remain searchable.

        # The runtime trim note must never reach the Turns DB — it is
        # meaningful only in the live session.  Strip it from the copy
        # being persisted (the live history keeps its note).  The runtime
        # turn ids go with it: the loop needs them in memory to map turns
        # back to diary rows, but they are not part of the record.
        from slife.agent.message_history import MessageHistory as _MH
        turn_messages = _MH.strip_turn_ids(
            _MH.strip_trim_markers(turn_messages)
        )

        # Permanent memory keeps only head+tail digests of oversized tool
        # results — the Turns DB never hoards reproducible tool output, so a
        # single result can't grow a turn past what session restore can
        # rebuild within the context floor.  The live history is not
        # touched (the copy swapped into turn_messages is a new dict).
        compacted = compact_tool_results(
            turn_messages, self.config.memory_tool_result_chars,
        )
        if compacted:
            logger.info(
                "memory_tool_result_compacted count=%d budget=%d",
                compacted, self.config.memory_tool_result_chars,
            )

        if not self.memdb_enabled:
            # Memory writes are mandatory: the memdb channel is down (e.g. mid
            # watchdog-restart).  Never drop the turn silently — raise so the
            # inbox surfaces it like an LLM API error and the turn is not
            # treated as a clean completion.
            raise MemorySaveError(t("memory_save_no_channel"))
        assert self._plugins["memdb"].client is not None  # guarded above
        save_args = {
            "user_message": user_message,
            "messages": turn_messages,
            "token_count": token_count or 0,
            "context_tokens": context_tokens or 0,
            "who_helped": self.config.agent_name,
            "what_model": self.config.active_model.ref,
            "channel": channel,
            "channel_data": channel_data,
        }
        if created_at:
            # Normalise an aware datetime to the store's ISO format; a str
            # (e.g. tests) is passed through as-is.
            save_args["created_at"] = (
                created_at.astimezone().isoformat(timespec="seconds")
                if isinstance(created_at, datetime)
                else created_at
            )
        save_args["completed_at"] = now.isoformat(timespec="seconds")
        # A rowid-less turn_summarize captured the current turn's
        # summary/tags — ride them on the save so they land on the new row.
        summary, tags = _extract_turn_annotation(turn_messages)
        if summary is not None or tags is not None:
            save_args["summary"] = summary
            save_args["tags"] = tags
        try:
            result = await asyncio.wait_for(
                self._plugins["memdb"].client.call_tool(
                    "__memory_save_turn",
                    save_args,
                ),
                timeout=_timeouts.timeouts.work.save_memory,
            )
        except asyncio.TimeoutError:
            # The save is a fast insert (embedding is deferred to the memdb
            # plugin's background reindex) — a timeout now means the MCP
            # channel itself is slow, not a first-save model load.  The row
            # may still be written server-side — surface that uncertainty to
            # the user rather than silently skipping.
            logger.warning("memdb_save_timeout reason=save_call_exceeded_timeout")
            raise MemorySaveError(t("memory_save_timeout")) from None
        except Exception as e:
            # A raised call_tool is a transient MCP/channel failure (the
            # plugin returns {"error": ...} for DB-side failures instead).
            logger.warning("memdb_save_error err=%s", e)
            raise MemorySaveError(t("memory_save_channel_error", err=e)) from e

        # The plugin returns {"error": ...} on a persistent DB failure
        # (broken schema, corruption, disk).  Memory is core — this is a
        # hard stop, not a silent skip: freeze the inbox and surface it.
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("error"):
                self._memory_broken = True
                self._memory_error = parsed["error"]
                logger.error("memory_save_fatal err=%s", self._memory_error)
                if self.inbox is not None:
                    # Log-only (the TUI gets the localized memory_broken
                    # message via _on_memory_broken) — logs stay English.
                    self.inbox.freeze(f"memory save failed: {self._memory_error}")
                if self._on_memory_broken is not None:
                    try:
                        self._on_memory_broken(self._memory_error)
                    except Exception:
                        pass
            elif isinstance(parsed, dict):
                # The turn now has a turn_id — annotate its user message so the
                # next LLM call can reference it precisely (and the
                # turn_summarize default no longer needs the racy
                # latest_rowid fallback).  Live TUI bubbles are NOT
                # retro-updated: their lack of the footnote is itself the
                # "current session" signal.  Best-effort — a failure only
                # leaves the message unannotated.
                try:
                    self._annotate_saved_turn(
                        conv, user_idx, rowid=parsed.get("turn_id"),
                        created_at=created_at, completed_at=now,
                    )
                except Exception:
                    logger.debug("turn_annotation_skipped", exc_info=True)
                # Trim AFTER the turn is safely persisted: the just-completed
                # turn's real context size (the last API call's prompt +
                # completion) is now known, so the ceiling check is exact, and
                # a trim can never lose an unsaved turn.  Operates on *conv*
                # (the history
                # this turn ran in — human / wechat / a2a), never the global.
                # Best-effort: a trim failure must not break the save flow.
                loop = getattr(self, "agent_loop", None)
                if loop is not None:
                    try:
                        await loop._trim_after_save(conv, handler)
                    except Exception:
                        logger.exception("trim_after_save_failed")
            else:
                # The channel returned something that is neither a save
                # ack nor an error object (non-JSON text, or JSON that
                # isn't an object) — the save may or may not have landed
                # and there is no way to tell.  Don't swallow it silently:
                # surface it to the user (memory writes are mandatory).
                logger.warning("memdb_save_unparsable response=%.120r", result)
                raise MemorySaveError(t("memory_save_unconfirmed"))

    def _annotate_saved_turn(
        self,
        history: "MessageHistory",
        user_idx: int,
        rowid: int | None,
        created_at: "datetime | str | None",
        completed_at: datetime,
    ) -> None:
        """Append the turn footnote to the just-saved turn's user message.

        Called after a successful ``__memory_save_turn`` so the rowid is
        known: the next LLM call sees ``[INFO: {"turn_id": N, …}]`` and can
        reference the turn precisely.  Purely additive and best-effort — a
        failure leaves the message unannotated.

        **Every** saved turn is annotated, autonomous ones included (the
        heartbeat / schedule / timer note here is the one thing that changed):
        the footnote is what makes a turn *addressable*, and the per-turn
        recall's keep-list addresses turns by exactly this id.  Leaving the
        synthetic turns unannotated meant a keep-list silently dropped them —
        including scheduled turns that did real work — with nothing in the
        conversation to say why.

        The **structural** id (``_turn_id``) is separate from that footnote
        and is set on every saved turn too: the loop maps in-context turns
        back to diary rows with it, so the trim can hand the store the real
        ids to drop.  It is runtime-only and stripped before the turn is
        persisted; the footnote is not — it rides the stored user message.
        """
        if rowid is None:
            return
        msgs = history.messages
        if not (0 <= user_idx < len(msgs)) or msgs[user_idx].get("role") != "user":
            return
        msgs[user_idx]["_turn_id"] = rowid

        def _iso(value) -> str:
            if isinstance(value, datetime):
                return value.astimezone().isoformat(timespec="seconds")
            return value or ""

        header = turn_header({
            "rowid": rowid,
            "created_at": _iso(created_at),
            "completed_at": _iso(completed_at),
        })
        if not header:
            return
        content = msgs[user_idx].get("content", "")
        if isinstance(content, str):
            msgs[user_idx]["content"] = f"{content} {header}".strip()
        elif isinstance(content, list):
            msgs[user_idx]["content"] = list(content) + [
                {"type": "text", "text": " " + header}
            ]
        else:
            return

    async def get_exit_context_turns(self) -> list[dict]:
        """Load the exit-time context's turns for restore. Returns [] if none.

        Named for what it returns rather than for "recent" turns: this is not
        a recency window but the persisted live-context list — the context
        the agent was working with when it exited.  Restore is its only
        caller, which is what makes the exit-time reading exact.

        Restores the **exit-time context** verbatim: it reads the persisted
        ordered live-context id list (:meth:`SessionStore.get_context_turns`,
        maintained by the save / ``_trim_after_save`` / the per-turn rebuild)
        and returns exactly those turns **in the list's order** — the slice the
        agent was working with when it exited.  No re-slicing against the
        ceiling: the list already encodes the trimmed state, so restore simply
        replays it (the agent picks up exactly where it left off).  The list is
        its own bound, so nothing is dropped for a budget and there is no
        ceiling to report against.

        The list order is authoritative and is never re-sorted by rowid: the
        in-context slice is not necessarily contiguous, so its order is part
        of the contract.  Heartbeat turns are included — they restore as
        ⚡ 自主, consistent with the live TUI.

        Reads directly from SQLite — independent of the memory plugin / MCP.
        """
        store = None
        db_path = None
        try:
            from slife.plugins.memdb.store import SessionStore

            db_path = self._get_memory_db_path()
            if not (db_path and db_path.is_file()):
                return []
            store = SessionStore(db_path)
            await store.setup(embedding_dim=0)
            turn_ids = await store.get_context_turns()
            if not turn_ids:
                return []
            return await store.get_turns_by_ids(turn_ids)
        except Exception as e:
            # A present-but-broken memory DB (missing column, corruption,
            # disk error) must NOT start a memory-less session silently —
            # memory is core, so restore failure is fatal.
            logger.error("memory_restore_fatal err=%s", e)
            raise MemoryDatabaseError(
                f"cannot read memory database {db_path or 'unknown'}: {e}"
            ) from e
        finally:
            # Close the aiosqlite connection so its worker thread doesn't
            # outlive the event loop (leaks + "Event loop is closed" in tests).
            if store is not None:
                try:
                    await store.close()
                except Exception:
                    pass

    async def drop_context_turns(self, turn_ids: list[int]) -> bool:
        """Persist the live context after the internal trim evicted turns.

        Called by ``AgentLoop._trim_after_save`` with the ids it removed, so
        a restart rebuilds the exit-time context from exactly where the live
        one stood.  Best-effort — if the memdb channel is unreachable the
        list just stays longer than it should, which makes the next restore a
        *superset* (trimmed turns come back into context), never a loss.
        """
        if not turn_ids or not self.memdb_enabled:
            return False
        return await self._call_context_tool(
            "__memory_context_turns_drop",
            {"turn_ids": list(turn_ids)},
            f"count={len(turn_ids)}",
        )

    async def _call_context_tool_payload(
        self, tool: str, args: dict,
    ) -> dict | None:
        """Call an internal memdb tool; return its parsed payload or None.

        The sibling of :meth:`_call_context_tool` for the calls whose *answer*
        matters rather than just whether the write landed.

        ``None`` covers four states and they are all "the store could not be
        asked" — no client, a failed/timed-out call, a non-text result, an
        unparseable one.  A plugin-side failure arrives as an ``"Error: …"``
        string (the MCP client never raises), which is exactly the
        unparseable case, so it is logged here: it is the shape a *fatal*
        store failure takes, and it must not read as silence.
        """
        plugin = self._plugins.get("memdb")
        client = getattr(plugin, "client", None) if plugin else None
        if client is None:
            return None
        try:
            result = await asyncio.wait_for(
                client.call_tool(tool, args),
                timeout=_timeouts.timeouts.work.save_memory,
            )
        except Exception:
            logger.warning("context_tool_call_failed tool=%s", tool)
            return None
        if not isinstance(result, str):
            logger.warning("context_tool_result_not_text tool=%s", tool)
            return None
        try:
            parsed = json.loads(result)
        except Exception:
            # Local import, off the success path (as at the save path).
            from slife.logfmt import sanitize_secrets

            logger.warning(
                "context_tool_payload_unusable tool=%s result=%.120s",
                tool, sanitize_secrets(result),
            )
            return None
        return parsed if isinstance(parsed, dict) else None

    async def recall_turns(
        self, query: str = "", since: str | None = None,
        until: str | None = None, reserved_tokens: int = 0,
    ) -> list[int] | None:
        """Recall the turn ids this turn should **add** to its context.

        The selector is the memory plugin's internal tool, so this is the
        harness asking its own store — the model has ``turn_search`` and
        ``turn_list`` for its own reading, and neither touches the context.
        The rebuild needs only the ids, and fetches the turns themselves with
        :meth:`turns_by_ids`.

        *reserved_tokens* is what the turns being kept already spend: the
        recalled set is sized to the headroom *below* the context floor, so a
        kept context cannot push the turn over the window before it starts.
        It is derived from the config and the decision's own base — never a
        model's number — so recall's caps stay recall's configuration.

        ``None`` means the store could not be asked — memdb is off, the
        channel is unreachable, the store reported a failure, or the payload
        was not a usable selection — and the caller must keep the context it
        has: nothing was learned about what this turn needs, so changing the
        context would be a guess.

        ``[]`` is an *answer*, not a failure: the store was asked and no turn
        qualified.  The caller adds it to what it keeps, which is to say it
        adds nothing.
        """
        if not self.memdb_enabled:
            return None
        payload = await self._call_context_tool_payload(
            "__memory_turn_recall",
            {"query": query, "since": since, "until": until,
             "reserved_tokens": reserved_tokens},
        )
        if not payload or payload.get("error"):
            return None
        turns = payload.get("turns")
        if not isinstance(turns, list):
            return None
        ids = [t for t in turns if isinstance(t, int)]
        if turns and not ids:
            # A non-empty selection carrying no ids is a broken payload, not
            # an empty answer — and the difference is the context: `[]`
            # clears it, so only a selection that genuinely says "nothing"
            # may be read as one.
            logger.warning("recall_payload_unusable turns=%d", len(turns))
            return None
        return ids

    async def turns_by_ids(self, turn_ids: list[int]) -> list[dict]:
        """Fetch the turn rows for *turn_ids* (the recall companion).

        ``[]`` here is unambiguous — the caller only asks with a non-empty id
        list, so no turns means the fetch failed, which it reads as "keep the
        context" (a selection it cannot render must not replace one it can).
        """
        if not turn_ids or not self.memdb_enabled:
            return []
        payload = await self._call_context_tool_payload(
            "__memory_turns_by_ids", {"turn_ids": list(turn_ids)},
        )
        if not payload or payload.get("error"):
            return []
        return payload.get("turns") or []

    async def set_context_turns(self, turn_ids: list[int]) -> bool:
        """Replace the persisted live-context list with *turn_ids*.

        The write behind the per-turn rebuild: recall's selection overrides
        the previous context, so this replaces rather than merges.  Only
        called after a known-good recall — a partial list silently shrinks
        the context, so an empty one is refused outright rather than wiping
        it.  Best-effort like its siblings: on failure the in-memory rebuild
        still stands and only the *restart* path is stale (a superset).
        """
        if not turn_ids or not self.memdb_enabled:
            return False
        return await self._call_context_tool(
            "__memory_context_turns_set",
            {"turn_ids": list(turn_ids)},
            f"count={len(turn_ids)}",
        )

    async def clear_context_turns(self) -> bool:
        """Empty the persisted live-context list — the write behind an empty
        recall selection (the per-turn rebuild's own clear).

        Same best-effort contract as :meth:`drop_context_turns`.
        """
        if not self.memdb_enabled:
            return False
        return await self._call_context_tool(
            "__memory_context_turns_clear", {}, "clear",
        )

    async def _call_context_tool(
        self, tool: str, args: dict, tag: str,
    ) -> bool:
        """Call an internal memdb context tool, best-effort."""
        plugin = self._plugins.get("memdb")
        client = getattr(plugin, "client", None) if plugin else None
        if client is None:
            return False
        try:
            await asyncio.wait_for(
                client.call_tool(tool, args),
                timeout=_timeouts.timeouts.work.save_memory,
            )
            return True
        except Exception:
            logger.warning("context_turns_call_skipped tool=%s %s", tool, tag)
            return False

    def _get_memory_db_path(self) -> Path | None:
        """Return the memory database path."""
        from slife.paths import get_memdb_db_path

        return get_memdb_db_path()

    # ── Autonomous heartbeat ──────────────────────────────────────────

    def on_autonomous(self, callback) -> None:
        """Register a callback for autonomous (heartbeat) output."""
        self._on_autonomous = callback

    def on_schedule(self, callback) -> None:
        """Register a callback for scheduler-driven output (cron fires,
        run_schedule_now backfills)."""
        self._on_schedule = callback

    def on_timer(self, callback) -> None:
        """Register a callback for timer-driven output (wait_minutes wake)."""
        self._on_timer = callback

    def on_heartbeat(self, callback) -> None:
        """Register a callback for every heartbeat outcome (quiet|act)."""
        self._on_heartbeat = callback

    def on_memory_broken(self, callback) -> None:
        """Register a callback for a fatal memory-save failure (red banner)."""
        self._on_memory_broken = callback

    def on_tunnel_down(self, callback) -> None:
        """Register a callback for a file-sharing tunnel that failed to start."""
        self._on_tunnel_down = callback

    async def surface_autonomous(self, text: str) -> None:
        """Deliver an autonomous message to the TUI (⚡ 自主)."""
        cb = self._on_autonomous
        if cb is not None:
            try:
                await cb(text)
            except Exception:
                logger.debug("surface_autonomous_error", exc_info=True)

    async def surface_schedule(self, text: str) -> None:
        """Deliver a scheduler-driven message to the TUI (📅 定时).

        Cron fires and ``run_schedule_now`` backfills are scheduler output,
        not autonomous acts — they surface here, not through
        :meth:`surface_autonomous`."""
        cb = self._on_schedule
        if cb is not None:
            try:
                await cb(text)
            except Exception:
                logger.debug("surface_schedule_error", exc_info=True)

    async def surface_timer(self, text: str) -> None:
        """Deliver a timer-driven message to the TUI (⏰ timer).

        A ``wait_minutes`` wake resumes the agent's own work, not an
        autonomous act or a scheduled run — it surfaces here."""
        cb = self._on_timer
        if cb is not None:
            try:
                await cb(text)
            except Exception:
                logger.debug("surface_timer_error", exc_info=True)

    async def _notify_heartbeat(self, outcome: str) -> None:
        """Notify the TUI that a heartbeat beat happened (status-bar pulse)."""
        cb = self._on_heartbeat
        if cb is not None:
            try:
                await cb(outcome)
            except Exception:
                pass

    async def surface_autonomous_reply(
        self, text: str, cancelled: bool = False
    ) -> None:
        """``on_reply`` for heartbeat turns — surface only real content.

        The quiet reply is exactly ``.`` (checked in, nothing to do); any
        other non-empty text is an autonomous act worth surfacing.  Both
        outcomes are notified as a heartbeat (status-bar pulse).
        """
        t = (text or "").strip()
        if t and t != ".":
            logger.info("heartbeat_act text=%.200s", t)
            await self.surface_autonomous(t)
            await self._notify_heartbeat("act")
        else:
            logger.info("heartbeat_quiet")
            await self._notify_heartbeat("quiet")

    async def fire_schedule_now(self, name: str, due_at: str = "",
                                clone_context: bool = False) -> str:
        """Run a scheduled task immediately (backfill / manual trigger).

        Delegates to :func:`slife.agent.schedules.fire_task_now`, which
        records a run and injects the task's trigger into the inbox.  *due_at*
        targets an exact run (a missed/failed backfill); omit for a fresh
        cron-fire run at now.  *clone_context* spawns the worker with the main
        agent's current conversation, so a task with no stored description
        still has substance to act on.
        """
        from slife.agent.schedules import fire_task_now

        return await fire_task_now(
            self, name, due_at, clone_context=clone_context,
        )

    async def schedule_wakeup(self, delay_seconds: float, note: str) -> None:
        """Schedule a one-shot ``[Timer]`` wake after *delay_seconds*.

        The timer is in-memory — it survives exactly as long as this process.
        When it elapses a ``[Timer]`` message is posted to the inbox, which
        wakes the main agent as a fresh turn with full prior context (every
        inbox message runs against the one shared history).
        """
        from slife.a2a.identity import SYSTEM, AgentMessage, Channel
        from slife.agent.heartbeat import _SilentHandler
        from slife.agent.timer import timer_text

        async def _wake() -> None:
            await asyncio.sleep(delay_seconds)
            await self.inbox.post(AgentMessage(
                source=SYSTEM,
                content=timer_text(delay_seconds / 60, note),
                handler=_SilentHandler(),
                on_reply=self._surface_timer_reply,
                channel=Channel.system(),
            ))
            logger.info("timer_fired delay_seconds=%s", delay_seconds)

        task = asyncio.create_task(_wake())
        self._timer_tasks.add(task)
        task.add_done_callback(self._timer_tasks.discard)

    async def _surface_timer_reply(self, text: str, cancelled: bool = False) -> None:
        """``on_reply`` for timer turns — surface non-silent replies.

        A bare ``.`` or empty reply is suppressed (mirrors the heartbeat/schedule
        silence contract); anything else is the resumed work's answer, surfaced
        as ⏰ timer.
        """
        t = (text or "").strip()
        if t and t != ".":
            await self.surface_timer(t)

    def refresh_system_prompt(self) -> None:
        """Re-render the system prompt and replace it in the live history
        and the per-history store, so the next API call reads the new bytes.

        Called when model info changes (``reload_active_model``) and after a
        ``add_user_pref`` write (the USER.md section changed).  The byte
        change touches the prompt cache — accepted, both events are rare.
        """
        new_system = build_system_prompt(self.config)
        if self.message_history.messages and self.message_history.messages[0]["role"] == "system":
            self.message_history.messages[0]["content"] = new_system
        self.inbox._histories.update_system_prompt(new_system)

    # ── Inbox lifecycle (always active) ────────────────────────────────

    async def wait_startup_settled(self) -> None:
        """Wait until every attempted plugin spawn has converged.

        The service opens for user input only after this: the inbox
        consumer and the TUI input both await it.  Event-driven — set
        ``finally`` by the last ``start_plugin_server`` call, never
        polled and never time-bounded.
        """
        await self._startup_settled.wait()

    @property
    def startup_settled(self) -> bool:
        """True once every attempted plugin spawn has converged."""
        return self._startup_settled.is_set()

    async def _init_catalog(self) -> None:
        """Open the shared tools.db + build the catalog service, best-effort.

        Runs once per process — main agent and subagent workers all open the
        same file (WAL: concurrent readers, one writer at a time).  Startup
        seed (the session default "all registered loaded") and eviction
        policy are main-owner only (``write_owner``); a subagent worker
        reads the same shared state and can flip load status but never
        reseeds or evicts.  Failure degrades to no catalog: the loop injects
        the whole registry as today, no eviction, no unified search.
        """
        if self._catalog is not None:
            return
        from slife.tools.catalog import CatalogStore
        from slife.tools.catalog_service import ToolCatalogService
        from slife.paths import get_tools_db_path

        try:
            store = CatalogStore(get_tools_db_path())
            await store.open()
            # The op window the tool-set line reports opens HERE, with the
            # catalog — not at the first reconcile pass.  The boot seed below,
            # the skill/cli mirror and every plugin's connect write rows before
            # that pass runs, and one pass is also not the whole startup: the
            # pass that converges is the one whose LAST server finally lists,
            # so a window armed per-pass reported that pass's slice alone.  A
            # cold boot of 1586 rows read 新增 1239 — the one source (github,
            # the slowest listing) that converged it.  ``_report_tool_sync``
            # closes the window, which is exactly the line it feeds.
            store.begin_ops()
            svc = ToolCatalogService(
                store,
                threshold=self._tool_load_threshold,
                write_owner=self.caps.catalog_owner,
                # tools.yaml's per-entry `autoload: true` — the explicit "load
                # these at startup" escape hatch around the default (only the
                # whitelist is born loaded).  A server entry's flag covers its
                # whole tool set, whose names are unknown until it connects.
                autoload=tuple(self.config.autoload_tools),
                autoload_servers=tuple(self.config.autoload_servers),
                # The per-entry `enabled: false` mirrors, one section per
                # category: a job from `job`, a plugin's own tool from `plugin`.
                disabled_jobs=tuple(self.config.disabled_jobs),
                disabled_plugin=tuple(self.config.disabled_plugin),
                disabled_builtins=tuple(self.config.disabled_builtins),
            )
            self._catalog = svc
            self._tool_ctx.catalog = svc
            self.tool_registry.set_catalog(svc)
            # Owning the shared rows (the boot seed, the skill/cli mirror, the
            # external-status marks, the config purge) is one grant: a worker
            # only READS this catalog, and its parent has already seeded the
            # same file.  Everything below this branch is the owner's alone —
            # the rows it would write are already there, and two processes
            # racing upsert-then-purge on one db is what the grant prevents.
            if self.caps.catalog_owner:
                # The session seed, over everything currently registered (the
                # system tools: builtin + built-in plugin tools), PLUS the
                # builtins an override switched off: they are not registered
                # (the factory skips them) but tools.yaml still declares them, so
                # the db carries their row marked `disabled` rather than omitting
                # a tool yaml names.  External mcp/rest-api rows are seeded by the
                # reconcile as their servers connect.
                # ``own_builtins``: this list IS the builtin family, so a class
                # that left the code loses its row in this pass — every other
                # family purges from its own source (a plugin's rescan, the
                # skill/cli mirror, the `tools.yaml` comparison for servers).
                await svc.sync_system_tools([
                    *self.tool_registry.list_tools(),
                    *disabled_tool_instances(
                        self.config.tools, config=self.config, ctx=self._tool_ctx,
                    ),
                ], own_builtins=True)
                # Skill and cli rows come from their own live sources (the
                # skills dir, the cli section of tools.yaml), not from the
                # registry — sync_system_tools cannot see them, so they are
                # mirrored here.  Same rows as any other tool: that is how
                # tool_search reaches a skill, with no load state (type
                # skill/cli instead of func).
                await self._mirror_local_rows(svc)
                # Nothing external is usable yet — no server has connected.
                # Mark every external tool ``error`` so the injection set
                # starts empty (rather than offering tools from servers that
                # may never come up); each server clears its own mark as the
                # reconcile sees it connect.
                await svc.mark_all_external_error()
                # tools.yaml IS the authoritative config — anything that left
                # its mcp/rest-api sections loses its rows here (hand-edits and
                # agent-tool edits alike).  Startup does NOT wait on the servers
                # themselves: the wrapper connects them in the background and
                # each connect wakes the reconcile, so a slow machine opens the
                # TUI immediately.
                await self._sync_catalog_from_config()
            # The loop was built in __init__ before the catalog existed.
            self.agent_loop.tool_catalog = svc
            self.agent_loop.load_threshold = self._tool_load_threshold
            # The catalog's semantic surface, by grant.  ONE process maintains
            # the index — the drainer, and the only writer of its vectors.
            # Every process may QUERY it, because the vectors and the index's
            # published state both live in this shared db: a worker gets a
            # reader over what its parent embedded.  (Before this a worker got
            # nothing, so a subagent's tool_search was keyword-only for its
            # whole life against an index its parent was maintaining.)
            from slife.tools.semantic import SemanticManager, SemanticReader

            # ONE resolution of the section, for whichever surface this role
            # gets: both embed against the same endpoint, so a second read is a
            # second answer waiting to disagree.  ``__post_init__`` fills the
            # optional sections, so it is never None here (the same guarantee
            # ``__init__`` asserts for a2a/subagent config).
            embeddings_config = self.config.embeddings_config
            assert embeddings_config is not None
            if self.caps.catalog_drainer:
                self._catalog_semantic = SemanticManager(store, embeddings_config)
                svc.semantic_manager = self._catalog_semantic
                self._catalog_semantic_task = asyncio.create_task(
                    self._catalog_semantic.start(),
                    name="catalog-semantic",
                )
            else:
                svc.semantic_reader = SemanticReader(store, embeddings_config)
                # …and warm it at boot, so the capability is up before the
                # first task rather than during it.  Background, like the
                # drainer's own start(): a slow endpoint costs the worker
                # nothing at spawn, and the first query waits for the shared
                # load instead of degrading while it runs.
                self._catalog_reader_warmup = asyncio.create_task(
                    svc.semantic_reader.warmup(),
                    name="catalog-semantic-warmup",
                )
            logger.info("catalog_initialized role=%s", self.role.value)
        except Exception:
            logger.exception("catalog_init_failed — continuing without catalog")

    async def _mirror_local_rows(self, svc) -> None:
        """Mirror the two registry-less categories into the catalog.

        ``skill`` and ``cli`` have no registered tool instance to seed from —
        their sources are the skills dir and tools.yaml's ``cli`` section — so
        they are pushed here at boot, and again by each skill_*/cli_* tool right
        after it writes its source.  Best-effort: a failure leaves the previous
        rows in place rather than blocking startup.
        """
        from slife.paths import get_skills_dir
        from slife.tools.cli import sync_cli_catalog
        from slife.tools.skill import sync_skill_catalog

        try:
            await sync_skill_catalog(self._tool_ctx, get_skills_dir())
            await sync_cli_catalog(self._tool_ctx, self.config.cli_tools)
            logger.info(
                "catalog_local_rows_mirrored skills=%d cli=%d",
                len(await svc.store.names_by_category("skill")),
                len(await svc.store.names_by_category("cli")),
            )
        except Exception as e:
            logger.debug("catalog_local_mirror_failed err=%s", e)

    async def _refresh_local_rows_if_changed(self, svc) -> None:
        """Re-mirror the registry-less families when their source moved.

        The mutation TOOLS re-mirror right after they write, so the agent's own
        edits are never stale — this exists for the other editor, a person with
        ``tools.yaml`` or a ``SKILL.md`` open.  mtimes are the cheap, precise
        signal: a pass that finds them unchanged costs two stats.

        **The cli section is re-read from DISK**, not from ``self.config`` —
        that object is the boot snapshot, and re-pushing it would write the
        stale rows back.  Same for the per-entry disable flags, which are
        otherwise captured when the catalog service is built.
        """
        from slife.paths import get_skills_dir
        from slife.plugins.mcp_gateway import config as _gw_cfg
        from slife.tools.cli import sync_cli_catalog
        from slife.tools.skill import sync_skill_catalog

        def _mtime(path) -> float:
            """0.0 for a source that is not there — a missing skills dir must
            not also freeze the cli mirror."""
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        mtimes = (_mtime(_gw_cfg.current_path()), _mtime(get_skills_dir()))
        # A row currently marked ``error`` is re-checked on EVERY pass, mtime or
        # not: its status comes from the source itself (a SKILL.md that could
        # not be read), so nothing else would ever ask again — and the fix for
        # one (a permission restored, a drive remounted) moves no mtime, which
        # would leave the row ``error`` until the next boot.  The mirror is a
        # delta writer, so an unchanged re-read writes nothing.
        if mtimes == self._local_rows_mtimes and not await svc.store.has_error_rows(
            {"skill", "cli"},
        ):
            return
        self._local_rows_mtimes = mtimes

        try:
            raw = _gw_cfg.load_config()
            cli_section = raw.get("cli") if isinstance(raw, dict) else None
            await sync_cli_catalog(
                self._tool_ctx, cli_section if isinstance(cli_section, dict) else {},
            )
            await sync_skill_catalog(self._tool_ctx, get_skills_dir())
            # The in-memory disable flags move with the file too — otherwise a
            # hand-edited `enabled: false` would wait for the next boot.
            from slife.config import _disabled_names
            svc.reload_disabled(
                builtins=_disabled_names(raw.get("builtin") or []),
                jobs=_disabled_names(raw.get("job") or []),
                plugin=_disabled_names(raw.get("plugin") or []),
            )
            logger.info(
                "catalog_local_rows_refreshed skills=%d cli=%d",
                len(await svc.store.names_by_category("skill")),
                len(await svc.store.names_by_category("cli")),
            )
        except Exception as e:
            # Best-effort, like every other mirror: the previous rows stand.
            logger.debug("catalog_local_refresh_failed err=%s", e)

    async def _sync_catalog_from_config(self) -> None:
        """Startup db ← tools.yaml sync — tools.yaml IS the authority.

        Covers BOTH hand-edits and agent-tool edits to the mcp/rest-api
        sections: mirror server rows (enabled + category from the config) and
        purge rows for servers removed from the config.  Blocking by design —
        the TUI shows "工具注册表同步中" while it runs.  The live wrapper
        reconcile refines runtime/tool rows afterwards.  Best-effort.
        """
        catalog = self._catalog
        if catalog is None or not self.caps.catalog_owner:
            return
        try:
            from slife.plugins.mcp_gateway import config as _cfg
            await catalog.purge_unconfigured_sources(set(_cfg.servers()))
        except Exception as e:
            # Best-effort by contract: a failure leaves whichever rows the
            # purge already handled and keeps the catalog live.  Nulling it
            # here would silently disable every catalog-using path for the
            # rest of the session (and leak the store on close) — a transient
            # DB lock must not take the whole catalog down.
            logger.debug("catalog_config_sync_failed err=%s", e)

    async def start_inbox(self) -> None:
        """Start the inbox background processor.

        Called during app startup before A2A/WeChat so the queue is
        ready to accept messages from any input channel.
        """
        await self._init_catalog()
        if self._inbox_task is not None:
            return
        self._inbox_task = asyncio.create_task(self.inbox.run())
        logger.info("inbox_started")

        # slife-as-plugin — the in-process MCP server exposing the live
        # ToolRegistry to external MCP consumers (DESIGNER_NOTES §8).  Main
        # agent only; subagents are workers and never serve their own face.
        if self.caps.host_server:
            from slife.mcp.host_server import start_host_server
            try:
                # Every instance binds its own OS-assigned free port — there is
                # no fixed well-known address, so a second concurrent agent in
                # the same data dir never collides.  The bound port is returned,
                # logged, and published for consumers.
                server, task, _stop, host_port = start_host_server(
                    self.tool_registry,
                    catalog=self._catalog,
                )
                self._host_server = (server, task, _stop, host_port)
                os.environ["SLIFE_HOST_PORT"] = str(host_port)
                logger.info(
                    "host_server_started port=%s tools=%d",
                    host_port,
                    len(self.tool_registry.list_tools()),
                )
            except Exception:
                logger.exception("host_server_start_failed")
                self._host_server = None

        # Autonomous heartbeat — idle turns the agent gives itself (period
        # configurable via agent.heartbeat_interval).  A worker is task-driven
        # and never receives a heartbeat trigger.
        if self.caps.heartbeat:
            from slife.agent.heartbeat import heartbeat_loop

            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(heartbeat_loop(self))
                logger.info("heartbeat_started")

        # Scheduled-task trigger loop — fires due tasks; it never sweeps
        # unfinished runs (see schedule_startup_sweep).  One timer per task,
        # and it belongs to the session the user is in.
        if self.caps.schedules:
            from slife.agent.schedules import schedule_loop

            if self._schedule_task is None or self._schedule_task.done():
                self._schedule_task = asyncio.create_task(schedule_loop(self))
                logger.info("schedule_loop_started")

            # One-shot startup pass for runs a previous lifetime left undone:
            # reaps unconfirmed runs to failed.  Runs once and exits.
            from slife.agent.schedules import schedule_startup_sweep

            if (self._schedule_startup_task is None
                    or self._schedule_startup_task.done()):
                self._schedule_startup_task = asyncio.create_task(
                    schedule_startup_sweep(self)
                )
                logger.info("schedule_startup_sweep_started")

    async def stop_inbox(self) -> None:
        """Stop the inbox background processor (and the heartbeat)."""
        if self._host_server is not None:
            _server, _task, _stop, _port = self._host_server
            try:
                await _stop()
            except Exception as e:
                logger.debug("host_server_stop_error err=%s", e)
            self._host_server = None
            os.environ.pop("SLIFE_HOST_PORT", None)
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._schedule_task is not None:
            self._schedule_task.cancel()
            try:
                await self._schedule_task
            except asyncio.CancelledError:
                pass
            self._schedule_task = None
        if self._schedule_startup_task is not None:
            self._schedule_startup_task.cancel()
            try:
                await self._schedule_startup_task
            except asyncio.CancelledError:
                pass
            self._schedule_startup_task = None
        for t in list(self._timer_tasks):
            t.cancel()
        if self._timer_tasks:
            await asyncio.gather(*self._timer_tasks, return_exceptions=True)
            self._timer_tasks.clear()
        if self._inbox_task is None:
            return
        self._inbox_task.cancel()
        try:
            await self._inbox_task
        except asyncio.CancelledError:
            pass
        self._inbox_task = None
        logger.info("inbox_stopped")

    # ── A2A lifecycle ──────────────────────────────────────────────────

    # A2A lifecycle: enable gate (config + Mosquitto probe, downgrades
    # a2a_config.enabled) and the drain glue live in the plugin behavior
    # methods (_gate_a2a / _after_ready_a2a / the a2a poll loop below) — the
    # uniform engine drives them, there is no start_a2a.

    async def _a2a_poll_loop(self, interval: float = 1.0) -> None:
        """Drain inbound a2a tasks/presence from the plugin into the inbox.

        The harness stays a thin client: it only drains the plugin's
        ``__a2a_drain_incoming`` and feeds the unified inbox.  Completion is
        the model's job, controlled through the message type: an inbound task
        (every inbound A2A exchange is a task) is answered by sending a
        ``message_type="task_response"`` message with the task_id — never a
        one-turn harness dispatch, because a task may need many turns.  The
        harness no longer auto-dispatches the turn's final text out.
        """
        import json as _json
        from slife.a2a.identity import AgentName, AgentMessage, Channel
        from slife.a2a.card import AgentCard, format_presence_line
        from slife.agent.message_history import a2a_marker

        logger.info("a2a_poll_loop_start interval=%.1fs", interval)

        while True:
            try:
                client = self._plugins["a2a"].client
                if client is None:
                    break
                result = await client.call_tool("__a2a_drain_incoming", {})
                data = _json.loads(result)

                # Inbound tasks orphaned by a restart — state, not a queue:
                # replaced wholesale so the turn prompt's reminder follows the
                # mesh down to empty as peers are answered.
                self.set_a2a_stale(data.get("stale_tasks", []))

                for ev in data.get("tasks", []):
                    # Every inbound A2A exchange is a task and carries its
                    # task_id; surface the same id to the receiver so it can
                    # reference the task it is responding to instead of making
                    # one up (a reported mismatch in round-trips).
                    task_text = ev.get("content", "")
                    src = ev.get("source", "unknown")
                    kind = ev.get("kind", "task")
                    task_id = ev.get("task_id") or ev.get("correlation_id") or ""
                    # The [A2A:…] marker's `from` names the sending peer —
                    # never the receiver — and its task id, so the LLM can
                    # attribute the turn and reference the task it is
                    # responding to instead of making one up.  Only TASK_REQUEST
                    # creates a task: a MESSAGE conversation is task-less
                    # (marker without task_id, no completion).  The TUI drops
                    # it for display (A2A(<name>)> bubble prefix).
                    marker_id = task_id or None if kind == "task" else None
                    marker_type = "message" if kind != "task" else "task_request"
                    task_text = (
                        f"{a2a_marker(src, marker_id, type=marker_type)}"
                        f"{task_text}"
                    )
                    msg = AgentMessage(
                        source=AgentName(src),
                        content=task_text,
                        correlation_id=task_id,
                        # No on_reply: a task may take many turns and is
                        # completed ONLY when the model sends
                        # a2a_send_message(message_type="task_response",
                        # task_id=…) — never by auto-dispatching this turn's
                        # final text out (a harness-complete is wrong for
                        # multi-turn work).
                        metadata={"a2a_kind": kind},
                        channel=Channel.a2a(src),
                    )
                    await self.inbox.post(msg)
                    logger.debug(
                        "a2a_in source=%s task=%.80s",
                        msg.source, ev.get("content", ""),
                    )

                # A peer withdrew an inbound task.  A task that never started
                # is dropped outright (nothing was done, so there is nothing
                # to judge and nothing to tell).  One that is running — or was
                # running and is still open across turns — reaches the model
                # as an ordinary inbound message, the road every other peer
                # event takes, because whether work on that task is still
                # going on is known only to the model.
                #
                # Placed AFTER this batch's tasks on purpose: a withdrawal
                # always refers to a task the peer sent earlier, so by the
                # time we look, an unstarted one is in the queue (droppable),
                # and the queue order the model reads is the arrival order.
                for cev in data.get("cancellations", []):
                    # Shape-guarded like the presence loop below: a malformed
                    # entry must not abort the drain — the plugin already
                    # cleared its queues, so the rest of this batch (auto-
                    # pushed completions, presence) would be lost for good.
                    if not isinstance(cev, dict):
                        continue
                    task_id = cev.get("corr_id", "")
                    if not task_id:
                        continue
                    if self.inbox.drop_queued(task_id):
                        logger.info(
                            "a2a_cancel_dropped_queued task=%s", task_id,
                        )
                        continue
                    peer = cev.get("peer", "unknown")
                    msg = AgentMessage(
                        source=AgentName(peer),
                        content=(
                            f"{a2a_marker(peer, task_id, type='cancel_task')}"
                            f"The sender withdrew this task — stop working "
                            f"on it."
                        ),
                        # No correlation_id: nothing expects a completion, and
                        # the id a *reply* would need rides the marker above.
                        metadata={"a2a_kind": "cancel_task"},
                        channel=Channel.a2a(peer),
                    )
                    await self.inbox.post(msg)
                    logger.debug(
                        "a2a_cancel_in source=%s task=%.80s", peer, task_id,
                    )

                # Fire-and-forget broadcast events — passive, no task_id, no
                # completion: informational input the agent may act on.
                for ev in data.get("events", []):
                    src = ev.get("source", "unknown")
                    content = ev.get("content", "")
                    if not content:
                        continue
                    msg = AgentMessage(
                        source=AgentName(src),
                        content=f"{a2a_marker(src, type='broadcast')}{content}",
                        metadata={"a2a_kind": "event"},
                        channel=Channel.a2a(src),
                    )
                    await self.inbox.post(msg)

                for pev in data.get("presence", []):
                    # The presence card comes off the wire from any peer —
                    # guard the shape so a malformed entry can't crash the
                    # drain loop (which would freeze all A2A processing).
                    card_data = pev.get("card") if isinstance(pev, dict) else None
                    if not isinstance(card_data, dict):
                        continue
                    card = AgentCard(
                        agent_name=AgentName(card_data.get("agent_name", "?")),
                        status=card_data.get("status", "idle"),
                    )
                    text = format_presence_line(card, pev.get("event", ""))
                    if text is not None:
                        self._presence_events.append((_time.time(), text))
                    await self._notify_activity(
                        "agent_change", event=pev.get("event", ""), card=card,
                    )

                # Outbound async-task results (auto-push) — the peer's result
                # arrived over MQTT; surface it so the agent doesn't need to
                # poll or block on subscribe.
                for cev in data.get("task_completions", []):
                    corr_id = cev.get("corr_id", "")
                    result = cev.get("result", "")
                    peer = cev.get("peer", "") or corr_id or "peer"
                    cancelled = bool(cev.get("cancelled"))
                    kind = cev.get("kind", "task")
                    if not result and not cancelled:
                        continue
                    if kind == "message":
                        # A MESSAGE conversation was answered: a bare message
                        # reply, no task involved.
                        content = (
                            f"{a2a_marker(peer, type='message')}"
                            f"Peer **{peer}** replied to your message:\n\n"
                            f"{result}"
                        )
                    else:
                        state = "cancelled" if cancelled else "completed"
                        content = (
                            f"{a2a_marker(peer, corr_id or None, type='task_response')}"
                            f"Peer **{peer}** {state} async task "
                            f"(ID: `{corr_id}`):\n\n{result}"
                        )
                    await self.inbox.post(AgentMessage(
                        source=AgentName(peer),
                        content=content,
                        # A result push is an FYI (a response to a prior request),
                        # never a task — frames the injected pair accordingly.
                        metadata={"a2a_kind": "push"},
                        channel=Channel.a2a(peer),
                    ))
                    logger.debug(
                        "a2a_completion_autopushed peer=%s task=%s",
                        peer, corr_id,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("a2a_poll_error err=%s", e)

            await asyncio.sleep(interval)

        logger.info("a2a_poll_loop_stop")


    def set_inbox_handler_factory(self, factory) -> None:
        """Register a factory that creates TUI handlers for inbox messages.

        Called by the TUI layer so remote A2A tasks always have a handler
        available, even before the first human message is typed.
        """
        if self.inbox is not None:
            self.inbox._histories.set_default_handler_factory(factory)

        logger.info("a2a_init_done tools=%d", len(self.tool_registry.list_tools()))

    # ── Subagent lifecycle ─────────────────────────────────────────────

    async def start_subagent(self) -> None:
        """Set up local subagent spawning (stdin/stdout pipes).

        Recursion is allowed — a subagent can spawn its own descendants.

        Independent of A2A over MQTT — both transports coexist.
        """
        logger.info("subagent_init_start")

        from slife.subagent.process import SubagentManager, set_manager
        self._subagent_manager = SubagentManager(self.config)

        # Set module-level transport reference so builtin subagent tools
        # (Slife.tools.subagent) can access the live manager at call time.
        set_manager(self._subagent_manager)

        # When a subagent completes an async task, push the result into
        # the inbox so the user sees it without having to poll.  A worker
        # that ran a scheduled task is reported as the task completing
        # (hides the subagent detail); what is said is decided by the run
        # record, not the worker's narration — a worker whose report
        # generation died is never announced as "report saved".
        async def _on_subagent_done(agent_name: str, task_id: str, result: str) -> None:
            from slife.a2a.identity import AgentMessage, Channel
            from slife.agent.message_history import subagent_marker
            from slife.subagent.identity import SUBAGENT
            from slife.agent.schedules import (
                _SCHEDULE_WORKERS, _schedule_completion_content,
            )
            scheduled = agent_name in _SCHEDULE_WORKERS
            if scheduled:
                content = await _schedule_completion_content(self, agent_name)
            else:
                # The [Subagent:…] marker names the worker and task id so the
                # LLM can attribute the pushed result; the TUI drops it for
                # display (the channel already shows as Subagent(<name>)>).
                content = (
                    f"{subagent_marker(agent_name, task_id)}"
                    f"Subagent **{agent_name}** completed async task "
                    f"(ID: `{task_id}`):\n\n"
                    f"{result}"
                )
            msg = AgentMessage(
                source=SUBAGENT,
                content=content,
                channel=Channel.subagent(
                    agent_name, task_id=task_id, scheduled=scheduled,
                ),
            )
            await self.inbox.post(msg)

        self._subagent_manager.on_task_complete = _on_subagent_done

        logger.info("subagent_init_done tools=%d", len(self.tool_registry.list_tools()))
        from slife.health import record
        record(
            "subagent", "ok",
            key="status", value=(
                "ready (max_subagents="
                f"{(self.config.subagent_config or {}).get('max_subagents', '?')})"
            ),
        )

    async def stop_subagent(self) -> None:
        """Stop all local subagents and clean up."""
        if self._subagent_manager:
            try:
                await self._subagent_manager.stop_all()
            except Exception as e:
                logger.debug("subagent_stop_all_error err=%s", e)
            self._subagent_manager = None

        # Clear module-level transport reference
        from slife.subagent.process import clear_manager
        clear_manager()

        logger.info("subagent_shutdown")

    def _drain_presence_events(self) -> list[tuple[float, str]]:
        """Return pending presence events and clear the buffer.

        Called by ``AgentLoop`` at the start of each turn (read-once):
        events that happened since the last turn are injected into the
        turn prompt exactly once.  If the buffer ever grows
        pathologically large it is trimmed here, not silently at
        render time — the oldest entries are dropped with a warning.
        """
        if not self._presence_events:
            return []
        if len(self._presence_events) > 1000:
            logger.warning(
                "presence_events_overflow dropped=%d",
                len(self._presence_events) - 1000,
            )
        events = list(self._presence_events)[-1000:]
        self._presence_events.clear()
        return events

    def set_schedule_pending(self, runs: list[dict]) -> None:
        """Publish the open failed/missed scheduled runs (turn-prompt data).

        Written by the schedule loop and the startup sweep (schedules.py)
        on their cadence; read by the ``_turn_prompt`` each turn.
        Each item is ``{name, due_at, status}`` — failed and missed are
        the same question ("backfill or skip?") to the user, with
        ``status`` kept so the turn prompt can show which one it was.
        """
        self._schedule_pending = runs

    def _schedule_pending_provider(self) -> list[dict]:
        return self._schedule_pending

    def set_a2a_stale(self, tasks: list[dict]) -> None:
        """Publish the A2A tasks orphaned by a restart (turn-prompt data).

        Replaced wholesale on each a2a drain — the plugin owns this set and
        drops an entry when its peer is answered, so the reminder clears
        itself without the harness tracking anything.  Each item is
        ``{task_id, peer, since}``.
        """
        self._a2a_stale = tasks

    def _a2a_stale_provider(self) -> list[dict]:
        return self._a2a_stale

    async def _notify_activity(self, kind: str, **kwargs) -> None:
        """Fire all registered activity callbacks (TUI-only; never a turn)."""
        for cb in self._on_activity_callbacks:
            try:
                await cb(kind, **kwargs)
            except Exception:
                pass

    def on_activity(self, callback) -> None:
        """Register a callback for activity events (TUI notification).

        The channel is the TUI's single activity feed — **not** an A2A one.
        Whoever has something the user should see emits here; A2A is one
        contributor among several, and only ``agent_change`` is its own.

        Callback signature: ``async def cb(kind: str, **kwargs)``.  Kinds:
        the inbox's own (``busy`` / ``idle`` / ``task_received`` /
        ``peer_message`` / ``subagent_message`` / ``loop_error`` /
        ``task_completed``), A2A presence (``agent_change``), and anything a
        service-side task reports (e.g. a tool-set sync).

        Nothing emitted here reaches the model: it is a chat-view line, never
        an ``AgentMessage``, never a turn, never context.
        """
        self._on_activity_callbacks.append(callback)

    # ── Message processing ────────────────────────────────────────────

    async def process_message(
        self,
        user_input: str,
        images: list[str] | None,
        handler: AgentEventHandler,
    ) -> AgentResult:
        """Run the agent loop for a user message via streaming.

        All messages (human keyboard, A2A, WeChat) go through the
        unified inbox queue — processed serially, never cancelled.
        """
        from slife.a2a.identity import AgentMessage, Channel

        msg = AgentMessage(
            source=HUMAN,
            content=user_input,
            images=images if images else [],
            handler=handler,
            channel=Channel.human(),
        )
        await self.inbox.post(msg)

        # Return a placeholder — TUIHandler will update the UI
        # as streaming events arrive.  The actual result is not
        # available synchronously with the inbox model.
        return AgentResult(text="", usage=TokenUsage())
