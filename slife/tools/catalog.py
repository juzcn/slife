"""Unified tool catalog store — the single shared ``tools.db``.

The authoritative catalog for all six tool categories
(builtin/job/mcp/rest-api/skill/cli).  Lives on disk (data dir, WAL) and is
opened by every agent process (main agent + subagent workers) at the same
path; SQLite gives concurrent readers + one writer, ``busy_timeout`` backs off
the short load/unload writes.  Ops are deliberately store-shaped (upserts,
search, LRU, embed drainer contract) — policy lives in
:mod:`slife.tools.catalog_service`.

Which row IS what is ``category``, and nothing else: there is no ``type``
column projecting it.  A derived column is a second thing to write and keep in
sync on every insert, update and migration, and every question it answered
("does this row have a load state?") is a membership test over
:data:`FUNCTION_CATEGORIES`.

State model: two columns, two questions, and everything the injection gate
needs is ON THE ROW.

``tool.status`` is the row's state — ``enabled | disabled | error``, three
EXCLUSIVE values: a row is in exactly one of them, so an ``error`` row is not
also an enabled one and there is nothing for the config switch to do about it.
``disabled`` is CONFIG (the ``tools.yaml`` switch), ``error`` is the RUNTIME
verdict (the owner is unusable right now — many causes: a server that never
came up, a disconnect, a failed connect, a dead gateway child, a skill file
that cannot be read, …), and ``enabled`` is everything else.  The code that
reports an ``error`` states the state and never the cause: nothing here has
checked one.

Two writers move the value, each owning its own transition — never a
coexistence, just one value being replaced by another: config writes
``disabled ↔ enabled`` and the runtime writes ``enabled → error`` /
``error → enabled``, and every writer's ``WHERE`` is the guard that keeps it
in its lane (see ``mark_source_error`` / ``set_source_enabled`` /
``mark_source_connected``).  So a server switched off while it was down is
``disabled`` (off is not down — and it can no longer be seen as ``error``,
because the column holds one value), and a fixed owner returns to
``enabled``.

``tool.load_status`` is what the MODEL decided — ``loaded | unloaded | n/a``
(``n/a`` for skill/cli, which have no load concept).  It is the one thing the
db exists to persist, and no connectivity verdict is ever written into it: a
blip must not cost the session its loaded set.

There is deliberately no ``server`` table: which servers to bring up lives in
``tools.yaml``, what is live right now lives in the gateway's pool, and this
db only records the result on the tool rows.  Writes: load-status flips
(load/unload) and the status marks; eviction is the main agent's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from slife.plugins.memdb.store import (
    _clamp_limit,
    _contains_cjk,
    _like_terms,
    _serialize_f32,
    _split_sql,
    _to_fts5_query,
    in_placeholders,
)
from slife.timeutil import now_local_seconds
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)

#: Server-backed categories — a tool whose ``source_id`` names an external
#: server.  There is no ``server`` row to join: the tool's OWN ``status``
#: column carries the connectivity verdict (``error`` when its server is down).
#:
#: ``plugin`` is deliberately NOT here even though those rows also carry a
#: ``source_id`` (the plugin that owns them): a plugin is not an *external*
#: server, so the gateway's death must not mark its tools unusable
#: (``mark_all_external_error``), its rows keep a config-mirrored ``status``,
#: and it stays out of the "servers" count and the unconfigured-source purge.
SERVER_CATEGORIES = frozenset({"mcp", "rest-api"})

#: Function-tool categories — the only ones with a load state: a builtin
#: module tool, a job file's function, a built-in plugin's own tool, and an
#: external server's tool.  ``skill`` / ``cli`` are the other two categories
#: and have none.
#:
#: This set IS the derivation that replaced the ``type`` column: the load/unload
#: rules, the injection predicate and every "can this row be loaded?" refusal
#: ask it, so there is no second column for a writer to keep in sync.
FUNCTION_CATEGORIES = frozenset({"builtin", "job", "plugin", "mcp", "rest-api"})

#: SQL ``IN`` list of :data:`FUNCTION_CATEGORIES` — built once from the
#: constant (sorted, so the string is stable) rather than spelled out again in
#: each statement, which is how the SQL could drift from the set.
_FUNC_CATEGORY_SQL = "category IN ({})".format(
    ",".join(f"'{c}'" for c in sorted(FUNCTION_CATEGORIES)),
)


def is_function_category(category: str) -> bool:
    """Whether a ``category`` names a function tool — the load-state question."""
    return category in FUNCTION_CATEGORIES


# ``tool.status`` — the row's whole closed domain, and the three states a tool
# can actually be in.  They are MUTUALLY EXCLUSIVE: one value per row, so
# ``error`` never sits beside ``enabled`` (a row in ``error`` is not an enabled
# row) and a ``disabled`` row is never ``error``.
#
# ``disabled`` is the CONFIG's answer (``tools.yaml`` switched this tool, or
# its server, off); ``error`` is the RUNTIME verdict (its owner is not usable
# right now); ``enabled`` is everything else.
#
# Two writers, each owning one transition of that value:
# ``set_source_enabled`` moves ``disabled ↔ enabled``, ``mark_source_error``
# moves ``enabled → error``, ``mark_source_connected`` moves ``error →
# enabled``.  The WHERE clause of each is that ownership, which is why a server
# switched off while it was down stays ``disabled`` (off is not down) and a
# server that comes back does not resurrect a tool the config switched off.
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUS_ERROR = "error"
#: The stored load-status values — the OTHER column's closed domain.  ONLY
#: the load state lives here — the model's decision, and the one thing the db
#: exists to persist (``loaded`` / ``unloaded``).  The connectivity verdict is
#: a separate column (``status``): writing it into ``load_status`` destroyed
#: the load state it landed on, so a server blip — or the startup sweep —
#: reset every external tool the model had loaded.
STATUS_LOADED = "loaded"
STATUS_UNLOADED = "unloaded"

#: The stored spelling of "no load state applies" — the SAME literal the model
#: sees nowhere else, because the effective status of a skill/cli row is its
#: own ``status`` (``enabled``), not a 'n/a' the reader can do nothing with.
#: One literal for every column that would otherwise be NULL: no load state,
#: no owner, no schema text.
NA = "n/a"
STATUS_NA = NA

#: Schema revision this store expects (``catalog_schema.sql`` sets it; the
#: migration in :meth:`CatalogStore._migrate` moves an older file up to it).
#: Deliberately NOT bumped for the ``status`` column (v8 — the ``enabled`` /
#: ``unavailable`` pair collapsed into one three-state column) or for dropping
#: the derived ``type`` column (v9), and v5 (``status`` → ``load_status``) and
#: v6 (the two flag columns become ``NOT NULL``) have no step either: the file
#: is derived data, so a stale one is reported and rebuilt
#: (``_check_columns``), never upgraded in place.
SCHEMA_VERSION = 9

#: The category values the code can write — the set the live table's ``CHECK``
#: must accept (checked at open, see :meth:`CatalogStore._check_categories`).
ALL_CATEGORIES = FUNCTION_CATEGORIES | {"skill", "cli"}

#: Local ISO-seconds timestamp — the shared store convention.
_now = now_local_seconds


#: The columns the code reads or writes on ``tool`` — checked at open against
#: the live table (:meth:`CatalogStore._check_columns`), in BOTH directions:
#: a missing column cannot answer a query, and an unknown one is a leftover
#: from a schema this code no longer writes (the derived ``type`` column, for
#: one), which would otherwise sit there unmaintained forever.
_REQUIRED_COLUMNS = frozenset({
    "name", "description", "category", "source_id", "schema",
    "status", "load_status", "last_loaded",
})

#: The ``tool`` table's category constraint, parsed rather than substring-matched.
_CATEGORY_CHECK_RE = re.compile(
    r"check\s*\(\s*category\s+in\s*\(([^)]*)\)", re.IGNORECASE,
)


def _category_check_values(ddl: str) -> "set[str] | None":
    """The category values a ``tool`` DDL accepts; ``None`` when it has no CHECK.

    Scoped to the ``category`` clause rather than the whole statement on
    purpose: ``'skill'`` and ``'cli'`` are ordinary words that another clause
    could carry (the retired ``type`` CHECK did exactly that), so a substring
    test would report a category list missing them as complete.
    """
    m = _CATEGORY_CHECK_RE.search(ddl or "")
    if m is None:
        return None
    return {v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()}


def _config_status_move(incoming: str) -> tuple[str, tuple[str, ...]]:
    """``(the value config writes, the current values it may overwrite)``.

    The ONE statement of what the config's arm may do to ``status`` — both
    writers (:meth:`CatalogStore.set_source_enabled` and ``reconcile``'s
    in-memory comparison) build their write from it, so the rule cannot drift
    between the bulk path and the per-row one.

    Config speaks two of the three states, and only one of them is an
    assertion about liveness:

    - ``disabled`` is a DECISION — the user took this tool off — so it lands
      on whatever the row said, ``error`` included: off is not down, and the
      model must be able to tell the two apart.
    - ``enabled`` only un-switches.  It clears ``disabled`` and leaves an
      ``error`` row where it is, because "the owner is up" is the runtime's
      verdict to give (``mark_source_connected``), never a config projection's
      to assert.
    """
    if incoming == STATUS_DISABLED:
        return STATUS_DISABLED, (STATUS_ENABLED, STATUS_ERROR)
    return STATUS_ENABLED, (STATUS_DISABLED,)


def config_status(enabled: "bool | None") -> "str | None":
    """The ``status`` a config switch value stands for.

    Config speaks booleans (``enabled: false`` in ``tools.yaml``) and the
    column speaks states; this is the ONE conversion, so the two vocabularies
    meet in exactly one place.  ``None`` = no opinion — the caller has nothing
    to say about this row, which is not the same as saying "enabled".
    """
    if enabled is None:
        return None
    return STATUS_ENABLED if enabled else STATUS_DISABLED


def _embeddable(schema: str | None) -> bool:
    """Whether a row's ``schema`` carries text the drainer can embed.

    The Python face of ``CatalogStore._EMBEDDABLE_SCHEMA``, and it MUST agree
    with it: the reconcile wakes the drainer for exactly the rows this says yes
    to, and the drainer embeds exactly the rows the SQL says yes to.  Two
    predicates that disagreed (the old insert arm asked whether the FLATTENED
    text was non-empty, the SQL asks whether the column is off its sentinel)
    leave a row counted as unembedded that no pass ever hands over — the
    drainer burns its no-progress bound and gives up with the semantic gate
    shut.  "No schema text" is the sentinel, never a schema that merely
    flattens to nothing.
    """
    return (schema or NA).strip() not in ("", NA)


def _deserialize_f32(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance (1 - cosine similarity) between two vectors."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 1.0
    return max(0.0, min(2.0, 1.0 - dot / math.sqrt(na * nb)))


# ── Schema → text (semantic doc source) ─────────────────────────────

def _fold_children(spec: dict) -> str:
    """One level of nested parameter text (object properties / array items)."""
    schemas = None
    if spec.get("type") == "object":
        schemas = spec.get("properties")
    elif spec.get("type") == "array":
        items = spec.get("items")
        schemas = items.get("properties") if isinstance(items, dict) else None
    if not isinstance(schemas, dict):
        return ""
    parts = []
    for cname, cprop in schemas.items():
        if not isinstance(cprop, dict):
            continue
        ctype = cprop.get("type", "")
        cdesc = str(cprop.get("description", "")).strip()
        head = f"{cname} ({ctype})" if ctype else cname
        parts.append(f"{head}: {cdesc}" if cdesc else head)
    return "; ".join(parts)


def _param_line(name: str, spec: dict, required: bool) -> str:
    """One parameter as readable text: ``name (type, required): desc``."""
    ptype = str(spec.get("type", ""))
    desc = str(spec.get("description", "")).strip()
    children = _fold_children(spec)
    if children:
        desc = f"{desc} {children}".strip() if desc else children
    head = name
    if ptype:
        head += f" ({ptype}{', required' if required else ''})"
    elif required:
        head += " (required)"
    return f"{head}: {desc}" if desc else head


def _flatten_schema(schema_text: str) -> str:
    """Single flat readable text of a tool's ``schema`` column — the semantic doc.

    Two shapes reach the indexer.  A function tool stores its complete
    descriptor as compact JSON (``{name, description, inputSchema}``), so the
    doc covers name + description + each parameter + a return description.  A
    skill stores its SKILL.md verbatim — a playbook IS its documentation, so
    the text is the doc, indexing the whole thing for search.
    """
    text = schema_text or ""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return text.strip()
    if not isinstance(doc, dict):
        return ""

    name = doc.get("name")
    description = doc.get("description")
    inner = doc.get("inputSchema") if isinstance(doc.get("inputSchema"), dict) else doc
    if not isinstance(inner, dict):
        inner = {}

    lines: list[str] = []
    if isinstance(name, str) and name.strip():
        lines.append(f"name: {name.strip()}")
    if isinstance(description, str) and description.strip():
        lines.append(description.strip())

    required_raw = inner.get("required")
    required = set(required_raw) if isinstance(required_raw, list) else set()
    props = inner.get("properties")
    if isinstance(props, dict):
        params = [
            _param_line(pname, spec, pname in required)
            for pname, spec in props.items() if isinstance(spec, dict)
        ]
        if params:
            lines.append("params: " + "; ".join(params))

    for key in ("returns", "return", "result", "response"):
        ret = inner.get(key)
        if ret is None:
            continue
        ret_desc = ret.get("description") if isinstance(ret, dict) else ret
        if isinstance(ret_desc, str) and ret_desc.strip():
            lines.append(f"returns: {ret_desc.strip()}")
            break
    return "\n".join(lines)


#: What makes a row injectable — the ONE predicate the injection set is built
#: from, in SQL: **a function tool that is enabled and loaded**.
#:
#: - ``category IN`` the function set — skills and cli entries are never
#:   injected (their ``load_status`` is 'n/a', so this only makes the rule
#:   explicit).
#: - ``status = 'enabled'`` — the config switch AND the runtime verdict at
#:   once: a tool switched off in ``tools.yaml``, or one whose server is not
#:   up right now, leaves the injected set while the row keeps the load state
#:   the model chose.
#: - ``load_status = 'loaded'`` — the model's decision, the db's whole reason
#:   to exist.
#:
#: It is ``_effective_status``'s rule and MUST move with it.
_INJECTABLE_SQL = (
    f"{_FUNC_CATEGORY_SQL}"
    " AND load_status = 'loaded'"
    " AND status = 'enabled'"
)


def _effective_status(trow: dict) -> str:
    """Derived effective status (never stored): disabled / error / loaded /
    unloaded / enabled.

    Everything it needs is ON THE ROW, and in a deliberate order:

    1. ``status`` ≠ ``enabled`` → that value: ``disabled`` (the yaml switch)
       or ``error`` (the runtime verdict).  Either way the row's own state is
       the answer, and reporting it is what lets a reader tell "switched off"
       from "down".
    2. otherwise the row's own ``load_status`` — what the model decided.
    3. and for a row with no load state at all (skill / cli), ``enabled``:
       the status column IS its state, so echoing the sentinel ``'n/a'``
       would report "no information" about a row whose state is perfectly
       well known.

    The rule for step 2 is what keeps a ``loaded`` tool alive across a blip:
    the verdict lives in its own column, so it never overwrites the load
    state and the tool comes back ``loaded``.
    """
    status = trow.get("status") or STATUS_ENABLED
    if status != STATUS_ENABLED:
        return status
    load_status = trow.get("load_status")
    if not load_status or load_status == NA:
        return STATUS_ENABLED
    return load_status


def effective_from_row(row: dict) -> str:
    """Effective status for a search/scan row (row-only — no server aliases)."""
    return _effective_status(row)


#: The columns a search may filter on — the tool's filter parameters ARE
#: these, one-to-one, so the surface cannot drift from the table, and every
#: filter is a real predicate the SQL sees.  That is the fix for a filter
#: applied POST-hoc in Python: one ran after the ``limit * 2`` candidate
#: cutoff and returned 7 of the 14 qualifying rows, silently.
FILTER_COLUMNS = ("category", "source_id", "status", "load_status")


def column_filters(filters: "dict | None") -> tuple[list[str], list]:
    """``(clauses, params)`` for a column filter dict — AND-ed by the caller.

    A filter the caller did not supply contributes NO clause at all — not
    ``= NULL``, not a default value: a string column reads "not supplied" as
    emptiness.  Every column is NOT NULL and every value of interest is a
    non-empty string (``false`` / ``0`` / ``''`` are not in any of these
    domains), so every clause is a plain equality — no ``IS NULL`` branch for
    a caller to forget, which is what removing NULL bought.
    """
    f = filters or {}
    clauses: list[str] = []
    params: list = []
    for column in FILTER_COLUMNS:
        value = f.get(column)
        if value:
            clauses.append(f"t.{column} = ?")
            params.append(value)
    return clauses, params


# Row prefix used by the scan/search queries.
_SCAN_COLS = (
    "t.name, t.description, t.category, t.source_id, t.schema, "
    "t.status, t.load_status, t.last_loaded"
)
#: The catalog's TEXT columns — what ``tool_fts`` indexes, and therefore what
#: every text mode must read.  ONE list, because it used to be spelled out three
#: times and one spelling silently lost ``source_id``: the LIKE fallback read
#: four columns while the FTS index and grep read five, so a row matching only
#: in ``source_id`` was findable two ways and invisible the third.
_TEXT_COLUMNS = ("name", "description", "category", "source_id", "schema")

#: :data:`_TEXT_COLUMNS` qualified with the ``t`` alias the LIKE query uses.
_LIKE_COLUMNS = tuple(f"t.{c}" for c in _TEXT_COLUMNS)
# Keyword-result spine: scan columns + a description snippet anchored on the
# first search term + a zero rank.  Keyword searches order by category/name;
# rank only matters to the hybrid semantic merge.
_SEARCH_SELECT = (
    f"SELECT {_SCAN_COLS},"
    " substr(t.description, max(0, instr(t.description, ?) - 40), 160) AS snippet,"
    " 0 AS rank"
    " FROM tool t"
)


@dataclass
class CatalogOpDelta:
    """What a window of catalog writes did to the rows.

    Armed by :meth:`CatalogStore.begin_ops` and taken away by ``end_ops``, so
    the tool-sync line can report what a startup changed in the catalog:
    insert → ``added``, update → ``updated``, delete → ``removed``.  The
    window is the STARTUP's, not one pass's: it opens with the catalog and
    closes with the line (see :meth:`CatalogStore.begin_ops`).

    ``added`` counts the rows the user GAINED, so a row born switched off does
    not book one: the line prints these beside a count of what is callable
    (:meth:`CatalogStore.count_usable`), and both have to be asking the same
    question of the same ``status`` column.

    Only a **config-derived** column moves the counters.  ``status`` is
    partly config and partly runtime, so it is booked by the arm that wrote it
    (the config arm moves ``updated``; an ``override_status`` flip is counted
    separately) — ``load_status`` / ``last_loaded`` are the model's decisions
    and are runtime state, so a load, an eviction, an autoload override or a
    connectivity mark is not a change to the tool set and must not show up as
    one (a server coming up would otherwise report every one of its tools as
    "modified").
    """

    added: int = 0
    updated: int = 0
    removed: int = 0


class CatalogStore:
    """The shared catalog: SQLite file, WAL, FTS5 keyword + BLOB semantic.

    One instance per process (main agent / subagent / tests) on the same
    path.  Writers serialize on a per-instance ``asyncio.Lock`` (the WAL
    reader/writer split handles cross-process contention).  All ops are
    store-shaped; policy (whitelist, thresholds, refusal texts) is applied
    by :class:`~slife.tools.catalog_service.ToolCatalogService`.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        #: Armed by :meth:`begin_ops` (the process's startup window), cleared
        #: by :meth:`end_ops` (the tool-set line) — None means nobody is
        #: asking, so every write path's counting is a no-op call.
        self._ops: CatalogOpDelta | None = None

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None
        return self._conn

    # ── Op accounting ──────────────────────────────────────────────

    def begin_ops(self) -> CatalogOpDelta:
        """Arm the op collector; returns the accumulator the caller keeps.

        One window at a time, and the window is the process's STARTUP: armed
        where the catalog is opened, closed by ``end_ops`` where the tool-set
        line is reported.  It has to open that early — the boot seed, the
        skill/cli mirror and the plugins' connects all write rows before the
        first reconcile pass, and the pass that converges is not the pass that
        did the work (arming per pass reported one source's rows out of a
        whole catalog).  Reads accumulate until ``end_ops`` takes them; a
        second ``begin_ops`` starts a fresh window and DISCARDS what the
        previous one collected, so it is not a way to look without closing.
        """
        self._ops = CatalogOpDelta()
        return self._ops

    def end_ops(self) -> CatalogOpDelta:
        """Disarm the collector and return what it collected."""
        delta, self._ops = self._ops, None
        return delta or CatalogOpDelta()

    def _count_ops(
        self, *, added: int = 0, updated: int = 0, removed: int = 0,
    ) -> None:
        """Book row operations into the armed collector (a no-op when disarmed)."""
        delta = self._ops
        if delta is None:
            return
        delta.added += added
        delta.updated += updated
        delta.removed += removed

    # ── Lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the file db with the WAL cross-process pragmas + schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self._path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        busy_ms = int(_timeouts.timeouts.storage.sqlite_busy * 1000)
        await conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        await conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        await self._migrate()
        await self._run_schema()
        await self._check_categories()
        await self._check_columns()
        logger.info("catalog_ready path=%s", self._path)

    async def _check_categories(self) -> None:
        """Verify the live ``tool`` table accepts every category the code writes.

        An older file keeps the ``CHECK`` it was created with — ``CREATE TABLE
        IF NOT EXISTS`` never touches it, and there is no migration (a stale
        catalog is DELETED and rebuilt, not upgraded: every row is derived
        from the registry, ``tools.yaml``, the skills dir and the plugin
        children).  The file's ``user_version`` reads current either way, so
        the DDL itself is the only honest signal.

        Why it matters: a category the constraint rejects fails the INSERT —
        and the plugin mirror is best-effort, so the tools would silently stay
        uncatalogued (invisible to ``tool_search`` and to the per-turn
        injection).  Report it loudly and let ``system_health`` carry it.
        """
        try:
            cursor = await self._c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tool'",
            )
            row = await cursor.fetchone()
        except Exception as e:  # a probe failure is never fatal
            logger.debug("catalog_ddl_probe_failed err=%s", e)
            return
        allowed = _category_check_values((row[0] or "") if row else "")
        if allowed is None:  # no constraint at all → nothing can be rejected
            return
        missing = sorted(ALL_CATEGORIES - allowed)
        if not missing:
            return
        logger.error(
            "catalog_schema_stale path=%s missing_categories=%s action=delete_the_file",
            self._path, ",".join(missing),
        )
        from slife.health import record
        record(
            "tool_catalog", "warning", key="schema",
            value=f"stale (no {'/'.join(missing)} category)",
            hint=f"Delete {self._path} and restart slife. The catalog is "
                 f"rebuilt from the tool registry, tools.yaml and the plugins, "
                 f"so nothing is lost but the loaded/unloaded state.",
        )

    async def _check_columns(self) -> None:
        """Verify the live ``tool`` table's columns are exactly this code's.

        Same doctrine as :meth:`_check_categories`, same reason to verify the
        DDL rather than ``user_version``: ``CREATE TABLE IF NOT EXISTS`` never
        touches an existing file, and these revisions have no migration step —
        a stale catalog is DELETED and rebuilt.  Without this check an older
        file fails every scan with ``no such column: status``, which reads
        as a code bug instead of "delete the derived file".

        Checked in BOTH directions.  A MISSING column cannot answer a query; an
        UNEXPECTED one is a leftover from a schema this code no longer writes
        (the dropped ``type`` projection, say) — nothing maintains it, and
        leaving it in place forever is exactly the maintenance burden the
        column was dropped to remove.
        """
        try:
            cursor = await self._c.execute("PRAGMA table_info(tool)")
            columns = {r[1] for r in await cursor.fetchall()}
        except Exception as e:  # a probe failure is never fatal
            logger.debug("catalog_column_probe_failed err=%s", e)
            return
        if not columns:  # no table yet → the schema creates it complete
            return
        missing = sorted(_REQUIRED_COLUMNS - columns)
        unexpected = sorted(columns - _REQUIRED_COLUMNS)
        if not missing and not unexpected:
            return
        detail = "/".join(
            [*(f"no {c} column" for c in missing),
             *(f"unknown {c} column" for c in unexpected)]
        )
        logger.error(
            "catalog_schema_stale path=%s missing_columns=%s unexpected_columns=%s "
            "action=delete_the_file",
            self._path, ",".join(missing), ",".join(unexpected),
        )
        from slife.health import record
        record(
            "tool_catalog", "warning", key="schema",
            value=f"stale ({detail})",
            hint=f"Delete {self._path} and restart slife. The catalog is "
                 f"rebuilt from the tool registry, tools.yaml and the plugins, "
                 f"so nothing is lost but the loaded/unloaded state.",
        )

    async def _migrate(self) -> None:
        """Bring an older db up to ``SCHEMA_VERSION`` before the schema runs.

        v2 dropped the ``server`` table: connection state lives on the tool
        rows now (the ``status`` column's ``error``, when a server is
        unusable), so the table is dead weight AND a stale source of truth.
        ``CREATE TABLE IF NOT EXISTS`` would leave it in place forever — the
        only place a retired table can be removed is a migration step like
        this one.

        Everything since has NO step here on purpose.  v3 added ``tool.type``
        (later dropped again in v9 — a projection of ``category`` is a second
        thing to keep in sync, and every question it answered is a membership
        test over the category); v4 added the ``plugin`` value to the
        ``category`` CHECK; v5 renamed ``status`` → ``load_status`` and gave
        the column a closed domain (``loaded | unloaded | n/a``); v8 collapsed
        the ``enabled`` / ``unavailable`` pair into one three-state ``status``.
        Each one needs the table rebuilt or a column added-and-backfilled —
        and this file is DERIVED data (every row comes from the registry,
        ``tools.yaml``, the skills dir or the plugin children), so a stale
        catalog is deleted and rebuilt from its sources instead of being
        upgraded in place.  :meth:`_check_categories` and
        :meth:`_check_columns` report such a file rather than letting its
        writes fail silently; the cost of the rebuild is only the load
        decisions the model made this session.
        """
        cursor = await self._c.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = row[0] if row else 0
        if version >= SCHEMA_VERSION:
            return
        if version < 2:
            await self._c.execute("DROP TABLE IF EXISTS server")
            logger.info("catalog_migrated from=%s to=2 dropped=server", version)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _run_schema(self) -> None:
        schema_path = Path(__file__).parent / "catalog_schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        # Execute each statement individually — FTS/trigger virtual tables can
        # hang in aiosqlite's executescript.
        for stmt in _split_sql(schema_sql):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                await self._c.execute(stmt)
            except Exception as e:
                logger.error("catalog_schema_stmt_error err=%s stmt=%.80s", e, stmt)
        await self._c.commit()

    # ── Upserts ────────────────────────────────────────────────────

    async def reconcile(
        self,
        rows: "list[dict]",
        *,
        category: str = "",
        purge: bool = False,
    ) -> dict[str, Any]:
        """Apply a source's whole row set as a delta — write only what differs.

        The boot-seed / category-mirror contract: a name that is not in the
        catalog INSERTs, a name the source dropped DELETEs (when *purge*), and
        a name already present is compared **in memory** and written only if a
        column actually moved.  A steady-state boot therefore touches no row at
        all — one read, no writes, and no FTS churn.  (``tool_au`` fires on
        every UPDATE, so the previous unconditional upsert re-indexed all ~1500
        rows into ``tool_fts`` on every start, however little had changed.)

        **The comparison is these five columns**: ``description``,
        ``category``, ``source_id``, ``schema``, ``status`` — the config-derived
        set, and the whole of what a boot-seed pass can know.  ``load_status``
        and ``last_loaded`` are NOT in it: they are the model's decisions and
        the runtime's state, so a re-register must never look like a change to
        them (they move only through ``override_status``, and are counted
        separately when they do).  ``name`` is the key being compared.

        Each entry carries ``name`` plus the written columns (``description``,
        ``category``, ``source_id``, ``schema``, ``status``).  ``status`` is
        ``None`` for *no opinion*, which leaves the column alone.  A non-None
        value is written as the CONFIG mirror by default — ``enabled`` /
        ``disabled``, crossing the switch line and nothing else
        (``_config_status_move``), so the runtime's ``error`` is
        :meth:`mark_source_error`'s to write and :meth:`mark_source_connected`'s
        to clear, and a mirror that ran while an owner happened to be down
        cannot erase that verdict.  ``status_verdict`` marks the rows where the
        SOURCE is the verdict's author instead (the skill / cli mirror: a
        SKILL.md that cannot be read is ``error``, and reading it again after a
        fix is what puts it back to ``enabled``).

        ``load_status`` applies to a NEW row only: an existing row keeps its
        loaded/unloaded state, so a plugin re-register can never clobber a
        user unload.  The one exception is ``override_status`` (see below):
        the config's autoload statement, which owns the row's state and
        rewrites it when it differs.  ``category`` is written as given —
        nothing is derived from it into a second column.

        Returns ``{inserted, updated, status_updated, skipped, purged,
        schema_changed}``.  Every list but ``skipped`` (a count) holds names.
        ``updated`` is the rows whose CONFIG-DERIVED columns moved;
        ``status_updated`` is the ones only an ``override_status`` flip
        touched — runtime state, a deliberately separate answer (the split
        :class:`CatalogOpDelta` counts).  ``schema_changed`` names the rows
        whose embedding was invalidated — a new row with an embeddable schema,
        or one whose ``schema`` text moved — for the caller to wake the
        drainer.
        """
        async with self._write_lock:
            where = " WHERE t.category = ?" if category else ""
            cursor = await self._c.execute(
                f"SELECT {_SCAN_COLS} FROM tool t{where}",
                (category,) if category else (),
            )
            existing = {r["name"]: dict(r) for r in await cursor.fetchall()}

            inserted: list[str] = []
            #: Of ``inserted``, the ones the user GAINED — see the accounting
            #: note in ``_count_ops`` below.
            usable_added = 0
            updated: list[str] = []
            status_updated: list[str] = []
            invalidated: list[str] = []
            incoming: set[str] = set()
            skipped = 0

            for row in rows:
                name = row.get("name") or ""
                if not name:
                    continue
                incoming.add(name)
                prev = existing.get(name)
                new_schema = row.get("schema")
                # Config's opinion about the row's status — NOT the load
                # state's, which is ``row["load_status"]`` below.
                new_tool_status = row.get("status")

                if prev is None:
                    await self._c.execute(
                        """INSERT INTO tool(name, description, category,
                                            source_id, schema, status, load_status,
                                            last_loaded)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            name, row.get("description") or "",
                            row.get("category", "") or "",
                            row.get("source_id") or NA, new_schema or NA,
                            new_tool_status or STATUS_ENABLED,
                            row.get("load_status") or STATUS_NA, "",
                        ),
                    )
                    inserted.append(name)
                    # A row born SWITCHED OFF is not a tool the user gained:
                    # the config declares it, so the db carries it (yaml and db
                    # agree), but nothing became callable.  ``count_usable``
                    # asks the same question of the same column, so booking
                    # this as ``added`` would have the tool-set line report
                    # more added than usable — the mismatch that made a cold
                    # start read ``新增 1586 … 1575 个工具可用``.
                    if (new_tool_status or STATUS_ENABLED) != STATUS_DISABLED:
                        usable_added += 1
                    if _embeddable(new_schema):
                        invalidated.append(name)
                    continue

                # Build the SET list from the columns that actually moved, so
                # an enabled-only flip does not rewrite a multi-KB schema blob.
                # ``or NA`` on BOTH sides: "the caller said nothing" and "the
                # column holds 'n/a'" are the same fact, so a steady-state
                # reconcile still writes nothing (the no-op contract).
                schema_moved = (prev["schema"] or NA) != (new_schema or NA)
                sets: list[str] = []
                vals: list = []
                if (prev["description"] or "") != (row.get("description") or ""):
                    sets.append("description = ?")
                    vals.append(row.get("description") or "")
                if prev["category"] != (row.get("category", "") or ""):
                    sets.append("category = ?")
                    vals.append(row.get("category", "") or "")
                if (prev["source_id"] or NA) != (row.get("source_id") or NA):
                    sets.append("source_id = ?")
                    vals.append(row.get("source_id") or NA)
                if schema_moved:
                    sets.append("schema = ?")
                    vals.append(new_schema or NA)
                # The status arm has two writers, and a row's value comes from
                # exactly one of them:
                # - the CONFIG mirror (every family): it may only cross the
                #   switch line, so it never clears an ``error`` the runtime
                #   wrote — a config projection may not claim an owner is up;
                # - the SOURCE itself (``status_verdict`` — the skill / cli
                #   mirror): that row's status IS the verdict (a SKILL.md that
                #   cannot be read), so it writes whatever it found, which is
                #   also what puts a fixed file back to ``enabled``.
                if new_tool_status is not None:
                    prev_status = prev["status"] or STATUS_ENABLED
                    target = prev_status
                    if row.get("status_verdict"):
                        target = new_tool_status
                    else:
                        value, from_values = _config_status_move(new_tool_status)
                        if prev_status in from_values:
                            target = value
                    if target != prev_status:
                        sets.append("status = ?")
                        vals.append(target)
                # ``override_status`` is the config's autoload statement: the
                # row is loaded because tools.yaml says so, so the incoming
                # status is an AUTHORITY rather than an insert default and may
                # overwrite what the model decided.  Owned here (not by the
                # caller) for the same reason as every other column: one
                # place compares, and only a value that MOVES is written.
                # Settled before the load-status arm below: the question this
                # answers is "did a CONFIG-DERIVED column move", and the
                # autoload override is not one.
                content_moved = bool(sets)
                new_status = row.get("load_status")
                if row.get("override_status") and new_status is not None and (
                    (prev["load_status"] or None) != new_status
                ):
                    sets.append("load_status = ?")
                    vals.append(new_status)
                    if new_status == STATUS_LOADED:
                        # The same recency bump ``set_load_status(bump=True)`` does,
                        # so a tool the config loaded is not the first eviction
                        # victim if the autoload flag is ever dropped.
                        sets.append("last_loaded = ?")
                        vals.append(_now())

                if not sets:
                    skipped += 1
                    continue
                vals.append(name)
                await self._c.execute(
                    f"UPDATE tool SET {', '.join(sets)} WHERE name = ?", vals,
                )
                (updated if content_moved else status_updated).append(name)
                # The schema moved, so the vectors were built from text that is
                # no longer there: drop them and let the drainer re-embed.  This
                # is the ONE embedding trigger — nothing else deletes a vector,
                # so "has no vectors" means "new, or its schema moved".
                if schema_moved:
                    invalidated.append(name)

            purged: list[str] = []
            if purge and category:
                gone = sorted(n for n in existing if n not in incoming)
                if gone:
                    await self._delete_rows(gone)
                    purged = gone

            # Stale vectors go with the rows that moved: the drainer re-embeds
            # off count_unembedded, which now sees exactly these names.
            if invalidated:
                await self._c.execute(
                    f"DELETE FROM tool_embeddings "
                    f"WHERE name IN ({in_placeholders(len(invalidated))})",
                    invalidated,
                )

            await self._c.commit()

        # ``added`` is the usable rows gained, not the rows written: the
        # tool-set line pairs it with a count of what is usable NOW
        # (``count_usable``), so a row born switched off — written, but never
        # callable — books nothing on either side.  The log line below still
        # reports the raw write count, which is what a log is for.
        self._count_ops(
            added=usable_added, updated=len(updated), removed=len(purged),
        )
        logger.info(
            "catalog_reconcile category=%s inserted=%d updated=%d status=%d "
            "skipped=%d purged=%d",
            category or "(all)", len(inserted), len(updated),
            len(status_updated), skipped, len(purged),
        )
        return {
            "inserted": inserted,
            "updated": updated,
            "status_updated": status_updated,
            "skipped": skipped,
            "purged": purged,
            "schema_changed": invalidated,
        }

    async def upsert_tool(
        self,
        name: str,
        *,
        description: str = "",
        category: str,
        source_id: str | None = None,
        schema: str | None = None,
        status: str | None = None,
        load_status: str | None = None,
        override_status: bool = False,
    ) -> bool:
        """Upsert one tool row; returns True iff its ``schema`` text changed.

        The single-row face of :meth:`reconcile`, so the comparison that
        decides "re-embed or not" has exactly one implementation.  See that
        method for the column contracts (``status=None`` = no opinion,
        ``load_status`` on a NEW row only — unless ``override_status``).
        """
        result = await self.reconcile([{
            "name": name, "description": description, "category": category,
            "source_id": source_id, "schema": schema, "status": status,
            "load_status": load_status, "override_status": override_status,
        }])
        return name in result["schema_changed"]

    async def purge_source(self, source_id: str) -> int:
        """Delete every tool row owned by one external server.

        The removal path (config removal, or a server disabled in
        ``tools.yaml``): a server that is off owns no rows — its tools are
        re-mirrored when it connects again.  Returns the number of rows
        removed; embeddings follow via the FK cascade.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "SELECT COUNT(*) FROM tool WHERE source_id = ?", (source_id,),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await self._c.execute(
                "DELETE FROM tool WHERE source_id = ?", (source_id,),
            )
            # Embedding chunks go with the rows (tool_embeddings.name has
            # ON DELETE CASCADE and foreign_keys is ON).
            await self._c.commit()
        self._count_ops(removed=count)
        logger.info("catalog_source_purged source=%s tools=%d", source_id, count)
        return count

    async def purge_source_except(self, source_id: str, keep: "set[str]") -> list[str]:
        """Delete one owner's rows for every name NOT in *keep*.

        The per-tool half of :meth:`purge_source`: the owner is still
        configured and still reachable, it simply stopped publishing one of
        its tools.  Without this the vanished tool keeps its row, so
        ``tool_search`` goes on offering it and ``func_tool_load`` materializes
        a proxy with nothing behind it.

        *keep* MUST be the complete set the owner publishes now — a partial
        list deletes the difference, which is why both callers refuse to call
        this on an empty listing (empty means "not ready yet", never "owns
        nothing"; the registry diff skips the same case for the same reason).

        Returns the removed names.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "SELECT name FROM tool WHERE source_id = ?", (source_id,),
            )
            gone = sorted(r[0] for r in await cursor.fetchall() if r[0] not in keep)
            if gone:
                await self._delete_rows(gone)
                await self._c.commit()
        if gone:
            self._count_ops(removed=len(gone))
            logger.info(
                "catalog_source_purged_except source=%s tools=%d", source_id, len(gone),
            )
        return gone

    async def purge_missing_sources(self, keep: "set[str]") -> "set[str]":
        """Purge the rows of every server NOT in *keep*; returns what was purged."""
        purged: set[str] = set()
        for source_id in sorted(await self.list_source_ids() - keep):
            await self.purge_source(source_id)
            purged.add(source_id)
        return purged

    async def list_source_ids(
        self, categories: "frozenset[str] | set[str] | None" = SERVER_CATEGORIES,
    ) -> "set[str]":
        """Every source name that currently owns tool rows.

        Defaults to :data:`SERVER_CATEGORIES` — "which servers own rows", the
        question the health count asks and the one ``purge_missing_sources``
        must ask, since its ``keep`` set is the *configured* server list and
        would otherwise delete every plugin-owned row.  Pass ``None`` for
        every source regardless of category.
        """
        sql = "SELECT DISTINCT source_id FROM tool WHERE source_id != ?"
        params: list = [NA]
        if categories:
            sql += f" AND category IN ({in_placeholders(len(categories))})"
            params += sorted(categories)
        cursor = await self._c.execute(sql, params)
        return {row[0] for row in await cursor.fetchall()}

    async def set_source_enabled(self, source_id: str, enabled: bool) -> int:
        """Move one source's rows across the config's switch line.

        The whole of the sync's business with this column: a server switched
        off in ``tools.yaml`` is a row that reports ``disabled``.  Deliberately
        narrow:

        - ``load_status`` is NOT touched, so the model's loaded/unloaded
          decision survives a disable/enable round trip untouched.
        - the rows are NOT deleted, so re-enabling restores a tool set that
          still remembers what was loaded.
        - which rows move is ``_config_status_move``'s rule, the same one the
          mirror uses — switching off lands on an ``error`` row (off is not
          down), switching on only un-switches and leaves the runtime's
          ``error`` alone.
        - only a row that actually MOVES is written: config overrides the db
          value, but an override that overrides nothing is not a write.  Every
          pass projects the whole server list, and ``tool_au`` fires on any
          UPDATE, so the unconditioned form re-indexed every external row into
          ``tool_fts`` once per reconcile — the FTS churn ``reconcile`` exists
          to avoid.

        Returns the number of rows whose value moved (0 on a steady-state pass).
        """
        value, from_values = _config_status_move(
            STATUS_ENABLED if enabled else STATUS_DISABLED,
        )
        async with self._write_lock:
            cursor = await self._c.execute(
                f"UPDATE tool SET status = ? "
                f"WHERE source_id = ? AND status IN ({in_placeholders(len(from_values))})",
                (value, source_id, *from_values),
            )
            await self._c.commit()
        # ``status``'s config arm is a config-derived column: a server switched
        # off in tools.yaml IS a change to those tools.  The WHERE clause keeps
        # it honest — a steady-state pass writes no row and counts nothing.
        self._count_ops(updated=cursor.rowcount)
        return cursor.rowcount

    async def mark_source_error(self, source_id: str) -> int:
        """Flag one owner's tools ``error`` — its server/plugin is unusable.

        The runtime arm, and its own lane: ``load_status`` is untouched, so the
        rows keep saying what the model decided (``loaded`` / ``unloaded``) and
        they simply leave the injection set while the mark is up — the decision
        is still there when the owner comes back.  Only rows that can have a
        load state are flagged (the same set the old status write covered) and
        only rows not already flagged are written: every reconcile pass
        re-projects every unreachable server, so the unconditioned form
        rewrote — and, through ``tool_au``, re-indexed — the same rows on each
        pass.  A row the config switched off is NOT flagged: it is off, and
        the model must be able to tell that from down.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                f"UPDATE tool SET status = 'error' "
                f"WHERE source_id = ? AND {_FUNC_CATEGORY_SQL} "
                f"AND status = 'enabled'",
                (source_id,),
            )
            await self._c.commit()
        return cursor.rowcount

    async def mark_all_external_error(self) -> int:
        """Flag EVERY external tool ``error`` — the gateway child died.

        All of its servers are unreachable at once, so their tools must leave
        the injection set immediately rather than at the next reconcile.

        Only rows not already flagged are written — a gateway that dies, is
        restarted and dies again must not re-flag (and re-index) the whole
        external set.  Returns the number of rows newly flagged.
        """
        async with self._write_lock:
            placeholders = ",".join("?" * len(SERVER_CATEGORIES))
            cursor = await self._c.execute(
                f"UPDATE tool SET status = 'error' "
                f"WHERE category IN ({placeholders}) "
                f"AND status = 'enabled'",
                (*sorted(SERVER_CATEGORIES),),
            )
            await self._c.commit()
        return cursor.rowcount

    async def mark_source_connected(self, source_id: str) -> int:
        """Clear one owner's verdict — it is usable again.

        The only thing a reconnect does to the rows: every tool keeps the
        ``loaded`` / ``unloaded`` it had, which is what makes the load state
        survive a blip, a restart, and a plugin restart alike.  Scoped to rows
        currently marked ``error``, so the config's own ``disabled`` is never
        undone by a reconnect.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET status = 'enabled' "
                "WHERE source_id = ? AND status = 'error'",
                (source_id,),
            )
            await self._c.commit()
        return cursor.rowcount

    async def _delete_rows(self, names: "list[str]") -> int:
        """DELETE *names* and their embedding chunks; returns the rows removed.

        The ONE spelling of a tool-row removal, shared by every batch purge —
        :meth:`remove_tools`, :meth:`purge_source_except`, ``reconcile``'s
        purge.  The caller holds the write lock (each of those deletes as part
        of a read-then-write under one lock, and taking it again here would
        deadlock).  The embeddings' delete is explicit: it must not depend on
        the FK cascade being enabled.
        """
        if not names:
            return 0
        ph = in_placeholders(len(names))
        cursor = await self._c.execute(
            f"DELETE FROM tool WHERE name IN ({ph})", names,
        )
        await self._c.execute(
            f"DELETE FROM tool_embeddings WHERE name IN ({ph})", names,
        )
        return cursor.rowcount or 0

    async def remove_tools(self, names: "list[str]") -> list[str]:
        """Delete several tool rows at once — the batch face of ``remove_tool``.

        What a whole family vanishing costs: ONE statement for the set rather
        than one per row.  Returns the names asked for, sorted; ``removed``
        books the rows actually deleted, so a name that was not there is
        counted once and only here (the caller derived them from the db, so the
        two agree unless someone else got there first).
        """
        gone = sorted(set(names))
        async with self._write_lock:
            removed = await self._delete_rows(gone)
            await self._c.commit()
        if gone:
            self._count_ops(removed=removed)
            logger.info("catalog_tools_removed tools=%d names=%r", removed, gone)
        return gone

    async def remove_tool(self, name: str) -> None:
        """Delete a single tool row plus its embedding chunks.

        Used when a non-server tool disappears at runtime (a job file
        removed, a plugin dropping a tool) — the §8.5 "remove 清理干净"
        contract: a vanished tool must not linger as a stale catalog row
        that tool_search keeps returning.
        """
        async with self._write_lock:
            removed = await self._delete_rows([name])
            await self._c.commit()
        self._count_ops(removed=removed)

    async def names_by_category(self, category: str) -> set[str]:
        """Every row name of one category — the mirror's purge diff basis."""
        cursor = await self._c.execute(
            "SELECT name FROM tool WHERE category = ?", (category,),
        )
        return {row[0] for row in await cursor.fetchall()}

    async def count_usable(self) -> int:
        """How many rows are callable right now — every family, load state aside.

        The tool-set line's ``total``, and deliberately not a registry count:
        the registry holds registered tool INSTANCES, and ``skill`` / ``cli``
        have none — they are rows, not instances (a skill IS its SKILL.md text,
        a cli row its command), so no registry count can ever see them.  They
        are usable all the same; they simply have no load state to flip, which
        is the whole of what :data:`FUNCTION_CATEGORIES` splits on.

        ``status`` is the entire test: ``enabled`` is "neither switched off nor
        marked down", so a row the config disabled and a row whose server is in
        ``error`` are both correctly absent from what the line promises.
        """
        cursor = await self._c.execute(
            "SELECT COUNT(*) FROM tool WHERE status = ?", (STATUS_ENABLED,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def has_error_rows(self, categories: "set[str] | frozenset[str]") -> bool:
        """Whether any row of these categories is marked ``error`` right now.

        The re-check trigger for the source-fed families (skill / cli): their
        status comes from the SOURCE — a SKILL.md that cannot be read — not
        from a probe the host runs anyway, so nothing else would ever ask
        again.  A server's mark is re-projected by every connectivity pass; a
        file's has to be re-read, and the caller's mtime gate only fires when
        an entry in the directory changes.  A skill that merely became readable
        again (a permission fixed, a dropped drive remounted) moves no mtime,
        so without this the row would stay ``error`` until the next boot.
        """
        if not categories:
            return False
        params = sorted(categories)
        cursor = await self._c.execute(
            f"SELECT 1 FROM tool WHERE status = ? "
            f"AND category IN ({in_placeholders(len(params))}) LIMIT 1",
            (STATUS_ERROR, *params),
        )
        return await cursor.fetchone() is not None

    async def names_for_sources(self, sources) -> set[str]:
        """Every row name owned by the given servers — the autoload-protect set."""
        wanted = {s for s in sources if s}
        if not wanted:
            return set()
        names = tuple(sorted(wanted))
        cursor = await self._c.execute(
            f"SELECT name FROM tool WHERE source_id IN ({in_placeholders(len(names))})",
            names,
        )
        return {row[0] for row in await cursor.fetchall()}

    # ── Status flips ───────────────────────────────────────────────

    async def set_load_status(self, name: str, load_status: str, *, bump: bool = False) -> int:
        """Set a tool's ``load_status`` (loaded/unloaded/n/a); bump → LRU refresh.

        Only meaningful for a function tool's row (skill/cli stay 'n/a'); the
        store is lenient — callers guard with the category.
        """
        last_loaded = _now() if (bump and load_status == STATUS_LOADED) else None
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET load_status = ?, last_loaded = "
                "CASE WHEN ? IS NULL THEN last_loaded ELSE ? END"
                " WHERE name = ?",
                (load_status, last_loaded, last_loaded, name),
            )
            await self._c.commit()
        return cursor.rowcount

    async def touch(self, name: str) -> None:
        """Bump a loaded tool's ``last_loaded`` (LRU recency) on successful use.

        ``evict_lru`` orders NULL-first then by ``last_loaded``; without a
        per-use bump every seeded tool stays NULL and eviction degrades to a
        plain alphabetical slice (never the least-recently-used).  Only
        touched when the row is still ``loaded`` so a call made after an
        eviction never re-arms an evicted tool.
        """
        async with self._write_lock:
            await self._c.execute(
                "UPDATE tool SET last_loaded = ?"
                " WHERE name = ? AND load_status = 'loaded'",
                (_now(), name),
            )
            await self._c.commit()

    # ── Reads / effective status ───────────────────────────────────

    async def rows_for_names(self, names) -> list[dict]:
        """Catalog rows (name + schema) for *names* — the injection schema
        source.  The loop builds its LLM tool list from this column, so the
        catalog (not tool code, not a live MCP fetch) owns the injected
        schemas."""
        names = [n for n in names]
        if not names:
            return []
        ph = in_placeholders(len(names))
        cursor = await self._c.execute(
            f"SELECT name, schema FROM tool WHERE name IN ({ph})", names,
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_tool(self, name: str) -> dict | None:
        cursor = await self._c.execute(
            f"SELECT {_SCAN_COLS} FROM tool t WHERE t.name = ?", (name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_effective(self, name: str) -> str | None:
        """Effective status for one tool name (None if unknown)."""
        row = await self.get_tool(name)
        if row is None:
            return None
        return _effective_status(row)

    async def scan_effective(
        self, category: str = "",
    ) -> list[dict]:
        """Every tool row, annotated with its effective status.

        Returns rows: ``name/description/category/source_id/schema/status/
        last_loaded/eff`` — the display/search shape.
        """
        clauses = ""
        params: list = []
        if category:
            clauses = "WHERE t.category = ?"
            params.append(category)
        cursor = await self._c.execute(
            f"SELECT {_SCAN_COLS} FROM tool t {clauses} "
            f"ORDER BY t.category, t.name",
            params,
        )
        rows = []
        for row in await cursor.fetchall():
            r = dict(row)
            r["eff"] = _effective_status(r)
            rows.append(r)
        return rows

    async def loaded_names(self) -> list[str]:
        """Injectable tool names: ``load_status == 'loaded'`` and ``status ==
        'enabled'``.

        Everything is on the row now.  An external tool whose server is down
        is not injectable — the reconcile marked it ``error`` — so it drops
        out of the injection set without a join, exactly as the retired
        server-row join used to arrange.  See :data:`_INJECTABLE_SQL`.
        """
        cursor = await self._c.execute(
            f"SELECT name FROM tool WHERE {_INJECTABLE_SQL} ORDER BY name"
        )
        return [row[0] for row in await cursor.fetchall()]

    async def count_loaded(self) -> int:
        cursor = await self._c.execute(
            f"SELECT COUNT(*) FROM tool WHERE {_INJECTABLE_SQL}"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def evict_lru(self, limit: int, protected: frozenset | set = frozenset()) -> list[str]:
        """Evict the oldest-by-``last_loaded`` loaded tools, down to best effort.

        Evicts up to *limit* rows (the caller bounds it to the over-threshold
        excess).  ``protected`` (the meta whitelist) is never evicted.  Rows
        with a NULL ``last_loaded`` sort oldest (evict first).  Returns the
        evicted names (they now have ``load_status='unloaded'``).
        """
        limit = max(0, limit)
        if limit == 0:
            return []
        protected_expr = ""
        params: list = [limit]
        if protected:
            protected_expr = f" AND name NOT IN ({in_placeholders(len(protected))})"
            params = [*sorted(protected), limit]
        async with self._write_lock:
            cursor = await self._c.execute(
                f"""SELECT name FROM tool
                    WHERE load_status = 'loaded'{protected_expr}
                    ORDER BY last_loaded ASC, name
                    LIMIT ?""",
                params,
            )
            names = [row[0] for row in await cursor.fetchall()]
            if names:
                ph = in_placeholders(len(names))
                await self._c.execute(
                    f"UPDATE tool SET load_status = 'unloaded'"
                    f" WHERE name IN ({ph}) AND load_status = 'loaded'",
                    names,
                )
            await self._c.commit()
        if names:
            logger.info("catalog_evict count=%d names=%r", len(names), names)
        return names

    # ── Search ─────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20, filters: "dict | None" = None,
    ) -> list[dict]:
        """FTS5 keyword search, CJK-routed to :meth:`_search_like`.

        Cross-category (no server-join gate; effective status is layered on
        by the caller via ``scan_effective``/``eff_map``).
        """
        limit = _clamp_limit(limit)
        if _contains_cjk(query):
            return await self._search_like(query, limit=limit, filters=filters)
        fts_query = _to_fts5_query(query)
        filter_clauses, filter_params = column_filters(filters)
        clauses = "".join(f" AND {c}" for c in filter_clauses)
        params: list = [fts_query] + filter_params + [limit]
        try:
            cursor = await self._c.execute(
                f"""SELECT {_SCAN_COLS},
                          snippet(tool_fts, 1, '…', '…', '…', 40) AS snippet, rank
                   FROM tool_fts fts
                   JOIN tool t ON fts.rowid = t.rowid
                   WHERE tool_fts MATCH ?{clauses}
                   -- bm25 weights (fts cols: name, description, category,
                   -- source_id, schema): name dominates, schema generic JSON
                   -- words (string/array) get 0.5.
                   ORDER BY bm25(tool_fts, 5.0, 2.0, 1.0, 1.0, 0.5) LIMIT ?""",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]
        except aiosqlite.OperationalError as e:
            logger.debug("catalog_search_keyword_parse_error query=%s err=%s", query, e)
            return []

    async def _search_like(
        self, pattern: str, limit: int,
        filters: "dict | None" = None,
    ) -> list[dict]:
        """Substring (LIKE) search — CJK fallback for :meth:`search_keyword`.

        Space-split words all must match (AND semantics) across every text
        column; each CJK word matches by substring.  The predicate comes from the
        shared :func:`~slife.plugins.memdb.store._like_terms` — the same builder
        memdb's ``_search_like`` and memfiles' ``_like_search_kind`` use — so all
        three stores answer one CJK query the same way, and the column set cannot
        drift from theirs again.
        """
        words = [w for w in pattern.split() if w]
        if not words:
            return []
        clause, like_params = _like_terms(words, _LIKE_COLUMNS)
        return await self._search_by_sql(
            [clause], [words[0], *like_params], filters, limit,
        )

    #: The text `grep` matches against — :data:`_TEXT_COLUMNS` itself, so the
    #: three text modes see one corpus by construction rather than by review.
    _GREP_COLUMNS = _TEXT_COLUMNS

    async def browse(
        self, limit: int = 20, filters: "dict | None" = None,
    ) -> list[dict]:
        """Rows passing the column filters, in report order — no text match.

        What an EMPTY query means.  Running the search legs on an empty string
        was worse than useless: the semantic leg answers with whatever the
        index happens to hold, so a row with no embedding (a cli entry has no
        tool def to embed) could never be listed, while the keyword leg
        matched nothing at all.  "No query" is not "no results" — it is the
        catalog itself, which is also how a family gets enumerated.
        """
        limit = _clamp_limit(limit)
        filter_clauses, params = column_filters(filters)
        where = " AND ".join(filter_clauses) if filter_clauses else "1=1"
        cursor = await self._c.execute(
            f"SELECT {_SCAN_COLS} FROM tool t WHERE {where} "
            f"ORDER BY t.category, t.name LIMIT ?",
            (*params, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def search_grep(
        self, pattern: str, limit: int = 20, filters: "dict | None" = None,
    ) -> list[dict]:
        """REGEX search over the catalog's text columns.

        A real ``grep``: the pattern is a Python regex (``re.search``), so
        ``translat(e|or)`` and ``summ.rize`` match.  SQLite has no regexp
        engine, so the match runs in Python — the column filters still narrow
        in SQL, and only the text predicate is evaluated here.  That means a
        scan rather than an index seek, which the catalog can afford (bounded
        by its own row count) and which buys the property the LIKE version
        could not have: every filtered row is examined, so the result is the
        first *limit* matches, not the first limit candidates.

        Raises ``re.error`` for an unusable pattern — the caller turns that
        into a message rather than an empty result.
        """
        rx = re.compile(pattern)
        limit = _clamp_limit(limit)
        filter_clauses, filter_params = column_filters(filters)
        where = " AND ".join(filter_clauses) if filter_clauses else "1=1"
        cursor = await self._c.execute(
            f"SELECT {_SCAN_COLS} FROM tool t WHERE {where} "
            f"ORDER BY t.category, t.name",
            filter_params,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        hits: list[dict] = []
        for row in rows:
            if any(rx.search(str(row.get(col) or "")) for col in self._GREP_COLUMNS):
                hits.append(row)
                if len(hits) >= limit:
                    break
        logger.debug("catalog_search_grep pattern=%s scanned=%s hits=%s",
                     pattern[:80], len(rows), len(hits))
        return hits

    async def _search_by_sql(
        self, clauses: list[str], params: list, filters: "dict | None", limit: int,
    ) -> list[dict]:
        """Run one keyword ``AND``-clause search against the shared spine.

        Both LIKE flavours build their clauses+params (the first param of
        *params* is the snippet anchor — the first search term), then append
        the optional category filter and the LIMIT here.
        """
        filter_clauses, filter_params = column_filters(filters)
        where = " AND ".join(clauses + filter_clauses)
        params = params + filter_params + [limit]
        cursor = await self._c.execute(
            f"{_SEARCH_SELECT} WHERE {where} ORDER BY t.category, t.name LIMIT ?",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def search_semantic(
        self, vec: list[float], limit: int = 20, filters: "dict | None" = None,
    ) -> list[dict]:
        """Brute-force cosine KNN over stored BLOB vectors — one row per tool.

        A long schema's chunks are scored by their closest chunk, aggregated
        back to one row per tool.  Vectors whose width differs from *vec*
        are skipped (stale rows from a previous embedding model).
        """
        limit = _clamp_limit(limit)
        filter_clauses, params = column_filters(filters)
        clauses = "".join(f" AND {c}" for c in filter_clauses)
        cursor = await self._c.execute(
            f"""SELECT {_SCAN_COLS}, te.embedding
               FROM tool_embeddings te
               JOIN tool t ON te.name = t.name
               WHERE 1=1{clauses}""",
            params,
        )
        best: dict[str, dict] = {}
        for row in await cursor.fetchall():
            r = dict(row)
            stored = _deserialize_f32(r.pop("embedding"))
            if len(stored) != len(vec):
                continue
            r["distance"] = _cosine_distance(vec, stored)
            cur = best.get(r["name"])
            if cur is None or r["distance"] < cur["distance"]:
                best[r["name"]] = r
        results = sorted(best.values(), key=lambda x: x["distance"])[:limit]
        logger.debug("catalog_search_semantic hits=%s", len(results))
        return results

    # ── Embedding drainer contract (one document = one tool) ───────

    # A row is "embeddable" when it carries a schema to flatten.  ``skill`` /
    # ``cli`` rows have none — they can never be embedded, so they must be
    # excluded IN SQL, not after the fetch: filtering in Python let ``LIMIT``
    # pick a batch of exactly those rows, return no docs while
    # ``count_unembedded()`` still said 1, and the drainer burnt its
    # no-progress bound and gave up — leaving the semantic gate closed with
    # every other tool embedded.  (memdb/memfiles carry the same predicate for
    # the same reason.)  ``_embeddable`` is its Python face, for the one caller
    # that has a row in hand rather than a query.
    _EMBEDDABLE_SCHEMA = f"trim(schema) NOT IN ('', '{NA}')"

    async def count_unembedded(self) -> int:
        """Tools with no embedding chunk and an embeddable (non-empty) schema."""
        cursor = await self._c.execute(
            f"""SELECT COUNT(*) FROM tool
                WHERE name NOT IN (SELECT DISTINCT name FROM tool_embeddings)
                  AND ({self._EMBEDDABLE_SCHEMA})"""
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_unembedded_docs(self, limit: int = 100) -> list[dict]:
        """Drainer docs: ``doc_id`` = tool ``name``, ``text`` = flattened schema.

        Selects exactly the rows :meth:`count_unembedded` counts, so a
        non-zero count always yields at least one doc (``LIMIT`` truncates the
        batch, it never starves it).
        """
        cursor = await self._c.execute(
            f"""SELECT name, schema FROM tool
                WHERE name NOT IN (SELECT DISTINCT name FROM tool_embeddings)
                  AND ({self._EMBEDDABLE_SCHEMA})
                ORDER BY name
                LIMIT ?""",
            (limit,),
        )
        return [
            {"doc_id": name, "text": _flatten_schema(schema or "")}
            for name, schema in await cursor.fetchall()
        ]

    async def replace_embedding_chunks(
        self, doc: dict, embeddings: list[list[float]],
    ) -> None:
        """Atomically replace one tool's embedding chunks (delete+insert, one tx).

        The row carries no model tag: which model built the vectors is
        ``meta('embedding_model')``, one fact for the whole index.
        """
        name = doc["doc_id"]
        vec_blobs = [_serialize_f32(emb) for emb in embeddings]
        async with self._write_lock:
            try:
                await self._c.execute(
                    "DELETE FROM tool_embeddings WHERE name = ?", (name,),
                )
                for idx, blob in enumerate(vec_blobs):
                    await self._c.execute(
                        """INSERT INTO tool_embeddings(name, chunk_index, embedding)
                           VALUES (?, ?, ?)""",
                        (name, idx, blob),
                    )
                await self._c.commit()
            except Exception:
                await self._c.rollback()
                raise
        logger.debug("catalog_embedding_chunks_replaced name=%s chunks=%d", name, len(vec_blobs))

    async def drop_embeddings(self) -> int:
        """Delete all embedding rows. Returns chunk rows deleted."""
        async with self._write_lock:
            cursor = await self._c.execute("SELECT COUNT(*) FROM tool_embeddings")
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await self._c.execute("DELETE FROM tool_embeddings")
            await self._c.commit()
        logger.info("catalog_embeddings_dropped count=%d", count)
        return count

    # ── Meta ───────────────────────────────────────────────────────

    async def get_meta(self, key: str) -> str | None:
        cursor = await self._c.execute(
            "SELECT value FROM meta WHERE key = ?", (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )
            await self._c.commit()