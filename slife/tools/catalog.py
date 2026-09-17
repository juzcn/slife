"""Unified tool catalog store — the single shared ``tools.db``.

The authoritative catalog for all six tool categories
(builtin/job/mcp/rest-api/skill/cli), in three kinds of row — ``type`` is
``func`` / ``skill`` / ``cli``.  Lives on disk (data dir, WAL) and is opened
by every agent process (main agent + subagent workers) at the same path;
SQLite gives concurrent readers + one writer, ``busy_timeout`` backs off the
short load/unload writes.  Ops are deliberately store-shaped (upserts,
search, LRU, embed drainer contract) — policy lives in
:mod:`slife.tools.catalog_service`.

State model (see DESIGNER_NOTES §8.5): ``tool.status`` holds
``loaded | unloaded | error | NULL`` (NULL for skill/cli — no load concept),
and everything the injection gate needs is ON THE ROW.  ``error`` is the
connectivity verdict: whenever an external server is unusable — at startup
before it connects, on a disconnect, on a failed connect, when its gateway
child dies — the host marks that server's tools ``error``, and a successful
(re)connect resets them to their class default.  There is deliberately no
``server`` table: which servers to bring up lives in ``tools.json5``, what is
live right now lives in the gateway's pool, and this db only records the
result on the tool rows.  Writes: status flips (load/unload) and those
connectivity marks; eviction is the main agent's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import struct
from pathlib import Path
from typing import Any

import aiosqlite

from slife.plugins.memdb.store import (
    _clamp_limit,
    _contains_cjk,
    _like_escape,
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
#: carries the connectivity verdict (``error`` when its server is down).
#:
#: ``plugin`` is deliberately NOT here even though those rows also carry a
#: ``source_id`` (the plugin that owns them): a plugin is not an *external*
#: server, so the gateway's death must not mark its tools unusable
#: (``mark_all_external_error``), its rows keep a config-mirrored ``enabled``,
#: and it stays out of the "servers" count and the unconfigured-source purge.
SERVER_CATEGORIES = frozenset({"mcp", "rest-api"})

#: Function-tool categories — the only ones with a load/unload status.
#: ``plugin`` = a built-in plugin's own tool; ``job`` = one function from the
#: jobs directory (exposed by the job-coding plugin, whose OWN tools are
#: ``plugin`` — see ``catalog_service.plugin_category``).
FUNCTION_CATEGORIES = frozenset({"builtin", "job", "plugin", "mcp", "rest-api"})

#: The row's ``type`` — the coarse kind behind ``category``: a function tool
#: (loadable: builtin / job / mcp / rest-api), a skill, or a cli entry.
TYPE_FUNC = "func"
TYPE_SKILL = "skill"
TYPE_CLI = "cli"

_TYPE_BY_CATEGORY = {
    **{c: TYPE_FUNC for c in FUNCTION_CATEGORIES},
    "skill": TYPE_SKILL,
    "cli": TYPE_CLI,
}


def type_for_category(category: str) -> str:
    """The ``type`` a category implies — the one derivation both writers use.

    ``category`` says where a tool came from (a builtin module, a job file, an
    external server, a skill dir, a cli config entry); ``type`` says what kind
    of thing it is, which is what decides whether ``status`` applies at all.
    """
    return _TYPE_BY_CATEGORY[category]


# The stored status values for function tools.  ONLY the load state lives here
# — the model's decision, and the one thing the db exists to persist
# (``loaded`` / ``unloaded``).  The connectivity verdict is a SEPARATE column
# (``unavailable``): writing it into ``status`` destroyed the load state it
# landed on, so a server blip — or the startup sweep — reset every external
# tool the model had loaded.
STATUS_LOADED = "loaded"
STATUS_UNLOADED = "unloaded"

# Effective status labels (derived, never stored).
EFF_DISABLED = "disabled"
EFF_ERROR = "error"
EFF_NA = "n/a"

#: Schema revision this store expects (``catalog_schema.sql`` sets it; the
#: migration in :meth:`CatalogStore._migrate` moves an older file up to it).
#: Deliberately NOT bumped for the ``unavailable`` column: that revision has no
#: migration step — the file is derived data, so a stale one is reported and
#: rebuilt (``_check_columns``), never upgraded in place.
SCHEMA_VERSION = 4

#: The category values the code can write — the set the live table's ``CHECK``
#: must accept (checked at open, see :meth:`CatalogStore._check_categories`).
ALL_CATEGORIES = FUNCTION_CATEGORIES | {"skill", "cli"}

#: Local ISO-seconds timestamp — the shared store convention.
_now = now_local_seconds


#: The columns the code reads or writes on ``tool`` — checked at open against
#: the live table (:meth:`CatalogStore._check_columns`).
_REQUIRED_COLUMNS = frozenset({
    "name", "description", "category", "type", "source_id", "schema",
    "enabled", "status", "unavailable", "last_loaded",
})

#: The ``tool`` table's category constraint, parsed rather than substring-matched.
_CATEGORY_CHECK_RE = re.compile(
    r"check\s*\(\s*category\s+in\s*\(([^)]*)\)", re.IGNORECASE,
)


def _category_check_values(ddl: str) -> "set[str] | None":
    """The category values a ``tool`` DDL accepts; ``None`` when it has no CHECK.

    Scoped to the ``category`` clause on purpose: the ``type`` CHECK lists
    ``'skill'`` and ``'cli'`` too, so a substring test over the whole statement
    would report those two as present even in a table whose category list has
    never heard of them.
    """
    m = _CATEGORY_CHECK_RE.search(ddl or "")
    if m is None:
        return None
    return {v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()}


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

def _compact_schema(value: Any) -> str:
    """Compact JSON text of a tool's descriptor (dict/list or JSON str)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return ""


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
#: from, in SQL: **a func tool that is enabled and loaded**.
#:
#: - ``type = 'func'`` — skills and cli entries are never injected (their
#:   ``status`` is NULL, so this only makes the rule explicit).
#: - ``enabled`` — tools.json5's answer.  NULL is treated as enabled: the column
#:   is "not known to be off", and a row whose flag was never written must not
#:   silently vanish from the model's tool list.
#: - ``status = 'loaded'`` — the model's decision, the db's whole reason to exist.
#: - ``unavailable`` — the runtime verdict, its own column (never a status):
#:   a tool whose server is down leaves the injected set while the flag is up
#:   and comes back with the load state it had.
#:
#: It is ``_effective_status``'s rule and MUST move with it.
_INJECTABLE_SQL = (
    "type = 'func'"
    " AND status = 'loaded'"
    " AND (enabled IS NULL OR enabled = 1)"
    " AND (unavailable IS NULL OR unavailable = 0)"
)


