"""job-coding plugin — deterministic, code-defined Jobs as MCP tools.

A job is a plain public function in ``<data_dir>/jobs/*.py``; each becomes
an MCP tool named ``job-<function>`` (schema from its signature/docstring),
so it can never collide with another system tool.
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
``job_coding.llm`` model from slife.yaml).  No system prompt, no
conversation history, no agent loop.

After any tool-set mutation the plugin pushes the standard MCP
``notifications/tools/list_changed`` to connected clients (the mcp-gateway
pattern), so a harness re-syncs its registry without a restart.

Usage::
    uv run python -m slife.plugins.job_coding.server
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp.server.context import Context

from slife.plugins.job_coding import registry, runner
from slife.paths import get_jobs_dir
from slife.server_utils import (
    ToolsChangedNotifier,
    create_plugin_server,
    request_tools_changed,
    run_plugin_server,
    tools_changed_bus,
)

#: Tool names this plugin owns — a job may never take one of them.
_RESERVED_NAMES = frozenset({
    "job-write", "job-remove", "job-list", "job-run", "__check",
})

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _is_reserved(job_name: str) -> bool:
    """True when a job name collides with one of this plugin's own tools.

    The EXPOSED name is what collides: a job called ``write`` is exposed as
    ``job-write`` — the plugin's own management tool — so the prefixed name is
    tested too.  ``job-write`` itself stays rejected as well: it would be
    exposed as ``job-job-write``, which helps nobody.
    """
    return job_name in _RESERVED_NAMES or registry.tool_name(job_name) in _RESERVED_NAMES

# ── Plugin state ──────────────────────────────────────────────────────

_jobs_dir: Path = get_jobs_dir()
_registry: dict[str, registry.Job] = {}
_llm_client = None            # LLMClient for job llm.chat (lazy)
_llm_model_ref = "?"          # diagnostic: model ref resolved at boot
#: Fan-out of ``tools/list_changed`` to this server's listen subscribers
#: (:class:`slife.server_utils.ToolsChangedNotifier`) — a job write/delete
#: changes the exposed tool set, and the modern era delivers that to the
#: streams a client opened with ``subscriptions/listen``.
#: Bound to the server below (``create_plugin_server`` runs after this).
_notifier = ToolsChangedNotifier()


def _request_tools_changed() -> None:
    """Publish ``tools/list_changed`` to the listen subscribers.

    Fire-and-forget — see :func:`slife.server_utils.request_tools_changed`.
    A listening harness re-syncs its tool registry on receipt.
    """
    request_tools_changed(_notifier)


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
    from the ORIGINAL function; the tool NAME is the prefixed one
    (``job-translate``), which is what the LLM sees and calls.  The LLM client
    is passed as a lazy ``_LLMClientRef``: registration happens in the
    lifespan, which must stay connect-fast — constructing the real
    ``LLMClient`` cold-imports the provider SDK (30s+ on a slow machine) and is
    deferred to the job's first ``llm.chat``.
    """
    try:
        wrapped = runner.wrap(job.fn, runner._LLMClientRef(_get_llm_client))
        # FastMCP takes the tool name from ``fn.__name__`` (its ``add_tool`` has
        # no ``name=``); ``functools.wraps`` inside ``runner.wrap`` is what put
        # the job's bare name there, and the signature/docstring the schema is
        # derived from ride on ``__wrapped__``, so renaming here renames the
        # tool without touching its schema.
        wrapped.__name__ = registry.tool_name(job.name)
        mcp.add_tool(wrapped)
    except Exception as e:
        logger.warning("job_tool_register_failed name=%s err=%s", job.name, e)
        return
    _registry[job.name] = job
    logger.info("job_tool_registered name=%s file=%s", job.name, job.path.name)


def _unregister_tool(name: str) -> None:
    """Remove a job's MCP tool (idempotent).

    Accepts the bare job name (how the registry keys jobs) or the exposed tool
    name — callers iterate ``_registry``, so both spellings arrive here.
    """
    bare = registry.bare_name(name)
    _registry.pop(bare, None)
    try:
        mcp.local_provider.remove_tool(registry.tool_name(bare))
    except KeyError:
        pass
    logger.info("job_tool_unregistered name=%s", bare)


