-- ═══════════════════════════════════════════════════════════════
--  Slife memfiles — 笔记/日记/文件/定时报告 的知识库
--
--  四种文档，各自独立成表：
--    notes — 按 subject 为唯一键的笔记（内容双写 notes/<slug>.md）
--    diary — 按 date 为唯一键的日记（内容双写 diary/<YYYY-MM-DD>.md）
--    files — 保存的附件（二进制在文件系统；summary 由 LLM 写，供语义检索）
--    reports — 定时任务生成的报告（内容双写 reports/<slug>.md）
--
--  每类文档各带 FTS5（关键词）与 vec0（语义）索引，内容经触发器同步。
--  与 memdb 完全分开的 DB（{agent}.files/.index.db）。
--
--  定时任务注册表（scheduled_tasks / scheduled_runs）也在此 DB——
--  与报告同生命周期。
-- ═══════════════════════════════════════════════════════════════


CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY,
    subject      TEXT NOT NULL UNIQUE,   -- 键
    content      TEXT NOT NULL,          -- 完整 md 内容（= notes/<subject>.md 内容）
    summary      TEXT DEFAULT '',        -- LLM 摘要（参与向量与关键词检索）
    tags         TEXT DEFAULT '',
    file_path    TEXT NOT NULL,          -- notes/<slug>.md
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diary (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL UNIQUE,   -- 'YYYY-MM-DD'，键
    content      TEXT NOT NULL,          -- 完整 md 内容（= diary/<date>.md 内容）
    summary      TEXT DEFAULT '',        -- LLM 摘要（参与向量与关键词检索）
    tags         TEXT DEFAULT '',
    file_path    TEXT NOT NULL,          -- diary/<date>.md
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id             INTEGER PRIMARY KEY,
    title          TEXT DEFAULT '',
    original_path  TEXT DEFAULT '',      -- 来源路径（拷贝/URL）
    saved_path     TEXT NOT NULL,        -- 相对路径（文件系统实际存储）
    mime           TEXT DEFAULT '',
    size           INTEGER DEFAULT 0,
    tags           TEXT DEFAULT '',
    summary        TEXT DEFAULT '',      -- LLM 写的文件摘要 → 语义检索的文本来源
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER,                -- 归属定时任务 → scheduled_tasks.id；NULL = 独立报告（不绑定任务）。
                                        -- 注意：老库（task_id 可空化之前创建）保持 NOT NULL，不做 ALTER 迁移；
                                        -- report_save 对独立报告会先查 PRAGMA，老库上明确报错。
    title        TEXT DEFAULT '',
    content      TEXT NOT NULL,          -- 完整 md 内容（= reports/<slug>.md 内容）
    summary      TEXT DEFAULT '',        -- LLM 摘要（参与向量与关键词检索）
    tags         TEXT DEFAULT '',
    file_path    TEXT NOT NULL,          -- reports/<slug>.md
    period_start TEXT,                   -- 报告覆盖时间范围起点（ISO）
    period_end   TEXT,                   -- 报告覆盖时间范围终点（ISO）
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);


-- ── 定时任务注册表 ──────────────────────────────────────────────
-- 任务定义（schedule 表达式存 DB → 运行时可变，对话里可加任务）
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,    -- 'daily_diary'（worker 身份 / log 名）
    description TEXT DEFAULT '',         -- 任务描述（worker 的任务文本）
    schedule    TEXT NOT NULL,           -- cron 5 字段表达式（或 'manual'）
    timezone    TEXT DEFAULT '',         -- 触发时区（空 = 本地时区）
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 每次 run 的状态 + 报告索引
CREATE TABLE IF NOT EXISTS scheduled_runs (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL,         -- → scheduled_tasks.id
    due_at     TEXT NOT NULL,            -- 计划触发时间（ISO）
    status     TEXT NOT NULL,            -- 'pending' 已派发未确认 | 'ran' 成功(有报告) | 'failed' 未完成 | 'missed' 停机错过 | 'skipped' 用户跳过不补做
    ran_at     TEXT,                     -- 实际执行时间（ISO）
    report_id  INTEGER,                  -- 产出报告 → reports.id（store 层反填）
    error      TEXT DEFAULT '',          -- 失败/跳过原因
    UNIQUE(task_id, due_at)
);

