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
    tags         TEXT DEFAULT '',
    file_path    TEXT NOT NULL,          -- notes/<slug>.md
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diary (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL UNIQUE,   -- 'YYYY-MM-DD'，键
    content      TEXT NOT NULL,          -- 完整 md 内容（= diary/<date>.md 内容）
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
    task_id      INTEGER NOT NULL,       -- 归属定时任务 → scheduled_tasks.id
    title        TEXT DEFAULT '',
    content      TEXT NOT NULL,          -- 完整 md 内容（= reports/<slug>.md 内容）
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
CREATE INDEX IF NOT EXISTS idx_runs_task_due ON scheduled_runs(task_id, due_at);


-- ── 关键词搜索 ────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    subject, content, tags,
    content='notes', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, subject, content, tags)
    VALUES (new.id, new.subject, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, subject, content, tags)
    VALUES ('delete', old.id, old.subject, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, subject, content, tags)
    VALUES ('delete', old.id, old.subject, old.content, old.tags);
    INSERT INTO notes_fts(rowid, subject, content, tags)
    VALUES (new.id, new.subject, new.content, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS diary_fts USING fts5(
    content, tags,
    content='diary', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS diary_ai AFTER INSERT ON diary BEGIN
    INSERT INTO diary_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS diary_ad AFTER DELETE ON diary BEGIN
    INSERT INTO diary_fts(diary_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS diary_au AFTER UPDATE ON diary BEGIN
    INSERT INTO diary_fts(diary_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO diary_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    title, original_path, tags, summary,
    content='files', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, title, original_path, tags, summary)
    VALUES (new.id, new.title, new.original_path, new.tags, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, title, original_path, tags, summary)
    VALUES ('delete', old.id, old.title, old.original_path, old.tags, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, title, original_path, tags, summary)
    VALUES ('delete', old.id, old.title, old.original_path, old.tags, old.summary);
    INSERT INTO files_fts(rowid, title, original_path, tags, summary)
    VALUES (new.id, new.title, new.original_path, new.tags, new.summary);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts USING fts5(
    title, content, tags,
    content='reports', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS reports_ai AFTER INSERT ON reports BEGIN
    INSERT INTO reports_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS reports_ad AFTER DELETE ON reports BEGIN
    INSERT INTO reports_fts(reports_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS reports_au AFTER UPDATE ON reports BEGIN
    INSERT INTO reports_fts(reports_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO reports_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;


-- ── 语义搜索 ──────────────────────────────────────────────────
-- 一个文档 → 一个或多个 chunk（长文按段落切分）。
-- doc_id 引用 notes.id / diary.id / files.id；chunk_index 0 起。
-- 结构对齐 memdb diary_semantic，方便复用嵌入/替换逻辑。
CREATE VIRTUAL TABLE IF NOT EXISTS notes_semantic USING vec0(
    doc_embedding float[1536],
    +doc_id       INTEGER,
    +chunk_index  INTEGER,
    +summary      TEXT,
    +tags         TEXT,
    +created_at   TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS diary_semantic USING vec0(
    doc_embedding float[1536],
    +doc_id       INTEGER,
    +chunk_index  INTEGER,
    +summary      TEXT,
    +tags         TEXT,
    +created_at   TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS files_semantic USING vec0(
    doc_embedding float[1536],
    +doc_id       INTEGER,
    +chunk_index  INTEGER,
    +summary      TEXT,
    +tags         TEXT,
    +created_at   TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS reports_semantic USING vec0(
    doc_embedding float[1536],
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
