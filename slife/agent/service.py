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
from typing import cast

from slife.agent.system_prompt import build as build_system_prompt
from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.message_history import MessageHistory, turn_header
from slife.agent.loop import AgentLoop, AgentEventHandler, AgentResult
from slife.agent.inbox import Inbox, MemorySaveError, MessageHistoryStore
from slife.agent.plugins import (
    PluginBehavior,
    PluginLifecycle,
    PluginRegistry,
    PluginStartStatus,
    plugin_port_env,
)
from slife.plugins.spec import PLUGIN_SPECS
from slife.a2a.identity import HUMAN
from slife.tools.factory import create_tools_from_config
from slife.mcp.tool_adapter import create_proxy_tools
from slife.platform import terminate_process_sync
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
# by these constants, and surfaces "tunnel down" only once.
_TUNNEL_SETTLE_TIMEOUT = 20.0  # seconds — max wait for the eager attempt
_TUNNEL_PROBE_INTERVAL = 1.0   # seconds — between __check probes


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
# restore can fill the context floor, and keeps turn_search recall cheap.
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

    def __init__(self, config: Config, is_subagent: bool = False):
        self.config = config
        # __post_init__ guarantees these are never None at runtime
        assert self.config.a2a_config is not None
        assert self.config.subagent_config is not None
        self.is_subagent = is_subagent

        # Build the shared ToolContext, which carries the config + registry
        # that tools need at runtime.
        from slife.tools.context import ToolContext
        self._tool_ctx = ToolContext(config=config)
        self.tool_registry = create_tools_from_config(
            config.tools, config=config, is_subagent=is_subagent,
            ctx=self._tool_ctx,
        )
        # Backfill the registry reference (created by the factory)
        self._tool_ctx.registry = self.tool_registry
        self.llm_client = LLMClient(config.active_model)
        # Max tool result = tool_result_ceiling × context_window × 3 chars/token
        max_tool_result_chars = int(
            config.tool_result_ceiling
            * config.active_model.context_window
            * 3
        )
        # Pending A2A peer presence events (epoch, TUI line), drained into
        # the context footer on the next turn.  Unbounded by design — events
        # are consumed on every turn, so the steady-state size is "events
        # since last turn"; the guard below only protects against a
        # pathological long-idle + heavy-flapping session.
        self._presence_events: deque[tuple[float, str]] = deque()
        # Open failed/missed scheduled runs, surfaced in the ``_sys_note``
        # footer until the user backfills or skips them.  Refreshed by the
        # schedule loop and the startup sweep (see schedules.py).
        self._schedule_pending: list[dict] = []
        # Subagents fail fast on LLM errors and cap a single stream call:
        # they have no user to wait on, so a raised provider error / timeout
        # must surface as a pushed-back result rather than retrying a flaky
        # provider or hanging on a silent stall.  The main agent keeps the
        # module defaults (retry transient transport errors, no stream cap).
        subagent_stream_timeout = (
            (config.subagent_config or {}).get("task_timeout", 120)
            if is_subagent else None
        )
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
            max_tool_result_chars=max_tool_result_chars,
            tool_timeout=config.tool_timeout,
            context_window=config.active_model.context_window,
            context_ceiling=config.context_ceiling,
            context_floor=config.context_floor,
            supports_vision=config.active_model.supports_vision,
            model_name=config.active_model.display_name,
            input_modalities=", ".join(config.active_model.input_modalities),
            presence_provider=self._drain_presence_events,
            schedule_provider=self._schedule_pending_provider,
            advance_context_start=self.advance_context_start,
            stream_timeout=subagent_stream_timeout,
            stream_max_retries=0 if is_subagent else None,
        )
        self.message_history = MessageHistory(
            system_prompt=build_system_prompt(self.config),
        )
        self._tool_ctx.message_history = self.message_history
        # Runtime iteration-cap hook for the set_max_iterations tool.
        self._tool_ctx.set_max_iterations = self.agent_loop.set_max_iterations
        # USER.md write hook for the add_user_pref tool — re-render the
        # system prompt (re-reads USER.md) so the new preference is live
        # from the next call.  Populated for the main agent and subagents.
        self._tool_ctx.refresh_system_prompt = self.refresh_system_prompt
        # Scheduled-task manual-fire hook for the run_schedule_now tool.
        # Subagents are workers, never the scheduler — leave it None there.
        if not is_subagent:
            self._tool_ctx.fire_schedule_now = self.fire_schedule_now
            self._tool_ctx.schedule_wakeup = self.schedule_wakeup
        # Live-context boundary hook — the trim and clear_context (one big
        # trim) advance the boundary so a restart rebuilds the exit-time
        # context.  The bound method resolves the memdb client at call time
        # (it is not connected during __init__), so a missing/unready
        # client degrades to "skip" rather than raising.
        self._tool_ctx.advance_context_start = self.advance_context_start
        self._tool_ctx.reset_context_time = self.agent_loop.reset_context_time
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
        # initialize handshake either completed or the spawn failed).
        # The TUI input and the inbox consumer gate on this.
        # Event-driven: set by the last spawn's ``finally``, never polled.
        self._startup_plugins: set[str] = set()
        self._startup_settled = asyncio.Event()

        # ── Unified message queue (always active) ──────────────────
        # Every input — human keyboard, A2A MQTT, WeChat — flows
        # through the same inbox queue.  Processed serially.
        histories = MessageHistoryStore(
            system_prompt=build_system_prompt(self.config),
        )
        histories._by_source[HUMAN] = self.message_history

        self.inbox = Inbox(
            agent_loop=self.agent_loop,
            histories=histories,
            on_activity=self._notify_a2a_activity,  # always active for WeChat etc.
            on_turn_complete=self.save_to_memory,
            # Startup gate: no turn runs until every plugin spawn converged.
            # Main agent only — subagents share the main process's plugins
            # and spawn none themselves, so nothing would ever set the
            # event; their inbox must not gate on it.
            ready=(
                None if self.is_subagent
                else self.wait_startup_settled
            ),
        )
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
        # On-demand reconcile guard: prevents concurrent mcp_tool_load /
        # tools/list_changed reconciliation from racing.
        self._mcp_reconciling: bool = False

        # A2A integration state
        self._subagent_manager = None
        self._on_a2a_callbacks: list = []  # callbacks for TUI notification

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

    @property
    def context_window(self) -> int:
        """Context window size (tokens) of the active model."""
        return self.config.active_model.context_window

    @property
    def current_context_tokens(self) -> int:
        """Context tokens the next API call would send — same single source
        as ``_sys_note`` (see :meth:`AgentLoop.context_tokens_for`):
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
        raw = self.config._read_config("switch_model", ref)
        if raw is not None:
            raw["active_model"] = ref
            self.config._write_config(raw)
        self.reload_active_model(ref)
        return f"Switched to {self.config.active_model.display_name}"

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
        # real prompt_tokens (or, on a freshly restored session, the
        # previous turn's persisted prompt_tokens that restore_session
        # primed into _last_usage).  Wiping either on a switch made the
        # first _sys_note after a restart-with-model-restore (cc-switch
        # restoring the recorded active model before the first turn)
        # report "Context usage: 0" even though the exit context WAS
        # restored.  The reading self-corrects on the next API call
        # anyway, so the switch stays a no-op here.

        # Rebuild system prompt with updated model info — for the human
        # history AND every persistent one (WeChat) and future ones.
        self.refresh_system_prompt()

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
        from slife.agent.plugins import PLUGIN_SPAWN_TIMEOUT

        self._startup_plugins.add(name)
        try:
            # Hang guard on the spawn await — convergence must fire even for
            # a stuck child, so the service can still open.  Not a
            # readiness deadline: the normal path settles as fast as the
            # real spawn, no timing guess involved.
            async with asyncio.timeout(PLUGIN_SPAWN_TIMEOUT):
                return await self._start_plugin_server_impl(name, module)
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
            # Expose the plugin's live client where its native tools/health
            # checks read it.
            setattr(self._tool_ctx, spec.ctx_field, lc.client)
        bhv = self._plugin_behaviors.get(spec.name)
        if bhv is not None and bhv.after_ready is not None:
            await bhv.after_ready(lc)
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
        from slife.health import record
        record(
            "wechat_service", "ok",
            key="status", value="connected",
            hint="WeChat plugin started and tools registered.",
        )

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
        from slife.health import record
        record(
            "a2a", "ok",
            key="status", value="connected",
            hint="A2A P2P mesh connected (plugin).",
        )
        logger.info("a2a_plugin_started")

    async def _after_ready_sharefile(self, lc) -> None:
        """After a sharefile child is ready: watch its eager ngrok tunnel
        attempt and surface "tunnel down" on a terminal failure.  (The child's
        port env is published by the generic spawn on start AND every restart,
        so subagents always inherit the live port — nothing to do here.)"""
        self._watch_sharefile_tunnel(lc)

    async def _after_ready_mcp(self, lc) -> None:
        """After the gateway child is ready: wire the mcp enrichment (expose
        the wrapper client, register external-server tool proxies)."""
        await self._wire_mcp_glue()

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
        for tool in proxy_tools:
            if tool.name not in old_names:
                self.tool_registry.register(tool)
        for stale in old_names - new_names:
            self.tool_registry.unregister(stale)
        lifecycle.registered_tools = new_names
        logger.debug(
            "plugin_tools_resync name=%s added=%d removed=%d total=%d",
            name, len(new_names - old_names), len(old_names - new_names),
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
        bounded by ``_TUNNEL_SETTLE_TIMEOUT`` — an unresolved state within
        the window stays silent rather than guessing.
        """
        deadline = _time.monotonic() + _TUNNEL_SETTLE_TIMEOUT
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
            # e.g. the native `_sys_note`.)
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
            self._plugins[name].registered_tools = {t.name for t in proxy_tools}
            for tool in proxy_tools:
                self.tool_registry.register(tool)

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

            # Readiness (MCP plugin contract): create_client() ran the
            # initialize handshake — completing it is the plugin's ready
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
    # mcp-plugin is an external standalone distribution that self-hosts its
    # config (mcp-plugin.json5) and self-connects servers on startup.  This
    # is the ONE bounded, mcp-aware integration left in the harness: expose
    # the wrapper client to slife tools and register the external servers'
    # ``{server}__{tool}`` proxies as native tools.  Persistence, auto-connect
    # and reconciliation happen inside the plugin, not here.  The lifecycle
    # itself (spawn / connect / watchdog) is the generic one all plugins use.

    async def _wire_mcp_glue(self) -> None:
        """Wire the gateway enrichment after (re)connect: client + tool proxies.

        Idempotent — re-arming after a watchdog respawn re-points the tool
        context and re-registers the external servers' tools.
        """
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

    async def _push_gateway_port_to_jobs(self, lc) -> None:
        """Best-effort push of the gateway's port to the job-coding plugin.

        job-coding's ``mcp`` handle resolves the gateway port lazily, but a
        child's spawn-time env snapshot never changes — a gateway restarted
        on a new port would leave live jobs pointing at the old endpoint.
        Re-pointing via the internal tool fixes that.  Non-fatal by design:
        job-coding may not be up yet (its rebuild inherits the current env;
        a later gateway event re-pushes) and a dead client just logs.
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
        """Reconcile external MCP tool proxies (on-demand model).

        Reads the configured server list LIVE from the wrapper (``mcp_list``),
        so neither slife nor subagents need mcp-plugin.json5.  Two jobs:

        1. Servers with ``auto_load: true`` get their tools bulk-registered
           (full-diff, unchanged — ``_discover_and_register_external_tools``).
        2. Every OTHER loaded EXTERNAL proxy (an on-demand ``mcp_tool_load``)
           is validated via ``__mcp_get_tool`` — if the tool vanished, its
           server disconnected, or it was disabled, the proxy is unregistered.

        This is the ONLY tool-maintenance path for non-auto_load servers:
        external tools are on-demand by default and there is no bulk
        registration for them.
        """
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

        lc = self._gateway_lifecycle()
        client = lc.client if lc is not None else None
        if client is None or not client.is_connected:
            return
        if self._mcp_reconciling:
            return
        self._mcp_reconciling = True
        try:
            try:
                raw = await client.call_tool("mcp_list")
                servers = json.loads(raw)
            except Exception as e:
                logger.debug("mcp_reconcile_list_failed err=%s", e)
                servers = []
            auto_servers: set[str] = set()
            if isinstance(servers, list):
                for s in servers:
                    if (
                        isinstance(s, dict)
                        and s.get("enabled") is not False
                        and s.get("auto_load") is True
                        and s.get("name")
                    ):
                        auto_servers.add(s["name"])

            for name in auto_servers:
                try:
                    await self._discover_and_register_external_tools(server_name=name)
                except Exception:
                    logger.debug("mcp_auto_load_sync_failed server=%s", name, exc_info=True)

            # Validate on-demand proxies (those not owned by an auto_load
            # server — the full-diff path owns auto_load tools).
            for tool in list(self.tool_registry.list_tools()):
                if not (isinstance(tool, MCPProxyTool) and tool._route == ProxyRoute.EXTERNAL):
                    continue
                if getattr(tool, "_server", "") in auto_servers:
                    continue
                try:
                    raw = await client.call_tool("__mcp_get_tool", {"full_name": tool.name})
                    data = json.loads(raw)
                except Exception:
                    data = {"status": "error"}
                if data.get("status") != "ok" or data.get("enabled") is not True:
                    if self.tool_registry.unregister(tool.name):
                        logger.debug("mcp_proxy_reconciled_unload full_name=%s", tool.name)
        finally:
            self._mcp_reconciling = False

    async def _register_external_server_tools(self, name: str = "", **kwargs) -> None:
        """mcp_set / mcp_set_enabled connected a server — reconcile proxies.

        (Persistence happens inside mcp-plugin; this only touches the registry.)
        """
        if name:
            await self._sync_mcp_proxies()

    async def _unregister_external_server_tools(self, name: str = "", **kwargs) -> None:
        """mcp_remove / mcp_set_enabled disabled a server — drop its tools."""
        if not name:
            return
        removed = self.tool_registry.unregister_by_prefix(f"{name}__")
        if removed:
            logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)

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
            # Reconcile external {server}__{tool} proxies (auto_load + on-demand
            # validation) — the same network the main agent's _wire_mcp_glue
            # uses, mirrored for a worker sharing the gateway.
            await self._sync_mcp_proxies()
        elif spec.ctx_field is not None:
            setattr(self._tool_ctx, spec.ctx_field, self._plugins[name].client)
        logger.info("%s_http_connect_done tools=%d", name, len(self.tool_registry.list_tools()))

    async def _register_plugin_tools(self, name: str) -> None:
        """Discover and register a connected plugin's tools as proxy tools.

        Filters out internal tools (names starting with ``__``), creates
        proxy tools, and registers them under their bare semantic names
        (built-in plugin tools are first-class, like native tools — no
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
        for stale in old_names - new_names:
            self.tool_registry.unregister(stale)
        # Record the exact registered names so dead-process cleanup and stop
        # can unregister this plugin's bare-name tools without a prefix.
        self._plugins[name].registered_tools = new_names
        for tool in proxy_tools:
            self.tool_registry.register(tool)
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
                "mcp_list_tools", {"server": server_name}
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
            from slife.health import record
            record(
                "mcp_server", "ok",
                key=server_name, value="connected",
                hint=f"MCP server '{server_name}' connected.",
                replace=True,
            )
            logger.debug(
                "mcp_tools_registered server=%s count=%d",
                server_name, len(proxy_tools),
            )
        except Exception as e:
            logger.error("mcp_discover_failed server=%s err=%s", server_name, e)
            from slife.health import record
            record(
                "mcp_server", "warning",
                key=server_name, value="discovery_failed",
                hint=f"MCP server '{server_name}' connected but tool discovery failed: {e}",
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

    def kill_child_processes(self) -> None:
        """Synchronous best-effort child process cleanup.

        Called from the finally block in main() — no event loop required.
        Directly terminates known subprocesses so they don't become
        orphans holding log file handles on Windows.

        Scans for all ``_<name>_process`` attributes — works with
        auto-discovered plugins, not just the three built-ins.
        """
        # Kill known plugin processes first
        for plugin in self._plugins.values():
            plugin.kill()

        # Scan for auto-discovered plugins
        for attr_name in dir(self):
            if not attr_name.endswith("_process") or not attr_name.startswith("_"):
                continue
            wrapper = getattr(self, attr_name, None)
            if wrapper is None:
                continue
            p = getattr(wrapper, "_process", None)
            if p is None:
                continue
            terminate_process_sync(p, label=attr_name)

        # Subagent manager cleanup
        mgr = self._subagent_manager
        if mgr is not None:
            for name in list(mgr._subagents.keys()):
                proc = mgr._subagents.get(name)
                if proc is not None and proc._process is not None:
                    terminate_process_sync(proc._process, timeout=2.0, label=f"subagent-{name}")

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
        from slife.agent.message_history import WECHAT_MARKER

        logger.info("wechat_poll_loop_start interval=%.1fs", interval)

        while self.wechat_enabled:
            try:
                assert self._plugins["wechat"].client is not None

                result = await self._plugins["wechat"].client.call_tool(
                    "__wechat_drain_incoming", {},
                )
                data = _json.loads(result)
                msgs = data.get("messages", [])

                for m in msgs:
                    from_id = m.get("to_user_id", "")
                    text = m.get("text", "")

                    if not text.strip():
                        continue

                    msg = AgentMessage(
                        source=WECHAT,
                        content=f"{WECHAT_MARKER}{text}",
                        metadata={"channel": "wechat"},
                        channel=Channel.wechat(),
                    )
                    await self.inbox.post(msg)
                    logger.debug("wechat_in from=%s text=%.100s", from_id, text)

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
        prompt_tokens: int | None = None,
        history: "MessageHistory | None" = None,
        channel: str = "",
        channel_data: str = "{}",
        created_at: "datetime | str | None" = None,
        handler: "object | None" = None,
    ) -> None:
        """Save the just-completed turn as a new row in memory.

        Args:
            user_message: The user's input text.
            token_count: Cumulative token usage for the turn (billing).
            prompt_tokens: The LAST LLM call's prompt_tokens — the exact
                context size at turn end.  Persisted so restore primes the
                footer / _sys_note with the real exit-time occupancy.
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
        """
        # Accumulate turn's billed tokens into the session total.
        if token_count:
            self.session_usage.total_tokens += token_count

        conv = history if history is not None else self.message_history

        # Invariant: never persist an inconsistent turn.  Repair orphaned
        # tool_calls and close the turn if needed BEFORE extracting — the
        # same ensure used on load and before each user message.
        conv._ensure_turn_consistent()

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
        # all (the turn was rolled back on a content-policy / bad-request
        # error), there is nothing to persist.
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
        # being persisted (the live history keeps its note).
        from slife.agent.message_history import MessageHistory as _MH
        turn_messages = _MH.strip_trim_markers(turn_messages)

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
            "prompt_tokens": prompt_tokens or 0,
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
                timeout=10.0,
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
                # turn's real prompt_tokens are now known (the last API call's
                # usage), so the ceiling check is exact, and a trim can never
                # lose an unsaved turn.  Operates on *conv* (the history
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
        reference the turn precisely.  Heartbeat turns are skipped (their
        user message is a synthetic trigger).  Purely additive and
        best-effort — a failure leaves the message unannotated.
        """
        if rowid is None:
            return
        msgs = history.messages
        if not (0 <= user_idx < len(msgs)) or msgs[user_idx].get("role") != "user":
            return
        from slife.agent.schedules import is_autonomous_trigger

        content = msgs[user_idx].get("content", "")
        if isinstance(content, str):
            if is_autonomous_trigger(content):
                return
        elif isinstance(content, list):
            joined = "".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )
            if is_autonomous_trigger(joined):
                return
        else:
            return

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
        if isinstance(content, str):
            msgs[user_idx]["content"] = content + " " + header
        else:
            msgs[user_idx]["content"] = list(content) + [
                {"type": "text", "text": " " + header}
            ]

    async def get_recent_turns(self, limit: int = 20) -> tuple[list[dict], int, int]:
        """Load recent turns for restore. Returns ([], 0, 0) if no turns.

        Restores the **exit-time context** verbatim: it reads the persisted
        live-context start boundary (:meth:`SessionStore.get_context_start`,
        maintained by ``_trim_after_save`` / ``clear_context``) and returns
        **every** turn recorded after it — the exact slice the agent was
        working with when it exited.  No re-slicing against the ceiling: the
        boundary already encodes the trimmed state, so restore simply replays
        it (the agent picks up exactly where it left off).

        Fetches newest-first in batches of *limit* (each batch already
        newest-first, so appending stays globally newest-first), then reverses
        to **oldest-first** so the restore rebuilds the history
        chronologically.  Heartbeat turns are included — they restore as
        ⚡ 自主, consistent with the live TUI.

        Returns ``(selected, skipped, budget)`` — *skipped* is always 0 (no
        turns are dropped for a budget), *budget* is 0 (no ceiling cap: the
        boundary already bounds what is restored).  Kept as a 3-tuple so the
        call site and ``restore_session`` stay compatible.

        A defensive hard cap (2× the ceiling) guards against a stale
        boundary of 0 from a pre-boundary DB: it would otherwise replay the
        entire history at once.  Normal operation never reaches it — the
        live trim bounds the in-context slice well below the ceiling.

        Reads directly from SQLite — independent of the memory plugin / MCP.
        """
        store = None
        db_path = None
        try:
            from slife.plugins.memdb.store import SessionStore
            from slife.ui.restore import estimate_turn_tokens

            db_path = self._get_memory_db_path()
            if not (db_path and db_path.is_file()):
                return [], 0, 0
            store = SessionStore(db_path)
            await store.setup(embedding_dim=0)
            start_rowid = await store.get_context_start()

            # Accumulate newest-first batches after the live-context boundary
            # until exhausted.  The defensive cap stops a pre-trim boundary
            # (0, not yet trimmed) from replaying unbounded history.
            hard_cap = int(
                self.config.active_model.context_window
                * self.config.context_ceiling * 2
            )
            all_turns: list[dict] = []
            total = 0
            offset = 0
            while total < hard_cap:
                batch = await store.get_recent_turns(
                    limit=limit, offset=offset, after_rowid=start_rowid,
                )
                if not batch:
                    break
                all_turns.extend(batch)
                total += sum(estimate_turn_tokens(t) for t in batch)
                offset += limit

            all_turns.reverse()  # oldest-first for restore
            return all_turns, 0, 0
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

    async def advance_context_start(self, count: int) -> bool:
        """Persist the live-context boundary after a cut removed *count*.

        The one cut-op behind every context cut: called by the internal
        trim (``AgentLoop._trim_after_save``) after it evicts the oldest
        turns, and by ``clear_context`` (a one-shot clear is one big trim —
        a generous count, the advance lands on the last row).  Either way,
        a restart rebuilds the exit-time context from exactly where the
        live one stood.  Best-effort — if the memdb channel is unreachable
        the boundary just stays stale, which makes the next restore a
        *superset* (old trimmed turns come back searchable in context),
        never a loss.
        """
        if count <= 0 or not self.memdb_enabled:
            return False
        plugin = self._plugins.get("memdb")
        client = getattr(plugin, "client", None) if plugin else None
        if client is None:
            return False
        try:
            await asyncio.wait_for(
                client.call_tool(
                    "__memory_context_start_advance", {"count": count},
                ),
                timeout=10.0,
            )
            return True
        except Exception:
            logger.warning("context_start_advance_skipped count=%d", count)
            return False

    def _get_memory_db_path(self) -> Path | None:
        """Return the memory database path."""
        import os
        from slife.paths import get_data_dir

        env_path = os.environ.get("SLIFE_MEMDB_DB")
        if env_path:
            return Path(env_path)
        agent_name = os.environ.get("SLIFE_AGENT_NAME", "slife")
        return get_data_dir() / f"{agent_name}.db"

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

    async def fire_schedule_now(self, name: str, due_at: str = "") -> str:
        """Run a scheduled task immediately (backfill / manual trigger).

        Delegates to :func:`slife.agent.schedules.fire_task_now`, which
        records a run and injects the task's trigger into the inbox.  *due_at*
        targets an exact run (a missed/failed backfill); omit for a fresh
        cron-fire run at now.
        """
        from slife.agent.schedules import fire_task_now

        return await fire_task_now(self, name, due_at)

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

    async def start_inbox(self) -> None:
        """Start the inbox background processor.

        Called during app startup before A2A/WeChat so the queue is
        ready to accept messages from any input channel.
        """
        if self._inbox_task is not None:
            return
        self._inbox_task = asyncio.create_task(self.inbox.run())
        logger.info("inbox_started")

        # slife-as-plugin — the in-process MCP server exposing the live
        # ToolRegistry to external MCP consumers (DESIGNER_NOTES §8).  Main
        # agent only; subagents are workers and never serve their own face.
        if not self.is_subagent:
            from slife.mcp.host_server import start_host_server
            try:
                # Every instance binds its own OS-assigned free port — there is
                # no fixed well-known address, so a second concurrent agent in
                # the same data dir never collides.  The bound port is returned,
                # logged, and published for consumers.
                server, task, _stop, host_port = start_host_server(
                    self.tool_registry,
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

        # Autonomous heartbeat — main agent only (period configurable via
        # agent.heartbeat_interval).  Subagents are workers and never
        # receive a heartbeat trigger.
        if not self.is_subagent:
            from slife.agent.heartbeat import heartbeat_loop

            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(heartbeat_loop(self))
                logger.info("heartbeat_started")

            # Scheduled-task trigger loop — same "main agent only" rule: a
            # subagent is a worker, never the scheduler.  Fires due tasks;
            # it never sweeps unfinished runs (see schedule_startup_sweep).
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
        ``__a2a_drain_incoming`` and feeds the unified inbox.  Replies are
        routed back through the plugin via ``__a2a_dispatch_result``.
        """
        import json as _json
        from slife.a2a.identity import AgentName, AgentMessage, Channel
        from slife.a2a.card import AgentCard, format_presence_line

        logger.info("a2a_poll_loop_start interval=%.1fs", interval)

        while True:
            try:
                client = self._plugins["a2a"].client
                if client is None:
                    break
                result = await client.call_tool("__a2a_drain_incoming", {})
                data = _json.loads(result)

                # A peer cancelled a task — drop it if still queued, or stop
                # the running loop if it is the message being processed
                # (Esc-equivalent).
                for cev in data.get("cancellations", []):
                    cid = cev.get("corr_id", "")
                    if cid:
                        self.inbox.cancel_correlation(cid)

                a2a_client = client  # narrowed MCPClient for the reply closure
                for ev in data.get("tasks", []):
                    async def _reply(
                        reply_text: str, cancelled: bool = False,
                        rt=ev.get("reply_to", ""),
                        cid=ev.get("correlation_id", ""),
                    ) -> None:
                        try:
                            assert a2a_client is not None
                            await a2a_client.call_tool("__a2a_dispatch_result", {
                                "reply_to": rt, "corr_id": cid, "text": reply_text,
                                "cancelled": cancelled,
                            })
                        except Exception:
                            pass

                    # The sender knows the task_id (a2a_send_task_async returns
                    # it); surface the same id to the receiver so it can
                    # reference the task it is responding to instead of making
                    # one up (a reported mismatch in round-trips).
                    task_text = ev.get("content", "")
                    corr_id = ev.get("correlation_id", "")
                    src = ev.get("source", "unknown")
                    if corr_id:
                        task_text = f"[Task {corr_id} from {src}] {task_text}"
                    msg = AgentMessage(
                        source=AgentName(src),
                        content=task_text,
                        reply_to=ev.get("reply_to", ""),
                        correlation_id=ev.get("correlation_id", ""),
                        on_reply=_reply,
                        channel=Channel.a2a(src),
                    )
                    await self.inbox.post(msg)
                    logger.debug(
                        "a2a_in source=%s task=%.80s",
                        msg.source, ev.get("content", ""),
                    )

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
                    await self._notify_a2a_activity(
                        "agent_change", event=pev.get("event", ""), card=card,
                    )

                # Outbound async-task results (auto-push) — the peer's result
                # arrived over MQTT; surface it so the agent doesn't need to
                # poll or block on subscribe.
                for cev in data.get("task_completions", []):
                    corr_id = cev.get("corr_id", "")
                    result = cev.get("result", "")
                    peer = cev.get("peer", "") or corr_id or "peer"
                    if not result:
                        continue
                    state = "cancelled" if cev.get("cancelled") else "completed"
                    await self.inbox.post(AgentMessage(
                        source=AgentName(peer),
                        content=(
                            f"Peer **{peer}** {state} async task "
                            f"(ID: `{corr_id}`):\n\n{result}"
                        ),
                        channel=Channel.a2a(peer),
                    ))
                    logger.debug("a2a_task_completed_autopushed peer=%s task=%s", peer, corr_id)
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

        # Set module-level transport reference so native subagent tools
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
            from slife.subagent.identity import SUBAGENT
            from slife.agent.schedules import (
                _SCHEDULE_WORKERS, _schedule_completion_content,
            )
            scheduled = agent_name in _SCHEDULE_WORKERS
            if scheduled:
                content = await _schedule_completion_content(self, agent_name)
            else:
                content = (
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
            key="status", value="ready",
            hint=f"Subagent manager ready (max_subagents={(self.config.subagent_config or {}).get('max_subagents', '?')}).",
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
        context footer exactly once.  If the buffer ever grows
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
        """Publish the open failed/missed scheduled runs (footer data).

        Written by the schedule loop and the startup sweep (schedules.py)
        on their cadence; read by the ``_sys_note`` footer each turn.
        Each item is ``{name, due_at, status}`` — failed and missed are
        the same question ("backfill or skip?") to the user, with
        ``status`` kept so the footer can show which one it was.
        """
        self._schedule_pending = runs

    def _schedule_pending_provider(self) -> list[dict]:
        return self._schedule_pending

    async def _notify_a2a_activity(self, kind: str, **kwargs) -> None:
        """Fire all registered A2A activity callbacks."""
        for cb in self._on_a2a_callbacks:
            try:
                await cb(kind, **kwargs)
            except Exception:
                pass

    def on_a2a_activity(self, callback) -> None:
        """Register a callback for A2A events (TUI notification).

        Callback signature: ``async def cb(kind: str, **kwargs)``
        where *kind* is ``"agent_change"``, ``"task_received"``, or
        ``"task_completed"``.
        """
        self._on_a2a_callbacks.append(callback)

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
