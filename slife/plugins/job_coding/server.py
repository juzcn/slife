"""job-coding plugin — deterministic, code-defined Jobs as MCP tools.

A job is a plain public function in ``<data_dir>/jobs/*.py``; each becomes
an MCP tool named after the function (schema from its signature/docstring).
Job tools are registered **dynamically** by the management tools:

  job-list      — list registered jobs
  job-write     — write a job's code (create or replace; file + re-register,
                  broken writes roll back)
  job-remove    — delete a job (file + unregister)
  job-run       — generic executor by name (works before the harness resync
                  picks up a brand-new job tool)

Execution is deterministic: the tool calls the job function with exactly
its declared arguments; the only LLM access is the job's own explicit
``llm.chat(...)`` calls (single narrow one-shot chats on the
``job_coding.llm`` model from slife.json5).  No system prompt, no
conversation history, no agent loop.

After any tool-set mutation the plugin pushes the standard MCP
``notifications/tools/list_changed`` to connected clients (the mcp-plugin
pattern), so a harness re-syncs its registry without a restart.

Usage::
    uv run python -m slife.plugins.job_coding.server
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp.server.context import Context

from slife.plugins.job_coding import registry, runner
from slife.paths import get_jobs_dir
from slife.server_utils import create_plugin_server, run_plugin_server

#: Job names that would collide with this plugin's own tools.
_RESERVED_NAMES = frozenset({
    "job-write", "job-remove", "job-list", "job-run", "__check",
})

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# ── Plugin state ──────────────────────────────────────────────────────

_jobs_dir: Path = get_jobs_dir()
_registry: dict[str, registry.Job] = {}
_llm_client = None            # LLMClient for job llm.chat (lazy)
_llm_model_ref = "?"          # diagnostic: model ref resolved at boot
_active_sessions: set = set()  # client sessions to notify on tool-set change
#: Bound on tracked sessions — a stalled entry otherwise leaks forever and
#: widens the fan-out.  Past the bound a dead entry is reaped, a live one
#: re-registers on its next tool call.
_MAX_TRACKED_SESSIONS = 64
#: Per-session deadline for tools/list_changed notifications.
_NOTIFY_TIMEOUT = 5.0
#: Coalescing state: at most ONE send in flight; pushes that land while it
#: runs fold into a trailing-edge re-send.
_notify_pending = False
_notify_task: asyncio.Task | None = None


def _capture_session(ctx: Context | None) -> None:
    """Remember the caller's session for background notifications."""
    if ctx is not None and ctx.session is not None:
        _active_sessions.add(ctx.session)
        if len(_active_sessions) > _MAX_TRACKED_SESSIONS:
            _active_sessions.discard(next(iter(_active_sessions)))


def _request_tools_changed() -> None:
    """Coalesce-and-schedule ``notifications/tools/list_changed`` to all clients.

    Fire-and-forget, sent from a DETACHED task — never inside a request
    handler's task/scope, where mcp 2.1.1's dispatcher desyncs its
    cancel-scope stack under a notification burst and crashes the session.
    A listening harness re-syncs its tool registry on receipt.
    """
    global _notify_pending, _notify_task
    _notify_pending = True
    if _notify_task is not None and not _notify_task.done():
        return  # a send is scheduled/in flight — it re-checks the flag
    _notify_task = asyncio.create_task(_notify_daemon())


async def _notify_daemon() -> None:
    """Trailing edge: re-send after the in-flight round while pushes keep
    landing, so the freshest catalog always reaches every host."""
    global _notify_pending
    while _notify_pending:
        _notify_pending = False
        await _notify_send_all()


async def _notify_send_all() -> None:
    """One eager notification round to every known client, in this task.

    Best-effort: a dead/stale session is dropped, the rest are served.
    Sends run CONCURRENTLY, each bounded by :data:`_NOTIFY_TIMEOUT`.
    """
    sessions = list(_active_sessions)

    async def _send_one(sess) -> None:
        try:
            await asyncio.wait_for(
                sess.send_tool_list_changed(), timeout=_NOTIFY_TIMEOUT,
            )
        except Exception:
            _active_sessions.discard(sess)

    await asyncio.gather(*(_send_one(s) for s in sessions))