def _load_file(path: Path) -> str:
    """Import one job file and register its jobs.  Returns '' or an error."""
    try:
        module = registry.load_module(path)
        setattr(module, "_job_file_path", str(path.resolve()))
    except registry.JobLoadError as e:
        return f"Error: {e}"
    jobs = registry.collect_jobs(module)
    if not jobs:
        return (
            f"Error: {path.name} defines no public job functions "
            "(module-level function with a non-underscore name)"
        )
    # A reserved-name collision is a malformed file — reject the WHOLE file
    # atomically, before anything is touched: no job from it registers, and
    # nothing already registered is unregistered.  (Matches the whole-file
    # skip _reload_all applies at startup, so a restart and a live edit
    # always agree for the same file.)
    invalid = next((j.name for j in jobs if _is_reserved(j.name)), None)
    if invalid is not None:
        return f"Error: job '{invalid}' collides with a reserved name"
    # A file can define MANY public functions; functions removed from it
    # must not linger as ghost tools (the old function object stays callable
    # forever otherwise).  Unregister this file's previously-registered jobs
    # that the new content no longer defines — the job_write rollback path
    # re-loads the previous content and re-registers them, so a failed write
    # still restores exactly.
    resolved = str(path.resolve())
    current_names = {j.name for j in jobs}
    for name in list(_registry):
        if name in current_names:
            continue
        if str(_registry[name].path.resolve()) == resolved:
            _unregister_tool(name)
    for job in jobs:
        _register_tool(job)
    return ""


def _reload_all() -> str:
    """Re-scan the jobs directory, syncing the live tool table.

    Registers newly-appeared jobs and removes vanished ones (files edited
    externally).  Shared by lifespan startup and the removal path.
    """
    if not _jobs_dir.is_dir():
        return f"ok: {len(_registry)} jobs"
    # A job is tracked by its SOURCE file, not by its name — one file can
    # define many jobs (job.name != file stem), so a vanished file is
    # detected against ``_registry[name].path``, never a ``{name}.py`` glob.
    live_files = {
        str(p.resolve()) for p in _jobs_dir.glob("*.py")
        if not p.name.startswith("_")
    }
    for name in list(_registry):
        if str(_registry[name].path.resolve()) not in live_files:
            _unregister_tool(name)
    # A file whose content collides with a reserved name is rejected WHOLE by
    # _load_file — the reload must agree, so a restart and a live edit never
    # diverge for the same file.
    jobs = registry.scan_jobs_dir(_jobs_dir)
    reserved_files = {
        str(j.path.resolve()) for j in jobs if _is_reserved(j.name)
    }
    for job in jobs:
        if str(job.path.resolve()) in reserved_files or job.name in _registry:
            continue  # whole-file skip / already live
        _register_tool(job)
    return f"ok: {len(_registry)} jobs"


@asynccontextmanager
async def _job_lifespan(_app):
    """Resolve the job model and register the jobs from the jobs dir.

    All connect-fast steps: register job tools from the jobs directory so
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
# The server exists only now — bind the notifier to its subscription bus so
# a job write/delete reaches the listen subscribers (`tools_changed_bus` also
# registers the listen handler, which fastmcp does not).
_notifier.bind(tools_changed_bus(mcp))


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
        "tool": registry.tool_name(j.name),
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
    if name.startswith("_") or _is_reserved(name):
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
    # Also accept the exposed tool name (``job-translate``): the LLM reads the
    # job's schema as that name, and job-list reports it, so both spellings
    # must resolve.
    entry = _registry.get(registry.bare_name(job))
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
        "Write a job's code — create or replace; a broken write is rolled back."
    ),
)
async def job_write(name: str, code: str, ctx: Context | None = None) -> str:
    """Write a job's code: creates <name>.py (or replaces it) and registers
    the tool now (persists across restart); a broken write rolls back."""
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
        }
    except Exception as e:
        result["mcp_gateway"] = {"error": str(e)}
    return json.dumps(result, ensure_ascii=False, indent=2)


def main() -> None:
    run_plugin_server(mcp)


if __name__ == "__main__":
    main()