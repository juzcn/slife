"""job-coding plugin — deterministic, code-defined Jobs as MCP tools.

A job is a plain public function in ``<data_dir>/jobs/*.py``; each becomes
an MCP tool named after the function (schema from its signature/docstring).
Job tools are registered **dynamically** by the management tools:

  job-list      — list registered jobs
  job-create    — create a job from code (writes the file, registers now,
                  persists across restart)
  job-edit      — rewrite a job's code (file + re-register, rollback on error)
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
    "job-create", "job-edit", "job-remove", "job-list", "job-run", "__check",
})

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# ── Plugin state ──────────────────────────────────────────────────────

_jobs_dir: Path = get_jobs_dir()
_registry: dict[str, registry.Job] = {}
_llm_client = None            # LLMClient for job llm.chat (lazy)
_llm_model_ref = "?"          # diagnostic: model ref resolved at boot
_active_sessions: set = set()  # client sessions to notify on tool-set change


def _capture_session(ctx: Context | None) -> None:
    """Remember the caller's session for background notifications."""
    if ctx is not None and ctx.session is not None:
        _active_sessions.add(ctx.session)


async def _notify_tools_changed() -> None:
    """Push ``notifications/tools/list_changed`` to every known client.

    A listening harness re-syncs its tool registry.  Best-effort: a
    dead/stale session is dropped, the rest are served.
    """
    for sess in list(_active_sessions):
        try:
            await sess.send_tool_list_changed()
        except Exception:
            _active_sessions.discard(sess)


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
    from the ORIGINAL function.
    """
    try:
        mcp.add_tool(runner.wrap(job.fn, _get_llm_client()))
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
        "job-create, job-edit, job-remove, job-run. Jobs make one-shot "
        "LLM calls via the injected llm handle only."
    ),
    lifespan=_job_lifespan,
)


# ── Execution ─────────────────────────────────────────────────────────

async def _execute(job: registry.Job, kwargs: dict) -> str:
    """Run one job deterministically; returns the normalized result."""
    return await runner.wrap(job.fn, _get_llm_client())(**kwargs)


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
    """Write a job source file, scaffolding the llm import when missing."""
    text = str(code)
    if "job_coding import llm" not in text:
        text = "from slife.plugins.job_coding import llm\n\n" + text
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
        "List registered jobs: name, description, source file. Call this "
        "first to see which jobs are available before running one."
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
        "Run a registered job by name with a JSON object of its arguments. "
        "Load the job-coding skill for the conventions. Use when you know "
        "the job name; the per-job tools expose the same jobs with native "
        "schemas."
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
    name="job-create",
    description=(
        "Create a job from Python code. The code must define one public "
        "function (typed params + docstring) and may import the llm handle "
        "from slife.plugins.job_coding for one-shot calls (e.g. translate). "
        "The file is "
        "written to the jobs directory and the tool registered immediately; "
        "it persists across restarts. Load the job-coding skill first for "
        "the exact conventions and a template."
    ),
)
async def job_create(name: str, code: str, ctx: Context | None = None) -> str:
    """Create a job: writes <name>.py in the jobs directory and registers
    the tool now (persists across restart)."""
    _capture_session(ctx)
    err = _validate_name(name)
    if err:
        return err
    path = _jobs_dir / f"{name}.py"
    if path.exists():
        return f"Error: job '{name}' already exists (use job-edit)"
    _write_job_file(path, code)
    failure = _load_file(path)
    if failure:
        path.unlink(missing_ok=True)  # don't leave a broken file behind
        return failure
    if name not in _registry:
        path.unlink(missing_ok=True)
        return (
            f"Error: the code must define a public function named '{name}' "
            f"(received {sorted(_registry) or '(none)'})"
        )
    await _notify_tools_changed()
    return (
        f"Job '{name}' created and registered as the tool '{name}'. "
        f"Load the job-coding skill to author more."
    )


@mcp.tool(
    name="job-edit",
    description=(
        "Edit an existing job's code. Rewrites <name>.py and re-registers "
        "the tool (schema and behavior update live). On a broken edit the "
        "previous code is restored. Load the job-coding skill first."
    ),
)
async def job_edit(name: str, code: str, ctx: Context | None = None) -> str:
    """Replace a job's code, restoring the previous version on failure."""
    _capture_session(ctx)
    if name not in _registry:
        return f"Error: unknown job '{name}' (use job-create first)"
    path = _jobs_dir / f"{name}.py"
    if not path.exists():
        return f"Error: job file {path.name} missing"
    previous = path.read_text(encoding="utf-8")
    _unregister_tool(name)
    _write_job_file(path, code)
    failure = _load_file(path)
    if failure or name not in _registry:
        reason = failure or (
            f"the code must define a public function named '{name}' "
            f"(received {sorted(_registry) or '(none)'})"
        )
        # Roll back to the previous working code.
        _unregister_tool(name)
        _write_job_file(path, previous)
        failure2 = _load_file(path)
        if failure2:
            _unregister_tool(name)
        return f"Error: edit failed — previous code restored ({reason})"
    await _notify_tools_changed()
    return f"Job '{name}' updated and re-registered."


@mcp.tool(
    name="job-remove",
    description=(
        "Remove a job: deletes its source file and unregisters its tool. "
        "Load the job-coding skill first if unsure."
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
    await _notify_tools_changed()
    return f"Job '{name}' removed."


# ═══════════════════════════════════════════════════════════════════════
# Internal (harness) tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="__check",
    description=(
        "job-coding live facts: jobs dir, registered job count/names, model "
        "ref. Internal — probed by the harness's system_health, never "
        "exposed to the LLM."
    ),
)
async def __check() -> str:
    """Return raw job-coding facts for the harness health check."""
    result = {
        "jobs_dir": str(_jobs_dir),
        "jobs": len(_registry),
        "job_names": sorted(_registry),
        "llm_model": _llm_model_ref,
        "error": "",
    }
    try:
        client = _get_llm_client()
        model = getattr(client, "model_config", None) if client else None
        result["llm_model"] = (
            getattr(model, "ref", _llm_model_ref) if model else "unconfigured"
        )
    except Exception as e:
        result["error"] = str(e)
    return json.dumps(result, ensure_ascii=False, indent=2)


def main() -> None:
    run_plugin_server(mcp)


if __name__ == "__main__":
    main()