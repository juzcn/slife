"""The headless agent — a complete agent with no terminal attached.

``slife --headless --agent jack`` runs the *same* agent the TUI runs: the same
``Role.MAIN`` capability table, so the same turn persistence, session restore,
plugin spawning, heartbeat, schedules and A2A mesh inbound.  What it does not
have is an operator.  The keyboard is gone and so is every surface the TUI
draws on; traffic arrives through the ordinary inbox channels — an A2A peer, a
worker's completion, a heartbeat, a schedule — and the process runs until
Ctrl+C.

The absence *is* the implementation, and that is the point.  No handler
factory is installed, so a turn takes the loop's existing ``handler=None``
path: silent, and auto-approving, because a call asking for confirmation has
nobody to ask.  The system prompt states that fact (``slife.j2``'s
``no_operator``) so the model does not ask in the first place; whether a tool
needs a human's consent is a *policy*, and this process has none to add to the
model's own judgment.

Registering the TUI's callbacks here would be worse than useless: they exist
to draw.  The one exception is ``on_memory_broken`` — the inbox is frozen when
it fires, so a host that ignored it would sit frozen forever with nothing
said.

Nothing is printed, also deliberately: the terminal is not a surface here.
The session log and the turns DB are the record, and the agent's real output
leaves over the mesh.  Startup warnings still reach stderr, because a process
that dies — or runs deaf — in silence is a bug report nobody can read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING

import slife.timeouts as _timeouts  # module ref — call-time lookup, patch-safe
from slife.agent.plugins import PluginStartStatus
from slife.agent.roles import Role
from slife.agent.service import AgentService, MemoryDatabaseError
from slife.agent.session import restore_context, shutdown_session
from slife.ui.i18n import t

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)


def run_headless(config: Config) -> tuple[AgentService | None, str]:
    """Run the agent with no terminal until Ctrl+C.

    Returns ``(service, fatal)``: the service, so the caller's teardown can
    still hard-kill whatever survived (the graceful stop already ran inside),
    and a message when startup aborted — ``""`` on an ordinary Ctrl+C exit.
    """
    service: AgentService | None = None

    async def _serve() -> str:
        nonlocal service
        # The process fact the system prompt renders from, set before the
        # service builds it.  It is a fact about the HOST, not the role: the
        # same agent, the same capability table, and no operator attached.
        os.environ["SLIFE_HEADLESS"] = "1"
        service = AgentService(config, role=Role.MAIN)

        # The one callback that is not a surface.  A turn that cannot be
        # saved is a hard stop, and ``save_to_memory`` has already frozen the
        # inbox by the time this fires: the TUI answers with a banner and
        # stays open for the reader, but a headless process has no reader, so
        # it says why on stderr and stops rather than sitting there frozen —
        # alive, deaf, and looking healthy to whatever started it.
        stopped = asyncio.Event()
        broken: list[str] = []

        def _memory_broken(err: str) -> None:
            broken.append(t("memory_broken", err=err))
            logger.error("headless_memory_broken err=%s", err)
            stopped.set()

        try:
            fatal = await _boot(service, config)
            if fatal:
                return fatal
            service.on_memory_broken(_memory_broken)
            logger.info("headless_ready agent=%s", config.agent_name)
            # Parked.  Every input is a channel, and every channel is a task
            # the service already started; there is nothing to poll and
            # nothing to read — waiting IS the idle loop.
            await stopped.wait()
            return broken[0] if broken else ""
        finally:
            # Ctrl+C cancels this task; the teardown runs anyway, because a
            # process that leaves plugin children and a live MQTT session
            # behind is worse than one that takes a moment to stop.  The
            # inbox is cancelled first so an in-flight turn stops calling
            # tools into the MCP clients we are about to disconnect.
            service.inbox.cancel()
            await shutdown_session(service)

    try:
        return service, asyncio.run(_serve())
    except KeyboardInterrupt:
        # Ctrl+C — the normal way out.
        return service, ""


async def _boot(service: AgentService, config: Config) -> str:
    """Start everything the TUI starts, in the same order.  ``""`` if healthy.

    A returned message aborts startup: only the two cases the TUI also treats
    as fatal — an unusable memory database and a missing *required* plugin —
    produce one.  Everything else degrades, and says so.
    """
    await service.start_inbox()

    # ── Session restore ───────────────────────────────────────────────
    # The exit-time context, restored exactly as the TUI restores it: an A2A
    # task may span many turns, and a restart that forgot the conversation
    # would make the agent a stranger to a peer mid-task.
    try:
        turns = await service.get_exit_context_turns()
    except MemoryDatabaseError as e:
        # Memory is core — a session that cannot read its own history is not
        # a session.  Same call as the TUI's.
        return t("memdb_unavailable", err=e)
    except Exception:
        logger.debug("session_restore_skip", exc_info=True)
    else:
        if turns:
            try:
                restore_context(service, turns, service.message_history, "")
            except Exception:
                # Already logged where it happened; the agent runs on what it
                # has, as the TUI does.
                logger.warning("headless_restore_failed turns=%d", len(turns))

    # ── Plugins ───────────────────────────────────────────────────────
    from slife.plugins import discover_plugins

    plugins = discover_plugins()

    # A plugin declared required but not discovered (typo, missing package)
    # violates the contract — abort before spawning anything rather than
    # running without a core component.
    missing = config.plugins_required - {name for name, _ in plugins}
    if missing:
        return t(
            "required_failed",
            name=next(iter(missing)),
            reason="plugin not discovered",
        )

    statuses: dict[str, PluginStartStatus] = {}
    raised: dict[str, str] = {}

    async def _start(name: str, module: str) -> None:
        try:
            statuses[name] = await service.start_plugin_server(name, module)
        except Exception as e:
            # The spawn hang-guard surfaces as a bare ``TimeoutError`` whose
            # ``str()`` is empty, and every plugin can hit it — without a
            # fallback the reason line says nothing at all about why, which is
            # exactly the case that needs a reason.
            raised[name] = str(e) or (
                f"timed out after {_timeouts.timeouts.ready.plugin_start:.0f}s"
            )

    # Concurrent, as the TUI's per-plugin workers are: the spawns are
    # independent and the slowest one sets the startup time either way.
    await asyncio.gather(*(_start(name, module) for name, module in plugins))

    for name in [n for n, _ in plugins]:
        if name in raised:
            if not _warn(name, "start failed", raised[name], config):
                return t("required_failed", name=name, reason=raised[name])
        elif statuses.get(name) is PluginStartStatus.STARTED:
            logger.debug("plugin_ready name=%s", name)
        elif statuses.get(name) is PluginStartStatus.SKIPPED:
            # An expected no-op — not configured, or a dependency is absent.
            # Never an error, even for a required plugin (the TUI's rule).
            logger.debug("plugin_skipped name=%s", name)
        elif not _warn(name, "never became ready", "", config):
            return t(
                "required_failed", name=name, reason="plugin never became ready",
            )

    await service.start_subagent()
    await service.wait_startup_settled()

    _warn_if_mesh_is_down(config, statuses)
    return ""


def _warn(name: str, what: str, reason: str, config: Config) -> bool:
    """Report a plugin that did not start; ``False`` if that is fatal.

    A required plugin's failure aborts startup; every other one is a warning,
    because the missing service is surfaced where it is used.
    """
    if name in config.plugins_required:
        logger.error("plugin_%s name=%s err=%s", what.replace(" ", "_"), name, reason)
        return False
    logger.warning("plugin_%s name=%s err=%s", what.replace(" ", "_"), name, reason)
    return True


def _warn_if_mesh_is_down(
    config: Config,
    statuses: dict[str, PluginStartStatus],
) -> None:
    """Say so when the mesh did not come up — it is this process's only input.

    Not fatal: a broker that is not up yet, or a name another process is
    holding, is recoverable, and the plugin watchdog keeps trying.  But
    saying nothing would leave an operator watching a process that looks
    healthy and can never be spoken to.
    """
    if statuses.get("a2a") is PluginStartStatus.STARTED:
        return

    a2a = config.a2a_config
    if a2a is None:
        why = "no `a2a:` section in the config"
    elif not a2a.enabled:
        why = f"the broker {a2a.broker_host}:{a2a.broker_port} did not answer"
    else:
        why = (
            f"the name {config.agent_name!r} may be held by another process, "
            f"or the plugin failed to start"
        )

    msg = (
        f"the A2A mesh is not connected ({why}), so this agent has no peers "
        f"to hear from. It keeps running: heartbeat and schedules still fire."
    )
    logger.warning("headless_no_mesh %s", msg)
    print(f"Warning: {msg}", file=sys.stderr)
