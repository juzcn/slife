"""Job registry — load job modules from the jobs directory.

A job is a plain module-level **public** function in ``<data_dir>/jobs/*.py``
(standard MCP tool norm: the function's ``__name__``/docstring/annotations
become the tool's name/description/params).  ``_``-prefixed names are private
helpers and never become tools; imported callables are not tools either (only
functions *defined in* the job file).

Job files can ``from slife.plugins.job_coding import llm`` for one-shot
LLM calls.

Re-import is repeatable: the module is evicted from ``sys.modules`` before
each load, so editing a file and re-scanning picks up the new code.
"""

from __future__ import annotations

import inspect
import logging
import sys
import types
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Module-name namespace for job files — keeps them out of the real module
#: namespace (a job file named e.g. ``re.py`` must not shadow stdlib).
_MODULE_PREFIX = "_slife_job_"


class JobLoadError(Exception):
    """A job module could not be imported (compile/runtime error)."""


@dataclass(frozen=True)
class Job:
    """One registered job: a public function from a jobs-dir file."""

    name: str
    description: str
    fn: object        # the original job function (schema + logic)
    module: str       # source file stem (the module name without prefix)
    path: Path        # absolute source file path


def load_module(path: Path) -> types.ModuleType:
    """Import one job file; evicts any prior version first.

    Compiled via ``compile``+``exec`` (not a spec loader) so a rewritten
    file is ALWAYS re-read — a fast ``job-edit`` rewrite + reload at the
    same mtime would otherwise be served the stale bytecode cache.

    Raises:
        JobLoadError: Import failed (syntax/reference error) — surfaced to
            the caller as a deterministic tool result, never a crash.
    """
    stem = path.stem
    modname = f"{_MODULE_PREFIX}{stem}"
    sys.modules.pop(modname, None)
    module = types.ModuleType(modname)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[modname] = module
    try:
        source = path.read_text(encoding="utf-8")
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except Exception as e:
        sys.modules.pop(modname, None)
        raise JobLoadError(f"Importing {path.name} failed: {type(e).__name__}: {e}") from e
    return module


def collect_jobs(module: types.ModuleType) -> list[Job]:
    """Return the jobs defined in one loaded module.

    A job is any module-level public function whose ``__module__`` is the
    job file itself (imported callables are not jobs).  Private and
    dunder names are skipped; one function per job.
    """
    modname = module.__name__
    jobs: list[Job] = []
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if not inspect.isfunction(obj):
            continue
        if getattr(obj, "__module__", None) != modname:
            continue
        jobs.append(Job(
            name=name,
            description=inspect.getdoc(obj) or "",
            fn=obj,
            module=getattr(module, "_job_file_stem", ""),
            path=Path(getattr(module, "_job_file_path", "")),
        ))
    return jobs


def scan_jobs_dir(jobs_dir: Path) -> list[Job]:
    """Scan *jobs_dir* and return every job across all job files.

    A file that fails to import is logged and skipped (broken edits surface
    via ``job-edit``'s rollback), never fatal.
    """
    if not jobs_dir.is_dir():
        return []
    jobs: list[Job] = []
    for path in sorted(jobs_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = load_module(path)
            module._job_file_stem = path.stem  # type: ignore[attr-defined]
            module._job_file_path = str(path.resolve())  # type: ignore[attr-defined]
            jobs.extend(collect_jobs(module))
        except Exception as e:
            logger.warning("job_file_skip path=%s err=%s", path.name, e)
    return jobs