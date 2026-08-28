-- ═══════════════════════════════════════════════════════════════
--  mcp-plugin 工具目录库
--
--  一行 = 一个外部 MCP 工具（以 ``{server}__{tool}`` 全名标识）。
--  持久化工具名、描述与启用状态；重启后目录仍在。
--  支持关键词（FTS5）+ 语义（BLOB 向量，Python 余弦）混合检索。
-- ═══════════════════════════════════════════════════════════════


CREATE TABLE IF NOT EXISTS tools (
    full_name    TEXT PRIMARY KEY,            -- "{server}__{tool}"
    server       TEXT NOT NULL,
    name         TEXT NOT NULL,               -- 外部 MCP 原样工具名
    description  TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,  -- 单工具启用/禁用（0/1）
    last_seen    TEXT NOT NULL,               -- ISO-8601，最近一次 sync_server 见到
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tools_server ON tools(server);


-- ── 关键词搜索 ────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
    full_name,
    server,
    name,
    description,
    content='tools',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS tools_ai AFTER INSERT ON tools BEGIN
    INSERT INTO tools_fts(rowid, full_name, server, name, description)
    VALUES (new.rowid, new.full_name, new.server, new.name, new.description);
END;

CREATE TRIGGER IF NOT EXISTS tools_ad AFTER DELETE ON tools BEGIN
    INSERT INTO tools_fts(tools_fts, rowid, full_name, server, name, description)
    VALUES ('delete', old.rowid, old.full_name, old.server, old.name, old.description);
END;

-- sync_server 更新 description / name —— 外链表必须跟踪 UPDATE，否则
-- 改过的描述对关键词检索不可见。
CREATE TRIGGER IF NOT EXISTS tools_au AFTER UPDATE ON tools BEGIN
    INSERT INTO tools_fts(tools_fts, rowid, full_name, server, name, description)
    VALUES ('delete', old.rowid, old.full_name, old.server, old.name, old.description);
    INSERT INTO tools_fts(rowid, full_name, server, name, description)
    VALUES (new.rowid, new.full_name, new.server, new.name, new.description);
END;


-- ── 语义搜索 ──────────────────────────────────────────────────
-- 一个工具一条向量（name + description），不切块。
-- 向量以 f32 BLOB 存储（struct.pack），检索时 Python 余弦。
CREATE TABLE IF NOT EXISTS tool_embeddings (
    full_name TEXT PRIMARY KEY REFERENCES tools(full_name) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    model     TEXT NOT NULL                    -- 产生该向量的模型 id
);


-- ── 元数据 ────────────────────────────────────────────────────
-- 记录哪个 embedding 模型产生了 tool_embeddings 向量。换模型（即使同维）
-- 也要丢弃旧向量——它们在不同的向量空间里。
CREATE TABLE IF NOT EXISTS tools_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
