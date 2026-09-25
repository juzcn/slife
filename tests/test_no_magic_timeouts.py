"""Review gate: no hardcoded time values outside the central registry.

Scans ``slife/**/*.py`` and ``tests/**/*.py`` and fails on:
  * a numeric literal in any call's ``timeout=`` kwarg,
  * a numeric literal as the timeout argument of ``wait_for`` /
    ``asyncio.timeout`` (the cancel-on-timeout primitives),
  * a numeric literal as the delay of ``sleep`` (a cadence) — in ``slife/``,
    where a sleep is a duration someone chose; a test's sleep is a
    synchronisation primitive and is left alone,
  * a module-level (or class-level, or annotated) numeric constant assigned to
    a *TIME-STYLE name — budget names (TIMEOUT/DEADLINE/WAIT/STALL/GRACE/DELAY)
    and cadence names (INTERVAL/CADENCE/BACKOFF/LIFETIME/TTL/POLL/PERIOD/
    KEEPALIVE/HEARTBEAT/REFRESH) alike, with simple arithmetic folded so
    ``24 * 60`` counts as a literal,
  * ``deadline — X = <expr> + <numeric literal>`` assignments,
  * a numeric default on a timeout/deadline-named *function argument* (the
    def-time-default pattern: ``deadline_s: float = 1200.0``).

Anything that must stay literal is either covered by the allowlist below or
carries a ``# noqa-timeout`` comment on the same line (deliberate sync
subprocess probes, desktop notifications, test fixtures whose whole point is a
value small enough to observe).

Model rules behind this gate (see DESIGN.md §4.7):
  * values live in ``slife/timeouts.py`` — consumers read them at call time
    via ``slife.timeouts.timeouts.<role>.<key>``;
  * budgets (``work``/``ready``/``grace``/``transport``/``stream``/
    ``storage``/``deliver``) bound an await; cadences (``pacing``) set how
    often something runs.  Both are registry-owned — a cadence left as a
    module constant is a second seat for a value nobody can find;
  * the ONLY sanctioned "total" is the tool-call budget (work.tool_budget /
    work.task_budget); no other totals, no chain budgets, no turn deadlines;
  * ``slife/timeouts.py`` itself is the registry — exempt.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (ROOT / "slife", ROOT / "tests")
EXEMPT = {"slife/timeouts.py", "tests/test_no_magic_timeouts.py"}

#: relpath → symbols that intentionally stay local.  Empty by design: cadence
#: values are registry-owned too, so nothing needs an exemption.  An entry here
#: is a claim that the value is not a time budget OR a cadence — a count, a
#: profile shape — and must say so in its comment.
ALLOWLIST: dict[str, frozenset[str]] = {}

#: Name suffix tokens that mark a number as time-valued.  Budget words and
#: cadence words are both here: the exemption that used to let pacing names
#: live as module constants is gone (they are registry cadences now).
_NAME_BANNED = re.compile(
    r"(TIMEOUT|_DELAY|_STALL|_GRACE|_WAIT|_KEEPALIVE|_S$|DEADLINE"
    r"|INTERVAL|CADENCE|BACKOFF|LIFETIME|_TTL|_POLL|PERIOD|HEARTBEAT|REFRESH"
    r"|_AGE|MINUTE|_HOUR)"
)

#: Argument names that mark a numeric default as a time value (rule 5).
_ARG_BANNED = re.compile(
    r"(TIMEOUT|DEADLINE|DELAY|INTERVAL|CADENCE|BACKOFF|LIFETIME|_WAIT|GRACE"
    r"|STALL|KEEPALIVE|_S$|POLL|PERIOD|REFRESH|_AGE)"
)


def _source_line(path: Path, node: ast.AST) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""


def _has_noqa(line: str) -> bool:
    return "# noqa-timeout" in line


def _numeric(v: ast.expr | None) -> bool:
    return isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) \
        and not isinstance(v.value, bool)


def _fold(v: ast.expr | None) -> float | None:
    """Constant-fold simple arithmetic — ``24 * 60`` is as hardcoded as 1440."""
    if _numeric(v):
        return v.value  # type: ignore[union-attr]
    if isinstance(v, ast.BinOp) and isinstance(v.op, (ast.Mult, ast.Add, ast.Sub, ast.Div)):
        left, right = _fold(v.left), _fold(v.right)
        if left is None or right is None:
            return None
        try:
            return {ast.Mult: left * right, ast.Add: left + right,
                    ast.Sub: left - right, ast.Div: left / right}[type(v.op)]
        except ZeroDivisionError:
            return None
    return None


def _collect(path: Path):
    """Return the list of violations in one file (empty = clean)."""
    rel = path.relative_to(ROOT).as_posix()
    if rel in EXEMPT:
        return []
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    findings: list[str] = []
    allowed = ALLOWLIST.get(rel, frozenset())
    is_prod = rel.startswith("slife/")

    def note(node: ast.AST, msg: str) -> None:
        if not _has_noqa(_source_line(path, node)):
            findings.append(f"{rel}:{node.lineno} {msg}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # 1) a numeric literal on a time-style keyword argument of any call
            #    — ``timeout=`` is the common one, but ``stream_stall_timeout=``
            #    / ``interval=`` / ``keepalive=`` are the same fact.
            for kw in node.keywords:
                if not kw.arg or not _numeric(kw.value):
                    continue
                if _ARG_BANNED.search(kw.arg.upper()):
                    note(kw.value, f"{kw.arg}={kw.value.value!r} — use "
                                   "timeouts.<role>.<key> or # noqa-timeout")
            name = node.func.attr if isinstance(node.func, ast.Attribute) \
                else node.func.id if isinstance(node.func, ast.Name) else ""
            # 2) wait_for(..., N) / asyncio.timeout(N) / httpx2.Timeout(N)
            if name in ("timeout", "Timeout"):
                pos = node.args
            elif name == "wait_for":
                pos = node.args[1:]
            elif name == "sleep":
                # 3) sleep(N) — a bare cadence.  ``sleep(0)`` is a scheduling
                #    yield, not a duration, so it is not a time value at all.
                #    Production only: in a test a sleep is a synchronisation
                #    primitive (``sleep(3600)`` means "until cancelled") whose
                #    magnitude is the fixture, not a configured duration.  In
                #    ``slife/`` it is a cadence someone decided, and it belongs
                #    in the registry like every other one.
                pos = node.args if is_prod else ()
                if pos and _numeric(pos[0]) and pos[0].value == 0:
                    pos = ()
            else:
                pos = ()
            for a in pos:
                if _numeric(a):
                    note(a, f"{name}({a.value!r}) — use timeouts.<role>.<key>")
        elif isinstance(node, ast.Assign):
            # 4) numeric constant assigned to a time-style name
            for tgt in node.targets:
                tgt_name = tgt.id if isinstance(tgt, ast.Name) \
                    else tgt.attr if isinstance(tgt, ast.Attribute) else None
                if not tgt_name or tgt_name in allowed:
                    continue
                value = _fold(node.value)
                if value is not None and _NAME_BANNED.search(tgt_name):
                    note(node, f"{tgt_name} = {value!r} — time constant; must be "
                               "a call-time registry lookup")
            # 5) deadline-style: name = <expr> + literal
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                    and "deadline" in node.targets[0].id.lower() \
                    and isinstance(node.value, ast.BinOp) \
                    and isinstance(node.value.op, ast.Add) \
                    and _fold(node.value.right) is not None:
                note(node, f"{node.targets[0].id} + literal — resource deadline "
                           "must read timeouts.<role>.<key>")
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            # 6) annotated constant / dataclass field with a time-style name
            if isinstance(node.target, ast.Name) and node.target.id not in allowed:
                value = _fold(node.value)
                if value is not None and _NAME_BANNED.search(node.target.id):
                    note(node, f"{node.target.id}: ... = {value!r} — time constant; "
                               "must be a call-time registry lookup")
        # 7) numeric default on a timeout/deadline-named function argument —
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
                if default is None or _fold(default) is None:
                    continue
                if _ARG_BANNED.search(arg.arg.upper()):
                    # Anchor on the default's own line, so the ``# noqa-timeout``
                    # sits next to the literal even in a multi-line signature.
                    note(default, f"def {node.name}(... {arg.arg}={_fold(default)!r})"
                                  " — numeric time default; use None + call-time "
                                  "registry lookup")
    return findings


def _scan_all() -> list[str]:
    violations: list[str] = []
    for base in SCAN_DIRS:
        for path in sorted(base.rglob("*.py")):
            try:
                violations.extend(_collect(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
    return violations


def test_no_magic_timeouts():
    violations = _scan_all()
    assert not violations, (
        "hardcoded time values — route every value through slife/timeouts.py:\n"
        + "\n".join(violations)
    )


def _scan_source(src: str) -> bool:
    """Run the literal rules against a snippet (for the neg tests)."""
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "timeout" and _numeric(kw.value):
                    return True
            name = node.func.attr if isinstance(node.func, ast.Attribute) \
                else node.func.id if isinstance(node.func, ast.Name) else ""
            if name in ("timeout", "Timeout", "sleep") and node.args \
                    and _numeric(node.args[0]):
                return True
            if name == "wait_for" and len(node.args) > 1 and _numeric(node.args[1]):
                return True
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and _fold(node.value) is not None \
                and _NAME_BANNED.search(node.targets[0].id):
            return True
    return False


def test_planted_violation_is_caught():
    """Sanity check that the scanning logic actually fires on a literal."""
    assert _scan_source("async def f():\n    await asyncio.wait_for(g(), timeout=1.2)")
    assert _scan_source("async def f():\n    async with asyncio.timeout(9):\n        pass")
    assert _scan_source("c = httpx2.Timeout(30.0)")
    assert _scan_source("async def f():\n    await asyncio.sleep(5)")
    # cadence names and folded arithmetic are time values too
    assert _scan_source("_POLL_INTERVAL = 15.0")
    assert _scan_source("HEARTBEAT_INTERVAL = 1800")
    assert _scan_source("SESSION_MAX_AGE = 23 * 3600")
    assert _scan_source("MAX_WAIT_MINUTES = 24 * 60")
    assert not _scan_source("async def f():\n    await asyncio.wait_for(g(), timeout=T.timeouts.ready.spawn)")
    assert not _scan_source("_CONNECT_RETRY_ATTEMPTS = 20")  # a count, not a time
    assert not _scan_source("_STDERR_BUFFER_LIMIT = 500")    # a count, not a time


# ── Companion: every declared registry key is consumed ─────────────────


REGISTRY_KEYS = re.compile(
    # Keys may contain digits (``a2a_drain``), so the character class includes them.
    r"timeouts\.(work|ready|grace|transport|stream|storage|deliver|pacing)\.([a-z0-9_]+)"
)
_ROLE_NAMES = ("work", "ready", "grace", "transport", "stream", "storage",
               "deliver", "pacing")


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
    for base in SCAN_DIRS:
        for path in base.rglob("*.py"):
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