async def _notify_tools_changed() -> None:
    """Eager-flush alias kept for tests/…: run one full notification round
    now, in this task (deterministic delivery — no coalescing).  Production
    notification paths should use :func:`_request_tools_changed`."""
    await _notify_send_all()


def _get_llm_client():
    """Return the shared LLMClient for job ``llm.chat`` (lazy).

    May be None when the model could not be resolved — jobs that never
    call ``llm`` still work; ``llm.chat`` raises a clear runtime error.
    """
    global _llm_client, _llm_model_ref
    if _llm_client is not None:
        return _llm_client
    model = runner.resolve_job_model()
    if model is None:
        return None
    _llm_model_ref = getattr(model, "ref", "?")
    from slife.agent.llm_client import LLMClient
    _llm_client = LLMClient(model)
    return _llm_client


# ── Job registry <-> FastMCP tool table ───────────────────────────────

def _register_tool(job: registry.Job) -> None:
    """Register one job as a live MCP tool.

    ``runner.wrap`` preserves the job function's ``__name__``/docstring/
    annotations (via ``functools.wraps``), so FastMCP derives the schema
    from the ORIGINAL function.  The LLM client is passed as a lazy
    ``_LLMClientRef``: registration happens in the lifespan, which must
    stay handshake-fast — constructing the real ``LLMClient`` cold-imports
    the provider SDK (30s+ on a slow machine) and is deferred to the job's
    first ``llm.chat``.
    """
    try:
        mcp.add_tool(runner.wrap(job.fn, runner._LLMClientRef(_get_llm_client)))
    except Exception as e:
        logger.warning("job_tool_register_failed name=%s err=%s", job.name, e)
        return
    _registry[job.name] = job
    logger.info("job_tool_registered name=%s file=%s", job.name, job.path.name)


def _unregister_tool(name: str) -> None:
    """Remove a job's MCP tool (idempotent)."""
    if name in _registry:
        del _registry[name]
    try:
        mcp.local_provider.remove_tool(name)
    except KeyError:
        pass
    logger.info("job_tool_unregistered name=%s", name)


def _load_file(path: Path) -> str:
    """Import one job file and register its jobs.  Returns '' or an error."""
    try:
        module = registry.load_module(path)
        setattr(module, "_job_file_stem", path.stem)
        setattr(module, "_job_file_path", str(path.resolve()))
    except registry.JobLoadError as e:
        return f"Error: {e}"
    jobs = registry.collect_jobs(module)
    if not jobs:
        return (
            f"Error: {path.name} defines no public job functions "
            "(module-level function with a non-underscore name)"
        )
    for job in jobs:
        if job.name in _RESERVED_NAMES:
            return f"Error: job '{job.name}' collides with a reserved name"
        _register_tool(job)
    return ""


def _reload_all() -> str:
    """Re-scan the jobs directory, syncing the live tool table.

    Registers newly-appeared jobs and removes vanished ones (files edited
    externally).  Shared by lifespan startup and the removal path.
    """
    for name in list(_registry):
        if not (_jobs_dir / f"{name}.py").exists():
            _unregister_tool(name)
    for job in registry.scan_jobs_dir(_jobs_dir):
        if job.name in _registry:
            continue  # already live
        if job.name in _RESERVED_NAMES:
            continue
        _register_tool(job)
    return f"ok: {len(_registry)} jobs"


