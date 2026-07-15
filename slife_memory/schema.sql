-- ═══════════════════════════════════════════════════════════════
--  Slife 记忆库 — 像日记一样记录每一次对话
--
--  设计原则：
--    1. 一行 = 一次完整的对话（一本"笔记"），自包含、无需 JOIN
--    2. 列名用自然语言，LLM 看到就能理解
--    3. 状态用中文描述，不是机器状态码
--    4. 检索先看摘要和标签（轻量），再看全文（按需）
--    5. 语义搜索基于摘要，不是原始消息（更接近"回忆"的感觉）
-- ═══════════════════════════════════════════════════════════════


-- ── 日记 ─────────────────────────────────────────────────────
-- 每一行是一段完整的记忆——一次对话从开始到结束的全部内容。
-- 就像一个日记条目：时间、标题、大意、正文。
CREATE TABLE IF NOT EXISTS diary (

    -- ▼ 谁写的
    author         TEXT NOT NULL DEFAULT 'default',    -- 谁（--user）

    -- ▼ 哪一本笔记（一次 Slife 启动 → 退出的完整对话）
    title          TEXT,                              -- 对话标题，如"重构工具系统"
    created_at     TEXT NOT NULL,                     -- 对话开始时间
    updated_at     TEXT NOT NULL,                     -- 最后更新时间
    status         TEXT NOT NULL DEFAULT '进行中',     -- 进行中 | 已完成 | 意外中断

    -- ▼ 笔记正文（给 LLM 看的——完整的 OpenAI 消息列表，JSON）
    --   [
    --     {"role":"system","content":"你是 Slife…"},
    --     {"role":"user","content":"帮我重构工具系统"},
    --     {"role":"assistant","content":null,"tool_calls":[…]},
    --     {"role":"tool","tool_call_id":"…","content":"…"},
    --     {"role":"assistant","content":"当前工具有几个改进点：1. …"},
    --     …
    --   ]
    messages       TEXT NOT NULL,                     -- 完整对话 JSON

    -- ▼ 回忆线索（帮助 LLM 快速判断这段记的是什么事）
    summary        TEXT,                              -- 一两句话概括这次聊了什么
    tags           TEXT,                              -- 主题标签，逗号分隔："重构,工具系统,MCP"
    key_moments    TEXT,                              -- 关键节点（重要决定、发现的bug、达成的共识）

    -- ▼ 背景信息
    who_helped     TEXT,                              -- 哪个 agent 服务的（--name）
    what_model     TEXT,                              -- 用的什么模型

    -- ▼ 规模感（不需要精确数字，知道"很多"还是"很少"就够了）
    how_many_turns    INTEGER NOT NULL DEFAULT 0,     -- 聊了几轮
    how_many_tokens   INTEGER NOT NULL DEFAULT 0,      -- 用了多少 token

    -- ▼ 裁剪位置（用于精确恢复 working context）
    trim_count        INTEGER NOT NULL DEFAULT 0       -- 从开头裁剪了多少条消息
);


-- ── 关键词搜索 ────────────────────────────────────────────────
-- 索引 messages 的文本内容，支持按关键词快速找到相关记忆。
-- 用 FTS5 的 BM25 排序——匹配越精确、出现越频繁，排名越靠前。
CREATE VIRTUAL TABLE IF NOT EXISTS diary_fts USING fts5(
    author,                                           -- 限定用户
    title,                                            -- 标题命中
    summary,                                          -- 摘要命中
    tags,                                             -- 标签命中
    key_moments,                                      -- 关键节点命中
    messages,                                         -- 全文搜索（对话正文）
    content='diary',
    content_rowid='rowid'
);

-- 自动同步——往 diary 写一行，FTS 索引自动跟上
CREATE TRIGGER IF NOT EXISTS diary_ai AFTER INSERT ON diary BEGIN
    INSERT INTO diary_fts(rowid, author, title, summary, tags, key_moments, messages)
    VALUES (new.rowid, new.author, new.title, new.summary, new.tags, new.key_moments, new.messages);
