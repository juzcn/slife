-- ═══════════════════════════════════════════════════════════════
--  slife 统一工具目录库（tools.db）— host 主进程持有的单一事实源
--
--  一行 = 一个工具（6 类：builtin | job | mcp | rest-api | skill | cli），
--  mcp/rest-api 用 ``{server}__{tool}`` 全名标识（source_id 指 server）。
--  落盘 + WAL（多进程：主 agent 写、subagent 只读/短写），busy_timeout
--  兜底 SQLITE_BUSY。无运行时 DDL 迁移：schema 版本走 PRAGMA user_version，
--  真需要迁移时在 _config_io 的跨进程 filelock 下重建新库。
--
--  tool.status 存 loaded|unloaded|error|NULL（skill/cli 无 load 概念）；
--  error = 该 server 此刻不可用（未连上/掉线/连接失败/网关子进程死亡），由 host 写。
--  FTS5（关键词）+ BLOB 向量（语义，over schema 文本）混合检索。
-- ═══════════════════════════════════════════════════════════════


-- 工具目录。mcp/rest-api 的 enabled 为 NULL（可用性改由 status='error' 表达），
-- 其余类别的 enabled 来自各 json5 section。status 会话内可翻转。
CREATE TABLE IF NOT EXISTS tool (
    name        TEXT PRIMARY KEY,            -- mcp: '{server}__{tool}'；否则裸名
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL               -- builtin | job | mcp | rest-api | skill | cli
                CHECK (category IN ('builtin','job','mcp','rest-api','skill','cli')),
    source_id   TEXT,                        -- 仅 mcp/rest-api：所属 server 名
    schema      TEXT,                        -- Tool def JSON | SKILL.md 文本 | NULL(cli)
    enabled     INTEGER,                     -- 0/1（builtin/job/skill/cli）；NULL（mcp/rest-api，join server）
    status      TEXT,                        -- 'loaded'|'unloaded'|'error'（function tool）；skill/cli NULL
    last_loaded TEXT                         -- 本地 ISO，LRU evict 排序
);
CREATE INDEX IF NOT EXISTS idx_tool_status ON tool(status);
CREATE INDEX IF NOT EXISTS idx_tool_source ON tool(source_id);
CREATE INDEX IF NOT EXISTS idx_tool_category ON tool(category);


-- 没有 server 表：哪些 server 该连由 tools.json5 的 enabled 决定，此刻谁活着由
-- 网关的 pool（mcp_list / __check）回答，这个库只把结果记在 tool 行上 ——
-- 服务器不可用（未连上 / 掉线 / 连接失败 / 网关子进程死亡）时，它的 tool 行
-- status 置 'error'；连上后由镜像重置回该类默认（autoload→loaded，否则 unloaded）。


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

PRAGMA user_version = 2;