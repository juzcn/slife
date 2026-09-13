"""Review gate: no hardcoded timeout values outside the central registry.

Scans ``slife/**/*.py`` and fails on:
  * a numeric literal in any call's ``timeout=`` kwarg,
  * a numeric literal as the timeout argument of ``wait_for`` /
    ``asyncio.timeout`` (the cancel-on-timeout primitives),
  * a module-level float assigned to a *TIMEOUT-style name (the old
    scattered-constant pattern),
  * ``deadline — X = <expr> + <numeric literal>`` assignments,
  * a numeric default on a timeout/deadline-named *function argument* (the
    def-time-default pattern: ``deadline_s: float = 1200.0``).

Anything that must stay literal is either covered by the allowlist below or
carries a ``# noqa-timeout`` comment on the same line (deliberate sync
subprocess probes, desktop notifications, one-off dep bring-up).

Model rules behind this gate (see TIMEOUT.md):
  * values live in ``slife/timeouts.py`` — consumers read them at call time
    via ``slife.timeouts.timeouts.<role>.<key>``;
  * the ONLY sanctioned "total" is the tool-call budget (work.tool_budget /
    work.task_budget); no other totals, no chain budgets, no turn deadlines;
  * ``slife/timeouts.py`` itself is the registry — exempt.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "slife"
EXEMPT = "timeouts.py"

#: relpath → symbols that intentionally stay local because they are cadences /
#: counts / profile-shape values, not timeout budgets.
ALLOWLIST = {
    "agent/schedules.py": frozenset({"POLL_INTERVAL", "MISS_GRACE"}),              # cadence / downtime grace
    "agent/heartbeat.py": frozenset({"HEARTBEAT_INTERVAL"}),                       # cadence (also user-config)
    "plugins/mcp_gateway/connection.py": frozenset({"_HEALTH_CHECK_INTERVAL"}),    # cadence
    "plugins/wechat/server.py": frozenset({"_TYPING_MAX_LIFETIME"}),               # typing UX lifetime
    "tools/timer.py": frozenset({"MAX_WAIT_MINUTES"}),                             # product bound (24h)
}

#: Name suffix tokens that mark a float as a timeout-like budget.  Pacing
#: names (INTERVAL, CADENCE, BACKOFF, LIFETIME) are deliberately absent.
_NAME_BANNED = re.compile(r"(TIMEOUT|_DELAY|_STALL|_GRACE|_WAIT|_KEEPALIVE|_S$|DEADLINE)")


def _source_line(path: Path, node: ast.AST) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""


def _has_noqa(line: str) -> bool:
    return "# noqa-timeout" in line


def _collect(path: Path):
    """Return the list of violations in one file (empty = clean)."""
    rel = path.relative_to(ROOT).as_posix()
    if rel == EXEMPT:
        return []
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    findings: list[str] = []

    def numeric(v: ast.expr) -> bool:
        return isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) \
            and not isinstance(v.value, bool)

    def calc(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return repr(node.value)
        return None

    for node in ast.walk(tree):
        # 1) timeout=N kwarg on any call
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "timeout" and numeric(kw.value) and kw.value.lineno:
                    if _has_noqa(_source_line(path, kw.value)):
                        continue
                    findings.append(
                        f"{rel}:{node.lineno} timeout={kw.value.value!r} — use timeouts.<role>.<key> or # noqa-timeout"
                    )
            # 2) wait_for(..., N) / asyncio.timeout(N) / httpx2.Timeout(N)
            if isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("timeout", "Timeout"):
                pos = node.args
            elif isinstance(node.func, ast.Name) and node.func.id == "wait_for":
                pos = node.args[1:]
            else:
                pos = ()
            for a in pos:
                if numeric(a):
                    if _has_noqa(_source_line(path, a)):
                        continue
                    findings.append(
                        f"{rel}:{node.lineno} {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id}({a.value!r}) — use timeouts.<role>.<key>"
                    )
        # 3) module-level float assigned to a banned-style name
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if not isinstance(tgt, ast.Name):
                    continue
                allowed = ALLOWLIST.get(rel, set())
                if tgt.id in allowed:
                    continue
                if isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, float) \
                        and _NAME_BANNED.search(tgt.id):
                    if _has_noqa(_source_line(path, node)):
                        continue
                    findings.append(
                        f"{rel}:{node.lineno} {tgt.id} = {node.value.value!r} — float timeout constant; must be call-time registry lookup"
                    )
        # 4) deadline-style: name += N literal
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and "deadline" in node.targets[0].id.lower() \
                and isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Add) \
                and calc(node.value.right) is not None:
            if _has_noqa(_source_line(path, node)):
                continue
            findings.append(
                f"{rel}:{node.lineno} {node.targets[0].id} + literal — resource deadline must read timeouts.<role>.<key>"
            )
        # 5) numeric default on a timeout/deadline-named function argument —
        #    the def-time default pattern.  args.defaults align with the LAST
        #    positional args; kwonly defaults pair 1:1 with kwonlyargs.  The
        #    name match is case-insensitive so ``deadline_s`` is caught.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            pos_defaults = ([None] * (len(args.args) - len(args.defaults))
                            + list(args.defaults))
            arg_defs = list(zip(args.args, pos_defaults)) \
                + list(zip(args.kwonlyargs, args.kw_defaults))
            for arg, default in arg_defs:
                if default is None:
                    continue
                if numeric(default) and _NAME_BANNED.search(arg.arg.upper()):
                    if _has_noqa(_source_line(path, node)):
                        continue
                    findings.append(
                        f"{rel}:{node.lineno} def {node.name}(... {arg.arg}={default.value!r})"
                        f" — numeric timeout default; use None + call-time registry lookup"
                    )
    return findings


def test_no_magic_timeouts():
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            pass
        try:
            violations.extend(_collect(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
    assert not violations, (
        "hardcoded timeouts — route every value through slife/timeouts.py:\n"
        + "\n".join(violations)
    )


def _scan_source(src: str):
    """Run the two numeric-literal rules against a snippet (for the neg test)."""
    tree = ast.parse(src)

    def numeric(v: ast.expr) -> bool:
        return isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) \
            and not isinstance(v.value, bool)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "timeout" and numeric(kw.value):
                return True
        if isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("timeout", "Timeout") and node.args \
                and numeric(node.args[0]):
            return True
        if isinstance(node.func, ast.Name) and node.func.id == "wait_for" \
                and len(node.args) > 1 and numeric(node.args[1]):
            return True
    return False


def test_planted_violation_is_caught():
    """Sanity check that the scanning logic actually fires on a literal."""
    assert _scan_source("async def f():\n    await asyncio.wait_for(g(), timeout=1.2)")
    assert _scan_source("async def f():\n    async with asyncio.timeout(9):\n        pass")
    assert _scan_source("c = httpx2.Timeout(30.0)")
    assert not _scan_source("async def f():\n    await asyncio.wait_for(g(), timeout=T.timeouts.ready.spawn)")


# ── Companion: every declared registry key is consumed ─────────────────


REGISTRY_KEYS = re.compile(r"timeouts\.(work|ready|grace|transport|stream|storage|deliver)\.([a-z_]+)")
_ROLE_NAMES = ("work", "ready", "grace", "transport", "stream", "storage", "deliver")


def test_every_registry_key_is_consumed():
    """No dead keys: each declared registry key must be referenced by ≥1 site.

    Catches the "config key silently accumulates" failure mode (previously the
    a2a heartbeat/task_timeout keys parsed by nothing).  Matches the call-time
    lookup convention ``_timeouts.timeouts.<role>.<key>`` (which the substring
    ``timeouts.<role>.<key>`` also matches).
    """
    from dataclasses import fields
    from slife.timeouts import Timeouts

    mentioned: set[tuple[str, str]] = set()
    for path in ROOT.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        mentioned.update(REGISTRY_KEYS.findall(text))

    declared: set[tuple[str, str]] = set()
    for role, obj in Timeouts().__dict__.items():
        if role in _ROLE_NAMES:
            for f in fields(obj):
                declared.add((role, f.name))

    missing = declared - mentioned
    assert not missing, (
        "unreferenced timeout keys — consume or remove: "
        + ", ".join(f"{r}.{k}" for r, k in sorted(missing))
    )