CREATE INDEX IF NOT EXISTS idx_reports_task ON reports(task_id);


-- ── 关键词搜索：整个 cabinet 一份索引 ──────────────────────────
-- ONE index for the whole cabinet, the way memdb has ONE diary_fts — because
-- a fusion consumes RANKS, and a rank only means something inside the corpus
-- that produced it (an FTS5 rank is bm25, a per-table score).  Four per-kind
-- indexes made four corpora, so a query spanning them had to answer with an
-- order nothing had measured.
--
-- Each kind's searchable columns normalize into the same four:
--   title  — what the row is called
--   body   — its text.  A file has none of its own, so its body is what it IS
--            — title, source path, saved path — plus the summary once one is
--            written.  The summary alone would not do: it is empty by default,
--            so a file would have no text to search or embed until a model
--            described it.
--   tags   — comma-separated tags
--   source — where it came from (a file's original_path; '' for the rest)
-- (kind, doc_id) ride along UNINDEXED so a hit names its own row and every
-- detail column is one join away (see the cabinet_docs view below).
--
-- The file kind's ``body`` expression appears here AND in the view — the
-- trigger writes the index's copy, the view reads the table's, and the two
-- must be the same text or the keyword leg would search something the semantic
-- leg does not embed.  SQL has nowhere to share it, so both spell it out.
--
-- The rowid is the KIND'S OFFSET plus the row id — note 1e12,
-- diary 2e12, file 3e12, report 4e12 (ids never
-- reach 1e12).  A deterministic key makes a delete an O(1) rowid hit: this
-- table is NOT external-content (FTS5 cannot be, over four tables), so it
-- keeps its own copy of the text, and finding one row by scanning for its
-- (kind, doc_id) would read every stored document back.
CREATE VIRTUAL TABLE IF NOT EXISTS cabinet_fts USING fts5(
    kind UNINDEXED, doc_id UNINDEXED, title, body, tags, source, summary
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (1000000000000 + new.id, 'note', new.id, new.subject, new.content, new.tags, '', new.summary);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 1000000000000 + old.id;
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 1000000000000 + old.id;
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (1000000000000 + new.id, 'note', new.id, new.subject, new.content, new.tags, '', new.summary);
END;

CREATE TRIGGER IF NOT EXISTS diary_ai AFTER INSERT ON diary BEGIN
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (2000000000000 + new.id, 'diary', new.id, new.date, new.content, new.tags, '', new.summary);
END;

CREATE TRIGGER IF NOT EXISTS diary_ad AFTER DELETE ON diary BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 2000000000000 + old.id;
END;

CREATE TRIGGER IF NOT EXISTS diary_au AFTER UPDATE ON diary BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 2000000000000 + old.id;
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (2000000000000 + new.id, 'diary', new.id, new.date, new.content, new.tags, '', new.summary);
END;

CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (3000000000000 + new.id, 'file', new.id, new.title,
            TRIM(new.title || ' ' || new.original_path || ' ' || new.saved_path || ' ' || new.summary),
            new.tags, new.original_path, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 3000000000000 + old.id;
END;

CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 3000000000000 + old.id;
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (3000000000000 + new.id, 'file', new.id, new.title,
            TRIM(new.title || ' ' || new.original_path || ' ' || new.saved_path || ' ' || new.summary),
            new.tags, new.original_path, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS reports_ai AFTER INSERT ON reports BEGIN
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (4000000000000 + new.id, 'report', new.id, new.title, new.content, new.tags, '', new.summary);
END;

CREATE TRIGGER IF NOT EXISTS reports_ad AFTER DELETE ON reports BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 4000000000000 + old.id;
END;

CREATE TRIGGER IF NOT EXISTS reports_au AFTER UPDATE ON reports BEGIN
    DELETE FROM cabinet_fts WHERE rowid = 4000000000000 + old.id;
    INSERT INTO cabinet_fts(rowid, kind, doc_id, title, body, tags, source, summary)
    VALUES (4000000000000 + new.id, 'report', new.id, new.title, new.content, new.tags, '', new.summary);
END;


-- ── The cabinet as ONE corpus ──────────────────────────────────
-- What the three search legs read.  A view rather than a table: the four kind
-- tables stay the writers' truth (each keeps its own columns), and this is the
-- one place their differences are normalized away.
--
--   ts  — the kind's own time axis (declared per kind in _KIND_SPECS), read at
--         a uniform DATETIME precision so ONE bound narrows every kind the same
--         way; a date-granular kind is read at the start of its day, which is
--         what a date means.
--   key — the kind's lookup key, which every read tool takes (a note's
--         subject, a diary's date, a file's path, a report's title).
--   id  — "note:5".  The fusion's key: unique across kinds, where a bare
--         doc_id is not (every kind counts from 1).
--   summary — the LLM's abstract of the row, empty until one is written.
--
-- **Which leg reads which column** is the cabinet's own rule, and it is not
-- memdb's.  memdb searches a conversation, where the "title" and the "path" a
-- row has are not separate things; a cabinet row has a name and a place on
-- disk as well as a text, and they are worth searching:
--
--   grep     title, body, file_path, source      (not the summary)
--   keyword  the four above, plus tags and summary
--   semantic body only
--
-- So an identity is reachable by pattern and by word, a meaning by meaning,
-- and a summary — the one column a model wrote rather than a document — is
-- reachable by word alone, which is what a summary is for.
--
CREATE VIEW IF NOT EXISTS cabinet_docs AS
    SELECT 'note' AS kind, id AS doc_id, subject AS key, subject AS title,
           content AS body, tags, '' AS source, file_path, updated_at AS ts,
           'note' || ':' || id AS id, summary
      FROM notes
    UNION ALL
    SELECT 'diary', id, date, date, content, tags, '', file_path,
           date || ' 00:00:00', 'diary' || ':' || id, summary
      FROM diary
    UNION ALL
    SELECT 'file', id, saved_path, title,
           TRIM(title || ' ' || original_path || ' ' || saved_path || ' ' || summary),
           tags, original_path, saved_path, created_at, 'file' || ':' || id, summary
      FROM files
    UNION ALL
    SELECT 'report', id, title, title, content, tags, '', file_path,
           created_at, 'report' || ':' || id, summary
      FROM reports;


-- ── 语义搜索：整个 cabinet 一份索引 ────────────────────────────
-- ONE vec0 table for the cabinet, for the same reason as the FTS5 one: the
-- semantic leg ranks by distance, and the distance is comparable across kinds
-- only because ONE model made every vector.  A table per kind neither
-- prevented nor expressed that.
-- 一个文档 → 一个或多个 chunk（长文按段落切分）。
-- (kind, doc_id) 标识文档，chunk_index 从 0 起。
-- distance_metric=cosine — see the memdb schema for why the metric (not the
-- formula) is what makes a raw distance readable as a 0–1 similarity.
CREATE VIRTUAL TABLE IF NOT EXISTS cabinet_semantic USING vec0(
    doc_embedding float[1536] distance_metric=cosine,
    +kind         TEXT,
    +doc_id       INTEGER,
    +chunk_index  INTEGER,
    +summary      TEXT,
    +tags         TEXT,
    +created_at   TEXT
);


-- ── 元数据 ────────────────────────────────────────────────────
-- 记录产生向量的 embedding 模型身份（迁移检测）。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