@asynccontextmanager
async def _job_lifespan(_app):
    """Resolve the job model and register the jobs from the jobs dir.

    All handshake-fast steps: register job tools from the jobs directory so
    the harness's first ``tools/list`` already shows them (and the restart
    contract — jobs re-register on every plugin start).  The ``job-coding``
    authoring skill lives in the standard skills directory (seed_skills).
    """
    # Diagnostics only: resolve the job model's REF (a cached Config read)
    # so the ready log / __check report the real model.  The LLMClient
    # itself stays lazy (built on a job's first llm.chat) — constructing
    # it in the lifespan cold-imports the provider SDK and blew the spawn
    # guard on slow machines.
    try:
        _model = runner.resolve_job_model()
        if _model is not None:
            global _llm_model_ref
            _llm_model_ref = getattr(_model, "ref", _llm_model_ref)
    except Exception:
        logger.debug("job_model_ref_resolve_failed", exc_info=True)
    _reload_all()
    logger.info(
        "job_coding_ready jobs_dir=%s jobs=%d llm_model=%s",
        _jobs_dir, len(_registry), _llm_model_ref,
    )
    try:
        yield
    finally:
        _registry.clear()


mcp, _log_path, logger = create_plugin_server(
    "slife-job-coding",
    instructions=(
        "job-coding — deterministic Jobs as MCP tools. Every job is a "
        "Python file in the jobs directory exposing one public function; "
        "job tools are registered dynamically. Management: job-list, "
        "job-write, job-remove, job-run. Jobs make one-shot "
        "LLM calls via the llm handle they import from "
        "slife.plugins.job_coding — only LLM jobs import it."
    ),
    lifespan=_job_lifespan,
)


# ── Execution ─────────────────────────────────────────────────────────

async def _execute(job: registry.Job, kwargs: dict) -> str:
    """Run one job deterministically; returns the normalized result."""
    # Lazy client ref — job-run must not cold-import the provider SDK.
    return await runner.wrap(
        job.fn, runner._LLMClientRef(_get_llm_client),
    )(**kwargs)


def _job_status() -> list[dict]:
    return [{
        "name": j.name,
        "description": j.description,
        "file": j.path.name,
    } for j in sorted(_registry.values(), key=lambda j: j.name)]


