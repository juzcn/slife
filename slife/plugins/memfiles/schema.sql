-- ═══════════════════════════════════════════════════════════════
--  Slife memfiles — 笔记/日记/文件 的知识库
--
--  三种文档，各自独立成表：
--    notes — 按 subject 为唯一键的笔记（内容双写 notes/<slug>.md）
--    diary — 按 date 为唯一键的日记（内容双写 diary/<YYYY-MM-DD>.md）
--    files — 保存的附件（二进制在文件系统；summary 由 LLM 写，供语义检索）
--
--  每类文档各带 FTS5（关键词）与 vec0（语义）索引，内容经触发器同步。
--  与 memdb 完全分开的 DB（{agent}.files/.index.db）。
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


-- ── 元数据 ────────────────────────────────────────────────────
-- 记录产生向量的 embedding 模型身份（迁移检测）。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
