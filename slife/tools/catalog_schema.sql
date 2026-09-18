-- ═══════════════════════════════════════════════════════════════
--  slife 统一工具目录库（tools.db）— host 主进程持有的单一事实源
--
--  一行 = 一个 tool：function tool（builtin | job | plugin | mcp | rest-api）、
--  skill（skills 目录里的一个 SKILL.md）、cli（tools.json5 里的一条 cli 配置）；
--  mcp/rest-api 用 ``{server}__{tool}`` 全名标识（source_id 指 server）；
--  skill/cli 加家族前缀（``skill:browser-harness`` / ``cli:browser-harness``）——
--  name 是行的身份（主键、embeddings 的外键、搜索结果的合并键），两个家族不能共用一个，
--  而共用也不是错误：browser-harness 既是一个 CLI，也是记录它的那个 skill。
--  plugin = 内置插件自己的工具，source_id 指该插件名（job-coding 的 job 工具仍是 job）。
--  type 是 category 的粗粒度投影：func | skill | cli —— load/unload 只属于 func。
--  落盘 + WAL（多进程：主 agent 写、subagent 只读/短写），busy_timeout
--  兜底 SQLITE_BUSY。无运行时 DDL 迁移：schema 版本走 PRAGMA user_version；
--  这个库是派生数据（行来自 tool registry / tools.json5 / skills 目录 / 插件子进程），
--  所以**结构变了就删库重建，不原地升级** —— 改 CHECK、加列、改列名都算（v5 的
--  status→load_status、v6/v7 的 NOT NULL 都没有迁移步骤）。旧文件由 _check_columns
--  （列）与 _check_categories（CHECK）报出来，提示里写着删哪个文件。
--
--  tool.load_status 只存 loaded|unloaded|n/a（type='func' 的前两者是 model 的
--  决定，也是这个库唯一要持久化的东西；skill/cli 无 load 概念，存 'n/a' 而
--  不是 NULL —— 列域与 model 看到的词汇表一一对应，过滤就是普通等值）。
--  tool.unavailable = 该 tool 的拥有者（server / plugin）此刻不可用
--  （未连上/掉线/连接失败/网关子进程死亡），由 host 写；它是一列**独立**的
--  运行态，不写进 load_status：写进去会把 model 的 loaded 决定抹掉（掉线一次、
--  重启一次就丢）。两者都不覆盖对方的存储。
--  FTS5（关键词）+ BLOB 向量（语义，over schema 文本）混合检索。
-- ═══════════════════════════════════════════════════════════════


-- 工具目录。enabled 一律来自 tools.json5（mcp/rest-api 也一样，就是该 server
-- 的开关）；可用性由 unavailable 单独表达，两者都不动 load_status。
-- **没有可空列**："本地"（source_id）、"无 schema"（schema）、"无 load 概念"
-- （load_status）都是真实取值，用 'n/a' 表达。NULL 的代价是每个读点都要多写一个
-- IS NULL 分支（`(unavailable IS NULL OR unavailable = 0)`），漏一个就静默返回
-- 空集；改用列上的默认值，同一个意思只有一种读法。
-- （"没有意见"是 *调用方* 的概念 —— upsert 传 None 表示"别动这列" —— 不是列里
-- 的一个取值。）
CREATE TABLE IF NOT EXISTS tool (
    -- NOT NULL is explicit: a TEXT PRIMARY KEY is NOT implicitly NOT NULL in
    -- SQLite (only INTEGER PRIMARY KEY is), so without it a nameless row is
    -- storable.  Every column carries a default, this one included, so an
    -- insert that names only what it knows never fails on a column it has no
    -- opinion about.  A nameless row is still not something anyone writes —
    -- the reconcile skips one before it reaches here — and '' being the
    -- primary key means a second one would collide rather than accumulate.
    name        TEXT PRIMARY KEY NOT NULL DEFAULT '',  -- mcp: '{server}__{tool}'；skill/cli: '{category}:{name}'；其余裸名
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL               -- builtin | job | plugin | mcp | rest-api | skill | cli
                CHECK (category IN ('builtin','job','plugin','mcp','rest-api','skill','cli')),
    type        TEXT NOT NULL DEFAULT 'func' -- func | skill | cli（粗粒度种类，由 category 派生）
                CHECK (type IN ('func','skill','cli')),
    source_id   TEXT NOT NULL DEFAULT 'n/a', -- 拥有者：server 名 / 插件名；'n/a' = 本地（builtin/job/skill/cli）
    schema      TEXT NOT NULL DEFAULT 'n/a', -- func：Tool def JSON；skill：SKILL.md 全文；
                                             -- cli：合成的 {name, description} 描述符 ——
                                             -- 没有 tool def，但这一列同时是语义索引的**文档**，
                                             -- 空了就不可嵌入（见文件末尾），row 会对语义腿隐形
    enabled     INTEGER NOT NULL DEFAULT 1,  -- 布尔，来自 tools.json5 的 enabled（1 = 开）
    load_status TEXT NOT NULL DEFAULT 'n/a', -- 'loaded'|'unloaded'（type='func'）|'n/a'（skill/cli）
    unavailable INTEGER NOT NULL DEFAULT 0,  -- 布尔：1 = 拥有者此刻不可用（effective status 记 unavailable）
    last_loaded TEXT NOT NULL DEFAULT ''     -- 本地 ISO；'' = 从未 load（LRU 里排最旧）
);
CREATE INDEX IF NOT EXISTS idx_tool_load_status ON tool(load_status);
CREATE INDEX IF NOT EXISTS idx_tool_source ON tool(source_id);
CREATE INDEX IF NOT EXISTS idx_tool_category ON tool(category);
CREATE INDEX IF NOT EXISTS idx_tool_type ON tool(type);


-- 没有 server 表：哪些 server 该连由 tools.json5 的 enabled 决定，此刻谁活着由
-- 网关的 pool（mcp_list / __check）回答，这个库只把结果记在 tool 行上 ——
-- 服务器不可用（未连上 / 掉线 / 连接失败 / 网关子进程死亡）时，它的 tool 行
-- unavailable 置 1（effective status 记 'unavailable'，退出注入集）；连上后清掉
-- 这个标记，**不动 load_status** —— model 之前 loaded 的工具连上后仍然是 loaded。


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
-- **可嵌入 = schema 不是 '' 也不是 'n/a'**（CatalogStore._EMBEDDABLE_SCHEMA）：
-- 哨兵值是可空列的替代品，不是文档；把它当文本嵌入会给"没有 schema"的行
-- 造一个毫无意义的向量。
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

PRAGMA user_version = 7;