def _validate_name(name: str) -> str | None:
    """Validate a new job name (a Python identifier, not reserved)."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return (
            "Error: job name must be a Python identifier "
            "(letters/digits/underscore, not starting with a digit)"
        )
    if name.startswith("_") or name in _RESERVED_NAMES:
        return f"Error: '{name}' is a reserved job name"
    return None


def _write_job_file(path: Path, code: str) -> None:
    """Write a job source file verbatim (no scaffolding).

    The ``llm`` import is the author's responsibility: only LLM jobs need it,
    and the job-coding skill is the guide. Pure-computation jobs must stay
    clean.
    """
    text = str(code)
    if not text.endswith("\n"):
        text += "\n"
    _jobs_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
# Management tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="job-list",
    description=(
        "List registered jobs (name, description, source file)."
    ),
)
async def job_list(ctx: Context | None = None) -> str:
    """List all currently-registered jobs."""
    _capture_session(ctx)
    return json.dumps(
        {"jobs": _job_status(), "count": len(_registry)},
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="job-run",
    description=(
        "Run a registered job by name with a JSON object of its arguments."
    ),
)
async def job_run(job: str, params: str = "{}", ctx: Context | None = None) -> str:
    """Execute a job deterministically with the given JSON arguments."""
    _capture_session(ctx)
    entry = _registry.get(job)
    if entry is None:
        return (
            f"Error: unknown job '{job}'. Registered: "
            f"{', '.join(sorted(_registry)) or '(none)'}"
        )
    try:
        kwargs = json.loads(params) if params else {}
    except json.JSONDecodeError as e:
        return f"Error: params is not valid JSON: {e}"
    if not isinstance(kwargs, dict):
        return "Error: params must be a JSON object"
    return await _execute(entry, kwargs)


@mcp.tool(
    name="job-write",
    description=(
        "Write a job's code — create or replace; the job file becomes its own "
        "durable native tool (callable directly, persists across restarts)."
    ),
)
async def job_write(name: str, code: str, ctx: Context | None = None) -> str:
    """Write a job's code: creates <name>.py (or replaces it) and registers
    the tool now (persists across restart); a broken write rolls back."""
    _capture_session(ctx)
    err = _validate_name(name)
    if err:
        return err
    path = _jobs_dir / f"{name}.py"
    created = not path.exists()
    previous = path.read_text(encoding="utf-8") if not created else ""
    if not created:
        _unregister_tool(name)
    _write_job_file(path, code)
    failure = _load_file(path)
    if failure or name not in _registry:
        if not created:
            # Roll back to the previous working code.
            reason = failure or (
                f"the code must define a public function named '{name}' "
                f"(received {sorted(_registry) or '(none)'})"
            )
            _unregister_tool(name)
            _write_job_file(path, previous)
            failure2 = _load_file(path)
            if failure2:
                _unregister_tool(name)
            return f"Error: write failed — previous code restored ({reason})"
        # A new job that fails to load is never left as a broken file.
        path.unlink(missing_ok=True)
        return failure or (
            f"Error: the code must define a public function named '{name}' "
            f"(received {sorted(_registry) or '(none)'})"
        )
    _request_tools_changed()
    if created:
        return (
            f"Job '{name}' created and registered as the tool '{name}'. "
            f"Load the job-coding skill to author more."
        )
    return f"Job '{name}' updated and re-registered."


@mcp.tool(
    name="job-remove",
    description=(
        "Remove a job: delete its source file and unregister its tool."
    ),
)
async def job_remove(name: str, ctx: Context | None = None) -> str:
    """Delete a job file and unregister its tool."""
    _capture_session(ctx)
    if name not in _registry:
        return f"Error: unknown job '{name}'"
    path = _jobs_dir / f"{name}.py"
    if path.exists():
        path.unlink()
    _unregister_tool(name)
    _request_tools_changed()
    return f"Job '{name}' removed."


# ═══════════════════════════════════════════════════════════════════════
# Internal (harness) tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="__set_mcp_gateway_port",
    description=(
        "Point the jobs' mcp handle at the mcp-gateway plugin's port. "
        "Internal — called by the harness on gateway connect/reconnect to "
        "keep jobs' bare-MCP access current across gateway restarts, never "
        "exposed to the LLM."
    ),
)
async def __set_mcp_gateway_port(port: int, ctx: Context | None = None) -> str:
    """Record the mcp-gateway port for jobs' bare-MCP access (host push).

    Re-pointing the port invalidates the lazy ``mcp`` client cache — the
    next job ``mcp.call`` reconnects to the new endpoint.

    Args:
        port: The gateway's Streamable HTTP port (0/None clears).
    """
    _capture_session(ctx)
    await runner.mcp.set_port(port)
    return json.dumps(
        {"port": str(port), "source": runner.mcp.port_source},
        ensure_ascii=False,
    )


@mcp.tool(
    name="__check",
    description=(
        "job-coding live facts: jobs dir, registered job count/names, model "
        "ref, mcp gateway. Internal — probed by the harness's system_health, "
        "never exposed to the LLM."
    ),
)
async def __check() -> str:
    """Return raw job-coding facts for the harness health check.

    Never constructs the LLM client (a cold provider-SDK import) — the
    probe reports the resolved model ref and lazy-build state instead.
    """
    result = {
        "jobs_dir": str(_jobs_dir),
        "jobs": len(_registry),
        "job_names": sorted(_registry),
        "llm_model": _llm_model_ref,
        "llm_client": "ready" if _llm_client is not None else "lazy",
        "error": "",
    }
    try:
        result["mcp_gateway"] = {
            "port": runner.mcp.port,
            "source": runner.mcp.port_source,
            "connected": runner.mcp.connected,
        }
    except Exception as e:
        result["mcp_gateway"] = {"error": str(e)}
    return json.dumps(result, ensure_ascii=False, indent=2)


def main() -> None:
    run_plugin_server(mcp)


if __name__ == "__main__":
    main()