def _effective_status(trow: dict) -> str:
    """Derived effective status (never stored): disabled / error / loaded /
    unloaded / n/a.

    Everything it needs is ON THE ROW, and in a deliberate order:

    1. ``enabled == 0`` → ``disabled`` — the json5 switch, for every category
       (a server switched off is distinguishable from one that is merely
       down).
    2. ``unavailable`` → ``error`` — the runtime verdict that the owner (a
       server, a plugin) is not usable right now.
    3. the row's own ``status`` — what the model decided.

    Neither fact above overwrites the one below it, which is what lets a
    ``loaded`` tool survive a blip and come back ``loaded``.
    """
    if trow.get("enabled") == 0:
        return EFF_DISABLED
    if trow.get("unavailable"):
        return EFF_ERROR
    status = trow.get("status")
    return status if status else EFF_NA


def effective_from_row(row: dict) -> str:
    """Effective status for a search/scan row (row-only — no server aliases)."""
    return _effective_status(row)


# Row prefix used by the scan/search queries.
_SCAN_COLS = (
    "t.name, t.description, t.category, t.type, t.source_id, t.schema, "
    "t.enabled, t.status, t.last_loaded, t.unavailable"
)
# The four-column substring predicate shared by _search_like / search_grep
# (4 ``?`` placeholders per column ANDed into name/description/category/schema).
_LIKE_WHERE = (
    "(t.name LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'"
    " OR t.category LIKE ? ESCAPE '\\' OR t.schema LIKE ? ESCAPE '\\')"
)
# Keyword-result spine: scan columns + a description snippet anchored on the
# first search term + a zero rank.  Keyword searches order by category/name;
# rank only matters to the hybrid semantic merge.
_SEARCH_SELECT = (
    f"SELECT {_SCAN_COLS},"
    " substr(t.description, max(0, instr(t.description, ?) - 40), 160) AS snippet,"
    " 0 AS rank"
    " FROM tool t"
)


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

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None
        return self._conn

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
        from the registry, ``tools.json5``, the skills dir and the plugin
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
                 f"rebuilt from the tool registry, tools.json5 and the plugins, "
                 f"so nothing is lost but the loaded/unloaded state.",
        )

    async def _check_columns(self) -> None:
        """Verify the live ``tool`` table has every column this code uses.

        Same doctrine as :meth:`_check_categories`, same reason to verify the
        DDL rather than ``user_version``: ``CREATE TABLE IF NOT EXISTS`` never
        touches an existing file, and these revisions have no migration step —
        a stale catalog is DELETED and rebuilt.  Without this check an older
        file fails every scan with ``no such column: unavailable``, which reads
        as a code bug instead of "delete the derived file".
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
        if not missing:
            return
        logger.error(
            "catalog_schema_stale path=%s missing_columns=%s action=delete_the_file",
            self._path, ",".join(missing),
        )
        from slife.health import record
        record(
            "tool_catalog", "warning", key="schema",
            value=f"stale (no {'/'.join(missing)} column)",
            hint=f"Delete {self._path} and restart slife. The catalog is "
                 f"rebuilt from the tool registry, tools.json5 and the plugins, "
                 f"so nothing is lost but the loaded/unloaded state.",
        )

    async def _migrate(self) -> None:
        """Bring an older db up to ``SCHEMA_VERSION`` before the schema runs.

        v2 dropped the ``server`` table: connection state lives on the tool
        rows now (``status='error'`` when a server is unusable), so the
        table is dead weight AND a stale source of truth.  ``CREATE TABLE IF
        NOT EXISTS`` would leave it in place forever — the only place a
        retired table can be removed is a migration step like this one.

        v3 added ``tool.type`` (func | skill | cli).  A fresh file gets it
        from the schema; an existing one is ALTERed and backfilled from
        ``category`` here, since the schema's ``IF NOT EXISTS`` never touches
        a table that is already there.

        v4 added the ``plugin`` value to the ``category`` CHECK and has NO
        step here on purpose: a wider CHECK needs the table rebuilt, and a
        stale catalog is deleted and rebuilt from its sources instead of
        upgraded in place.  :meth:`_check_categories` reports such a file
        rather than letting its writes fail silently.
        """
        cursor = await self._c.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = row[0] if row else 0
        if version >= SCHEMA_VERSION:
            return
        if version < 2:
            await self._c.execute("DROP TABLE IF EXISTS server")
            logger.info("catalog_migrated from=%s to=2 dropped=server", version)
        if version < 3:
            cursor = await self._c.execute("PRAGMA table_info(tool)")
            columns = {r[1] for r in await cursor.fetchall()}
            # No `tool` table yet (a brand-new file) → the schema creates it
            # with the column already in place.
            if columns and "type" not in columns:
                await self._c.execute(
                    "ALTER TABLE tool ADD COLUMN type TEXT NOT NULL DEFAULT 'func' "
                    "CHECK (type IN ('func','skill','cli'))",
                )
                for category, type_name in _TYPE_BY_CATEGORY.items():
                    await self._c.execute(
                        "UPDATE tool SET type = ? WHERE category = ?",
                        (type_name, category),
                    )
                await self._c.commit()
                logger.info("catalog_migrated from=%s to=3 added=type", version)

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

        Each entry carries ``name`` plus the written columns (``description``,
        ``category``, ``source_id``, ``schema``, ``enabled``).  ``enabled`` is
        tri-state: ``None`` means *no opinion* and leaves the column alone —
        the contract mcp/rest-api rows rely on to keep it NULL.  ``status``
        applies to a NEW row only: an existing row keeps its loaded/unloaded
        state, so a plugin re-register can never clobber a user unload.
        ``type`` is derived from ``category`` here — the one place it is
        written, so the two columns cannot drift.

        Returns ``{inserted, updated, skipped, purged, schema_changed}``.  The
        first four are name lists (``skipped`` is a count); ``schema_changed``
        names the rows whose embedding was invalidated, for the caller to wake
        the drainer.
        """
        async with self._write_lock:
            where = " WHERE t.category = ?" if category else ""
            cursor = await self._c.execute(
                f"SELECT {_SCAN_COLS} FROM tool t{where}",
                (category,) if category else (),
            )
            existing = {r["name"]: dict(r) for r in await cursor.fetchall()}

            inserted: list[str] = []
            updated: list[str] = []
            invalidated: list[str] = []
            incoming: set[str] = set()
            skipped = 0

            for row in rows:
                name = row.get("name") or ""
                if not name:
                    continue
                incoming.add(name)
                prev = existing.get(name)
                new_type = type_for_category(row.get("category", "") or "")
                new_schema = row.get("schema")
                new_enabled = row.get("enabled")

                if prev is None:
                    await self._c.execute(
                        """INSERT INTO tool(name, description, category, type,
                                            source_id, schema, enabled, status,
                                            last_loaded)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            name, row.get("description") or "",
                            row.get("category", "") or "", new_type,
                            row.get("source_id"), new_schema,
                            (1 if new_enabled else 0) if new_enabled is not None else None,
                            row.get("status"), None,
                        ),
                    )
                    inserted.append(name)
                    if _flatten_schema(new_schema or ""):
                        invalidated.append(name)
                    continue

                # Build the SET list from the columns that actually moved, so
                # an enabled-only flip does not rewrite a multi-KB schema blob.
                #
                # Two different questions, two different comparators:
                # ``row_dirty`` — the raw text moved, so the row (and the FTS
                # index the update trigger feeds) must be rewritten.
                # ``embed_stale`` — the text that actually gets EMBEDDED moved.
                # The drainer embeds ``_flatten_schema(schema)``, which drops
                # enum/default/pattern/format and nesting past one level, so a
                # change only in a dropped field would otherwise delete the
                # vectors and re-embed to a byte-identical vector.
                schema_moved = (prev["schema"] or "") != (new_schema or "")
                # ``embed_stale`` implies ``schema_moved`` (the flattener is a
                # pure function of the text), so short-circuit: an unchanged
                # row — every row on a steady-state boot — costs one string
                # compare and never parses a schema.
                embed_stale = schema_moved and (
                    _flatten_schema(prev["schema"] or "")
                    != _flatten_schema(new_schema or "")
                )
                sets: list[str] = []
                vals: list = []
                if (prev["description"] or "") != (row.get("description") or ""):
                    sets.append("description = ?")
                    vals.append(row.get("description") or "")
                if prev["category"] != (row.get("category", "") or ""):
                    sets.append("category = ?")
                    vals.append(row.get("category", "") or "")
                if prev["type"] != new_type:
                    sets.append("type = ?")
                    vals.append(new_type)
                if (prev["source_id"] or "") != (row.get("source_id") or ""):
                    sets.append("source_id = ?")
                    vals.append(row.get("source_id"))
                if schema_moved:
                    sets.append("schema = ?")
                    vals.append(new_schema)
                # An explicit value differs from NULL too — the old COALESCE
                # wrote 0/1 over a NULL column, and that must keep happening.
                if new_enabled is not None and (
                    prev["enabled"] is None or bool(prev["enabled"]) != bool(new_enabled)
                ):
                    sets.append("enabled = ?")
                    vals.append(1 if new_enabled else 0)

                if not sets:
                    skipped += 1
                    continue
                vals.append(name)
                await self._c.execute(
                    f"UPDATE tool SET {', '.join(sets)} WHERE name = ?", vals,
                )
                updated.append(name)
                if embed_stale:
                    invalidated.append(name)

            purged: list[str] = []
            if purge and category:
                gone = sorted(n for n in existing if n not in incoming)
                if gone:
                    ph = in_placeholders(len(gone))
                    await self._c.execute(
                        f"DELETE FROM tool WHERE name IN ({ph})", gone,
                    )
                    # Same explicit cleanup as remove_tool — the purge must
                    # not depend on the FK cascade being enabled.
                    await self._c.execute(
                        f"DELETE FROM tool_embeddings WHERE name IN ({ph})", gone,
                    )
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

        logger.info(
            "catalog_reconcile category=%s inserted=%d updated=%d skipped=%d purged=%d",
            category or "(all)", len(inserted), len(updated), skipped, len(purged),
        )
        return {
            "inserted": inserted,
            "updated": updated,
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
        enabled: bool | None = None,
        status: str | None = None,
    ) -> bool:
        """Upsert one tool row; returns True iff its ``schema`` text changed.

        The single-row face of :meth:`reconcile`, so the comparison that
        decides "re-embed or not" has exactly one implementation.  See that
        method for the column contracts (``enabled=None`` = no opinion,
        ``status`` on a NEW row only).
        """
        result = await self.reconcile([{
            "name": name, "description": description, "category": category,
            "source_id": source_id, "schema": schema, "enabled": enabled,
            "status": status,
        }])
        return name in result["schema_changed"]

    async def purge_source(self, source_id: str) -> int:
        """Delete every tool row owned by one external server.

        The removal path (config removal, or a server disabled in
        ``tools.json5``): a server that is off owns no rows — its tools are
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
        logger.info("catalog_source_purged source=%s tools=%d", source_id, count)
        return count

    async def purge_source_except(self, source_id: str, keep: "set[str]") -> list[str]:
        """Delete one owner's rows for every name NOT in *keep*.

        The per-tool half of :meth:`purge_source`: the owner is still
        configured and still reachable, it simply stopped publishing one of
        its tools.  Without this the vanished tool keeps its row, so
        ``tool_search`` goes on offering it and ``func-tool-load`` materializes
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
                ph = in_placeholders(len(gone))
                await self._c.execute(
                    f"DELETE FROM tool WHERE name IN ({ph})", gone,
                )
                # Same explicit cleanup as purge_source / remove_tool — the
                # delete must not depend on the FK cascade being enabled.
                await self._c.execute(
                    f"DELETE FROM tool_embeddings WHERE name IN ({ph})", gone,
                )
                await self._c.commit()
        if gone:
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
        sql = "SELECT DISTINCT source_id FROM tool WHERE source_id IS NOT NULL"
        params: list = []
        if categories:
            sql += f" AND category IN ({in_placeholders(len(categories))})"
            params = sorted(categories)
        cursor = await self._c.execute(sql, params)
        return {row[0] for row in await cursor.fetchall()}

    async def set_source_enabled(self, source_id: str, enabled: bool) -> int:
        """Set one source's ``enabled`` flag on all of its rows.

        The whole of the sync's business with this column: a server switched
        off in ``tools.json5`` is a row that reports ``disabled``.  Deliberately
        narrow — it writes ``enabled`` and nothing else:

        - ``status`` is NOT touched, so the model's loaded/unloaded decision
          survives a disable/enable round trip untouched.
        - the rows are NOT deleted, so re-enabling restores a tool set that
          still remembers what was loaded.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET enabled = ? WHERE source_id = ?",
                (1 if enabled else 0, source_id),
            )
            await self._c.commit()
        return cursor.rowcount

    async def mark_source_unavailable(self, source_id: str) -> int:
        """Flag one owner's tools ``unavailable`` — its server/plugin is down.

        The verdict is its own column, NOT a status: the rows keep saying what
        the model decided (``loaded`` / ``unloaded``), and they simply leave
        the injection set while the flag is up — so the decision is still there
        when the owner comes back.  Only rows that can have a load state are
        flagged (the same set the old status write covered).
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET unavailable = 1 "
                "WHERE source_id = ? AND status IS NOT NULL",
                (source_id,),
            )
            await self._c.commit()
        return cursor.rowcount

    async def mark_all_external_unavailable(self) -> int:
        """Flag EVERY external tool ``unavailable`` — the gateway child died.

        All of its servers are unreachable at once, so their tools must leave
        the injection set immediately rather than at the next reconcile.
        """
        async with self._write_lock:
            placeholders = ",".join("?" * len(SERVER_CATEGORIES))
            cursor = await self._c.execute(
                f"UPDATE tool SET unavailable = 1 "
                f"WHERE category IN ({placeholders}) AND status IS NOT NULL",
                (*sorted(SERVER_CATEGORIES),),
            )
            await self._c.commit()
        return cursor.rowcount

    async def clear_source_unavailable(self, source_id: str) -> int:
        """Clear one owner's verdict — it is usable again.

        The only thing a reconnect does to the rows: every tool keeps the
        ``loaded`` / ``unloaded`` it had, which is what makes the load state
        survive a blip, a restart, and a plugin restart alike.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET unavailable = NULL "
                "WHERE source_id = ? AND unavailable IS NOT NULL",
                (source_id,),
            )
            await self._c.commit()
        return cursor.rowcount

    async def remove_tool(self, name: str) -> None:
        """Delete a single tool row plus its embedding chunks.

        Used when a non-server tool disappears at runtime (a job file
        removed, a plugin dropping a tool) — the §8.5 "remove 清理干净"
        contract: a vanished tool must not linger as a stale catalog row
        that tool_search keeps returning.
        """
        async with self._write_lock:
            await self._c.execute("DELETE FROM tool WHERE name = ?", (name,))
            await self._c.execute(
                "DELETE FROM tool_embeddings WHERE name = ?", (name,),
            )
            await self._c.commit()

    async def names_by_category(self, category: str) -> set[str]:
        """Every row name of one category — the mirror's purge diff basis."""
        cursor = await self._c.execute(
            "SELECT name FROM tool WHERE category = ?", (category,),
        )
        return {row[0] for row in await cursor.fetchall()}

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

    async def set_status(self, name: str, status: str | None, *, bump: bool = False) -> int:
        """Set a tool's ``status`` (loaded/unloaded/NULL); bump → LRU refresh.

        Only meaningful for ``type='func'`` rows (skill/cli stay NULL); the
        store is lenient — callers guard with the type.
        """
        last_loaded = _now() if (bump and status == "loaded") else None
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET status = ?, last_loaded = "
                "CASE WHEN ? IS NULL THEN last_loaded ELSE ? END"
                " WHERE name = ?",
                (status, last_loaded, last_loaded, name),
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
                " WHERE name = ? AND status = 'loaded'",
                (_now(), name),
            )
            await self._c.commit()

    async def set_enabled(self, name: str, enabled: bool | None) -> int:
        """Set a local category's ``enabled`` (mcp/rest-api pass None → noop)."""
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET enabled = ? WHERE name = ?",
                (1 if enabled else 0) if enabled is not None else None,
                (name,),
            )
            await self._c.commit()
        return cursor.rowcount

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
        """Injectable tool names: ``status == 'loaded'`` (and not disabled).

        Everything is on the row now.  An external tool whose server is down
        is not ``loaded`` — the reconcile marked it ``error`` — so it drops
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
        evicted names (they now have ``status='unloaded'``).
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
                    WHERE status = 'loaded'{protected_expr}
                    ORDER BY (last_loaded IS NULL) DESC, last_loaded ASC, name
                    LIMIT ?""",
                params,
            )
            names = [row[0] for row in await cursor.fetchall()]
            if names:
                ph = in_placeholders(len(names))
                await self._c.execute(
                    f"UPDATE tool SET status = 'unloaded'"
                    f" WHERE name IN ({ph}) AND status = 'loaded'",
                    names,
                )
            await self._c.commit()
        if names:
            logger.info("catalog_evict count=%d names=%r", len(names), names)
        return names

    # ── Search ─────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20, category: str = "",
    ) -> list[dict]:
        """FTS5 keyword search, CJK-routed to :meth:`_search_like`.

        Cross-category (no server-join gate; effective status is layered on
        by the caller via ``scan_effective``/``eff_map``).
        """
        limit = _clamp_limit(limit)
        if _contains_cjk(query):
            return await self._search_like(query, limit=limit, category=category)
        fts_query = _to_fts5_query(query)
        clauses = ""
        params: list = [fts_query]
        if category:
            clauses += " AND t.category = ?"
            params.append(category)
        params.append(limit)
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
        category: str = "",
    ) -> list[dict]:
        """Substring (LIKE) search — CJK fallback for :meth:`search_keyword`.

        Space-split words all must match (AND semantics) across name /
        description / category / schema; each CJK word matches by substring.
        """
        words = [w for w in pattern.split() if w]
        if not words:
            return []
        and_clauses: list[str] = []
        params: list = [words[0]]  # instr context anchors on the first word
        for w in words:
            safe = _like_escape(w)
            like = f"%{safe}%"
            and_clauses.append(_LIKE_WHERE)
            params.extend([like, like, like, like])
        return await self._search_by_sql(and_clauses, params, category, limit)

    async def search_grep(
        self, pattern: str, limit: int = 20, category: str = "",
    ) -> list[dict]:
        """Exact substring search over the catalog columns."""
        limit = _clamp_limit(limit)
        safe = _like_escape(pattern)
        like_pattern = f"%{safe}%"
        params: list = [
            pattern, like_pattern, like_pattern, like_pattern, like_pattern,
        ]
        return await self._search_by_sql([_LIKE_WHERE], params, category, limit)

    async def _search_by_sql(
        self, clauses: list[str], params: list, category: str, limit: int,
    ) -> list[dict]:
        """Run one keyword ``AND``-clause search against the shared spine.

        Both LIKE flavours build their clauses+params (the first param of
        *params* is the snippet anchor — the first search term), then append
        the optional category filter and the LIMIT here.
        """
        where = " AND ".join(clauses)
        if category:
            where += " AND t.category = ?"
            params.append(category)
        params.append(limit)
        cursor = await self._c.execute(
            f"{_SEARCH_SELECT} WHERE {where} ORDER BY t.category, t.name LIMIT ?",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def search_semantic(
        self, vec: list[float], limit: int = 20, category: str = "",
    ) -> list[dict]:
        """Brute-force cosine KNN over stored BLOB vectors — one row per tool.

        A long schema's chunks are scored by their closest chunk, aggregated
        back to one row per tool.  Vectors whose width differs from *vec*
        are skipped (stale rows from a previous embedding model).
        """
        limit = _clamp_limit(limit)
        clauses = ""
        params: list = []
        if category:
            clauses = " AND t.category = ?"
            params.append(category)
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
    # the same reason.)
    _EMBEDDABLE_SCHEMA = "trim(coalesce(schema, '')) != ''"

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
        self, doc: dict, embeddings: list[list[float]], *, model: str = "",
    ) -> None:
        """Atomically replace one tool's embedding chunks (delete+insert, one tx)."""
        name = doc["doc_id"]
        if not model:
            model = (await self.get_meta("embedding_model")) or ""
        vec_blobs = [_serialize_f32(emb) for emb in embeddings]
        async with self._write_lock:
            try:
                await self._c.execute(
                    "DELETE FROM tool_embeddings WHERE name = ?", (name,),
                )
                for idx, blob in enumerate(vec_blobs):
                    await self._c.execute(
                        """INSERT INTO tool_embeddings(name, chunk_index, embedding, model)
                           VALUES (?, ?, ?, ?)""",
                        (name, idx, blob, model),
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

    async def count_embedded(self) -> int:
        cursor = await self._c.execute(
            "SELECT COUNT(DISTINCT name) FROM tool_embeddings",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

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