END;

CREATE TRIGGER IF NOT EXISTS diary_ad AFTER DELETE ON diary BEGIN
    INSERT INTO diary_fts(diary_fts, rowid, author, title, summary, tags, key_moments, messages)
    VALUES ('delete', old.rowid, old.author, old.title, old.summary, old.tags, old.key_moments, old.messages);
END;

CREATE TRIGGER IF NOT EXISTS diary_au AFTER UPDATE ON diary BEGIN
    INSERT INTO diary_fts(diary_fts, rowid, author, title, summary, tags, key_moments, messages)
    VALUES ('delete', old.rowid, old.author, old.title, old.summary, old.tags, old.key_moments, old.messages);
    INSERT INTO diary_fts(rowid, author, title, summary, tags, key_moments, messages)
    VALUES (new.rowid, new.author, new.title, new.summary, new.tags, new.key_moments, new.messages);
END;


-- ── 语义搜索 ──────────────────────────────────────────────────
-- 对摘要做向量嵌入，支持"感觉像"的搜索——即使用词不一样也能找到。
-- user 用分区键隔离，每个用户独立搜索空间。
CREATE VIRTUAL TABLE IF NOT EXISTS diary_semantic USING vec0(
    author     TEXT PARTITION KEY,
    summary_embedding float[1536],
    +title     TEXT,                                   -- 辅助列，直接取不用 JOIN
    +summary   TEXT,
    +tags      TEXT,
    +created_at TEXT
);


-- ── 索引 ──────────────────────────────────────────────────────
-- 高频查询：按用户 + 时间排序/筛选
CREATE INDEX IF NOT EXISTS idx_diary_author_updated ON diary(author, updated_at);
CREATE INDEX IF NOT EXISTS idx_diary_author_created ON diary(author, created_at);


-- ── 版本记录 ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS about (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

INSERT OR IGNORE INTO about VALUES ('version', '1');
INSERT OR IGNORE INTO about VALUES ('description', 'Slife 记忆库 — 以日记的方式记录每一次对话');


-- ═══════════════════════════════════════════════════════════════
--  怎么用 —— 给 LLM 看的查询指南
-- ═══════════════════════════════════════════════════════════════

-- ▼ 翻开最近的日记
-- SELECT rowid, title, summary, tags, created_at, status,
--        how_many_turns, who_helped
-- FROM diary
-- WHERE author = ?
-- ORDER BY updated_at DESC
-- LIMIT 20;


-- ▼ 找一段中断的对话（启动恢复）
-- SELECT rowid, title, summary, created_at, updated_at,
--        how_many_turns, how_many_tokens, who_helped, what_model
-- FROM diary
-- WHERE author = ? AND status = '进行中'
-- ORDER BY updated_at DESC
-- LIMIT 1;


-- ▼ 读到完整的对话内容（恢复或深入查看）
-- SELECT messages, title, summary, tags, key_moments
-- FROM diary
-- WHERE author = ? AND rowid = ?;


-- ▼ 按关键词回忆
-- SELECT d.rowid, d.title, d.summary, d.tags, d.created_at,
--        d.how_many_turns,
--        snippet(diary_fts, 4, '…', '…', '…', 60) AS 片段
-- FROM diary_fts fts
-- JOIN diary d ON fts.rowid = d.rowid
-- WHERE diary_fts MATCH ?
--   AND fts.author = ?
-- ORDER BY rank
-- LIMIT 20;


-- ▼ 按感觉回忆（语义相似）
-- SELECT rowid, title, summary, tags, created_at, distance
-- FROM diary_semantic
-- WHERE summary_embedding MATCH ?
--   AND author = ?
--   AND k = 20
-- ORDER BY distance;


-- ▼ 为完成对话写上总结
-- UPDATE diary
-- SET title = ?, summary = ?, tags = ?, key_moments = ?, status = '已完成', updated_at = ?
-- WHERE author = ? AND rowid = ?;
