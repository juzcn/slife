-- ═══════════════════════════════════════════════════════════════
--  mcp-plugin 工具目录库
--
--  一行 = 一个外部 MCP 工具（以 ``{server}__{tool}`` 全名标识）。
--  目录完全驻留内存（:memory:），加载时由活动连接重建，不落盘。
--  支持关键词（FTS5）+ 语义（BLOB 向量，Python 余弦）混合检索。
-- ═══════════════════════════════════════════════════════════════


-- 服务级元数据（per-mcp，无 per-tool 状态）：决定大模型可见性。
--   enabled   = 服务启用/禁用（禁用 → 不注册、不进 tool_search、不可用）
--   auto_load = true  → 工具注册到大模型（tool list），tool_search 不可见
--                false → 工具不注册，tool_search 可见，经 tool_load 可用
CREATE TABLE IF NOT EXISTS servers (
    name       TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    auto_load  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tools (
    full_name    TEXT PRIMARY KEY,            -- "{server}__{tool}"
    server       TEXT NOT NULL REFERENCES servers(name) ON DELETE CASCADE,
    name         TEXT NOT NULL,               -- 外部 MCP 原样工具名
    description  TEXT NOT NULL DEFAULT '',
    input_schema TEXT NOT NULL DEFAULT '',    -- 工具 inputSchema 全文（紧凑 JSON，单一文本源）
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
    input_schema,
    content='tools',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS tools_ai AFTER INSERT ON tools BEGIN
    INSERT INTO tools_fts(rowid, full_name, server, name, description, input_schema)
    VALUES (new.rowid, new.full_name, new.server, new.name, new.description, new.input_schema);
END;

CREATE TRIGGER IF NOT EXISTS tools_ad AFTER DELETE ON tools BEGIN
    INSERT INTO tools_fts(tools_fts, rowid, full_name, server, name, description, input_schema)
    VALUES ('delete', old.rowid, old.full_name, old.server, old.name, old.description, old.input_schema);
END;

-- sync_server 更新 description / name / input_schema —— 外链表必须跟踪 UPDATE，
-- 否则改过的描述/参数对关键词检索不可见。
CREATE TRIGGER IF NOT EXISTS tools_au AFTER UPDATE ON tools BEGIN
    INSERT INTO tools_fts(tools_fts, rowid, full_name, server, name, description, input_schema)
    VALUES ('delete', old.rowid, old.full_name, old.server, old.name, old.description, old.input_schema);
    INSERT INTO tools_fts(rowid, full_name, server, name, description, input_schema)
    VALUES (new.rowid, new.full_name, new.server, new.name, new.description, new.input_schema);
END;


-- ── 语义搜索 ──────────────────────────────────────────────────
-- 一个工具一个文档（完整 schema：name + description + 参数 + 返回说明），
-- 长 schema 按嵌入模型 token 上限切块（同 memdb/memfiles），一块一行；
-- 检索时按块取最小距离、每工具聚合成一条。向量以 f32 BLOB 存储
-- （struct.pack），Python 余弦比较。
CREATE TABLE IF NOT EXISTS tool_embeddings (
    full_name   TEXT NOT NULL REFERENCES tools(full_name) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    embedding   BLOB NOT NULL,
    model       TEXT NOT NULL,               -- 产生该向量的模型 id
    PRIMARY KEY (full_name, chunk_index)
);


-- ── 元数据 ────────────────────────────────────────────────────
-- 记录哪个 embedding 模型产生了 tool_embeddings 向量。换模型（即使同维）
-- 也要丢弃旧向量——它们在不同的向量空间里。
CREATE TABLE IF NOT EXISTS tools_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
