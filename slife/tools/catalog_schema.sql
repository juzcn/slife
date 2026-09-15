-- ═══════════════════════════════════════════════════════════════
--  slife 统一工具目录库（tools.db）— host 主进程持有的单一事实源
--
--  一行 = 一个工具（6 类：builtin | job | mcp | rest-api | skill | cli），
--  mcp/rest-api 用 ``{server}__{tool}`` 全名标识（source_id 指 server）。
--  落盘 + WAL（多进程：主 agent 写、subagent 只读/短写），busy_timeout
--  兜底 SQLITE_BUSY。无运行时 DDL 迁移：schema 版本走 PRAGMA user_version，
--  真需要迁移时在 _config_io 的跨进程 filelock 下重建新库。
--
--  tool.status 只存三态 loaded|unloaded|NULL（skill/cli 无 load 概念）；
--  error/disabled 不落库 —— 是查询时 join server 状态导出的 effective。
--  FTS5（关键词）+ BLOB 向量（语义，over schema 文本）混合检索。
-- ═══════════════════════════════════════════════════════════════


-- 工具目录。mcp/rest-api 的 enabled 为 NULL（由 server.enabled join），
-- 其余类别的 enabled 来自各 json5 section。status 会话内可翻转。
CREATE TABLE IF NOT EXISTS tool (
    name        TEXT PRIMARY KEY,            -- mcp: '{server}__{tool}'；否则裸名
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL               -- builtin | job | mcp | rest-api | skill | cli
                CHECK (category IN ('builtin','job','mcp','rest-api','skill','cli')),
    source_id   TEXT,                        -- 仅 mcp/rest-api：所属 server 名
    schema      TEXT,                        -- Tool def JSON | SKILL.md 文本 | NULL(cli)
    enabled     INTEGER,                     -- 0/1（builtin/job/skill/cli）；NULL（mcp/rest-api，join server）
    status      TEXT,                        -- 'loaded'|'unloaded'（function tool）；skill/cli NULL
    last_loaded TEXT                         -- 本地 ISO，LRU evict 排序
);
CREATE INDEX IF NOT EXISTS idx_tool_status ON tool(status);
CREATE INDEX IF NOT EXISTS idx_tool_source ON tool(source_id);
CREATE INDEX IF NOT EXISTS idx_tool_category ON tool(category);


-- 服务级元数据（mcp/rest-api）：enabled 是 tools.json5 的镜像（host 不直接翻转，
-- enable/disable 走 wrapper 的 mcp_set_enabled → 配置持久化 → 重连/reconcile）。
-- runtime 是 wrapper 连接池状态的镜像（重连/backoff 的控制在 wrapper）。
CREATE TABLE IF NOT EXISTS server (
    name         TEXT PRIMARY KEY,
    description  TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    runtime      TEXT,                       -- CONNECTED | CONNECTING | DISCONNECTED | ERROR
    error_reason TEXT,
    last_runtime TEXT,                       -- 上次终态，供重启 eager-connect 集
    source       TEXT                        -- provenance dict JSON (source.type ⇒ rest-api)
);


-- ── 关键词搜索（FTS5 external-content，列取 tool 的前缀列，顺序一致）──
CREATE VIRTUAL TABLE IF NOT EXISTS tool_fts USING fts5(
    name,
    description,
    category,
    source_id,
    schema,
    content='tool',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS tool_ai AFTER INSERT ON tool BEGIN
    INSERT INTO tool_fts(rowid, name, description, category, source_id, schema)
    VALUES (new.rowid, new.name, new.description, new.category, new.source_id, new.schema);
END;

CREATE TRIGGER IF NOT EXISTS tool_ad AFTER DELETE ON tool BEGIN
    INSERT INTO tool_fts(tool_fts, rowid, name, description, category, source_id, schema)
    VALUES ('delete', old.rowid, old.name, old.description, old.category, old.source_id, old.schema);
END;

-- upsert 更新描述/schema（关键词检索必须跟踪 UPDATE）
CREATE TRIGGER IF NOT EXISTS tool_au AFTER UPDATE ON tool BEGIN
    INSERT INTO tool_fts(tool_fts, rowid, name, description, category, source_id, schema)
    VALUES ('delete', old.rowid, old.name, old.description, old.category, old.source_id, old.schema);
    INSERT INTO tool_fts(rowid, name, description, category, source_id, schema)
    VALUES (new.rowid, new.name, new.description, new.category, new.source_id, new.schema);
END;


-- ── 语义搜索（over schema 文本：name+description+参数+返回说明）──
-- 长 schema 按嵌入模型 token 上限切块（同 memdb/memfiles），一块一行；
-- 检索按块取最小距离、每工具聚合成一条。向量 f32 BLOB + Python 余弦。
CREATE TABLE IF NOT EXISTS tool_embeddings (
    name        TEXT NOT NULL REFERENCES tool(name) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    embedding   BLOB NOT NULL,
    model       TEXT NOT NULL,
    PRIMARY KEY (name, chunk_index)
);


-- ── 元数据 ─────────────────────────────────────────────────────
-- embedding_model：换模型（即使同维）要丢旧向量——不同向量空间。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

PRAGMA user_version = 1;