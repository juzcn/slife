-- ═══════════════════════════════════════════════════════════════
--  slife 统一工具目录库（tools.db）— host 主进程持有的单一事实源
--
--  一行 = 一个 tool：function tool（builtin | job | plugin | mcp | rest-api）、
--  skill（skills 目录里的一个 SKILL.md）、cli（tools.yaml 里的一条 cli 配置）；
--  mcp/rest-api 用 ``{server}__{tool}`` 全名标识（source_id 指 server）；
--  skill/cli 加家族前缀（``skill:browser-harness`` / ``cli:browser-harness``）——
--  name 是行的身份（主键、embeddings 的外键、搜索结果的合并键），两个家族不能共用一个，
--  而共用也不是错误：browser-harness 既是一个 CLI，也是记录它的那个 skill。
--  plugin = 内置插件自己的工具，source_id 指该插件名（job-coding 的 job 工具仍是 job）。
--  **一行是什么，只由 category 回答**：没有 type 派生列。派生列意味着每次 insert /
--  update / 迁移都要多写一次、多同步一次，而它回答的问题（"这行有 load 状态吗"）
--  就是一次 category 的集合判断（FUNCTION_CATEGORIES）——写不成 stale。
--  落盘 + WAL（多进程：主 agent 写、subagent 只读/短写），busy_timeout
--  兜底 SQLITE_BUSY。无运行时 DDL 迁移：schema 版本走 PRAGMA user_version；
--  这个库是派生数据（行来自 tool registry / tools.yaml / skills 目录 / 插件子进程），
--  所以**结构变了就删库重建，不原地升级** —— 改 CHECK、加列、删列都算（v5 的
--  status→load_status、v6/v7 的 NOT NULL、v8 的 status 三态、v9 删 type 都没有
--  迁移步骤）。旧文件由 _check_columns（列，多一列少一列都算）与
--  _check_categories（CHECK）报出来，提示里写着删哪个文件。
--
--  tool.status 是这一行此刻的状态，三个**互斥**的值：enabled | disabled | error。
--  一行只在这三者之一：error 的行不是 enabled 的行，因此没有开关可拨（*_set_enabled
--  只解决 disabled）；disabled 的行也永远不会是 error。
--  disabled 来自配置（tools.yaml 的开关），error 是运行时判决（原因不止一种：
--  server / plugin 起不来、list tools 超时、SKILL.md 读不出来……），enabled 是其余一切。
--  修好后 error 回到 enabled —— 判决由写它的那条线收回（mark_source_connected /
--  源自己重算），别处的代码不猜原因，也不给"等一会儿就好了"这种建议。
--  两个写者各管一条转换线（是同一个值被换掉，不是两个事实并存）：配置写
--  disabled↔enabled，运行时写 enabled→error / error→enabled —— 每个 UPDATE 的
--  WHERE 就是这条线的守卫（mark_source_error 不碰 disabled 的行，set_source_enabled
--  只动 disabled↔enabled，mark_source_connected 只清 error 的行）。
--  它不写进 load_status，也不覆盖 load_status：写进去会把 model 的 loaded 决定
--  抹掉（掉线一次、重启一次就丢）。
--  tool.load_status 只存 loaded|unloaded|n/a（function tool 的前两者是 model 的
--  决定，也是这个库唯一要持久化的东西；skill/cli 无 load 概念，存 'n/a' 而
--  不是 NULL —— 列域与 model 看到的词汇表一一对应，过滤就是普通等值）。
--  effective status 是两列的合成，见 catalog._effective_status：status 不是
--  enabled 就是它自己（disabled|error），否则是 load_status —— 没有 load 概念的
--  skill/cli 报 status 自己（enabled），而不是一个读不出信息的 'n/a'。
--  FTS5（关键词）+ BLOB 向量（语义，over schema 文本）混合检索。
-- ═══════════════════════════════════════════════════════════════


-- 工具目录。status 一律来自 tools.yaml 的开关（mcp/rest-api 也一样，就是该 server
-- 的开关）或运行时的判决，两者都不动 load_status。
-- **没有可空列**："本地"（source_id）、"无 schema"（schema）、"无 load 概念"
-- （load_status）都是真实取值，用 'n/a' 表达。NULL 的代价是每个读点都要多写一个
-- IS NULL 分支，漏一个就静默返回空集；改用列上的默认值，同一个意思只有一种读法。
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
    source_id   TEXT NOT NULL DEFAULT 'n/a', -- 拥有者：server 名 / 插件名；'n/a' = 本地（builtin/job/skill/cli）
    schema      TEXT NOT NULL DEFAULT 'n/a', -- function tool：Tool def JSON；skill：SKILL.md 全文；
                                             -- cli：合成的 {name, description} 描述符 ——
                                             -- 没有 tool def，但这一列同时是语义索引的**文档**，
                                             -- 空了就不可嵌入（见文件末尾），row 会对语义腿隐形
    status      TEXT NOT NULL DEFAULT 'enabled' -- enabled | disabled（配置）| error（运行态）
                CHECK (status IN ('enabled','disabled','error')),
    load_status TEXT NOT NULL DEFAULT 'n/a', -- 'loaded'|'unloaded'（function tool）|'n/a'（skill/cli）
    last_loaded TEXT NOT NULL DEFAULT ''     -- 本地 ISO；'' = 从未 load（LRU 里排最旧）
);
CREATE INDEX IF NOT EXISTS idx_tool_status ON tool(status);
CREATE INDEX IF NOT EXISTS idx_tool_load_status ON tool(load_status);
CREATE INDEX IF NOT EXISTS idx_tool_source ON tool(source_id);
CREATE INDEX IF NOT EXISTS idx_tool_category ON tool(category);


-- 没有 server 表：哪些 server 该连由 tools.yaml 的 enabled 决定，此刻谁活着由
-- 网关的 pool（mcp_list / __check）回答，这个库只把结果记在 tool 行上 ——
-- 服务器不可用（未连上 / 掉线 / 连接失败 / 网关子进程死亡）时，它的 tool 行
-- status 置 'error'（退出注入集）；连上后清回 'enabled'，**不动 load_status** ——
-- model 之前 loaded 的工具连上后仍然是 loaded。


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
    PRIMARY KEY (name, chunk_index)
);


-- ── 元数据 ─────────────────────────────────────────────────────
-- embedding_model：换模型（即使同维）要丢旧向量——不同向量空间。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

PRAGMA user_version = 9;
