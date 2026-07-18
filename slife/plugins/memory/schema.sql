-- ═══════════════════════════════════════════════════════════════
--  Slife 记忆库 — 以 turn 为单位的永久记忆
--
--  一个 turn = 一次用户消息 + assistant 的完整响应
--  （包括 thinking、tool calls、tool results、最终回复）
--
--  每一行是独立的——没有 session 分组，没有生命周期。
--  恢复时按 rowid 倒序取最近 N 个 turns 重组上下文。
-- ═══════════════════════════════════════════════════════════════


CREATE TABLE IF NOT EXISTS diary (

    -- ▼ 谁写的
    author         TEXT NOT NULL DEFAULT 'default',

    -- ▼ 用户说了什么（独立列，便于搜索和嵌入）
    user_message   TEXT NOT NULL DEFAULT '',

    -- ▼ assistant 的完整响应（OpenAI 消息 JSON 数组）
    --   [
    --     {"role":"assistant","content":"…","thinking":"…","tool_calls":[…]},
    --     {"role":"tool","tool_call_id":"…","content":"…"},
    --     {"role":"assistant","content":"…"}
    --   ]
    messages       TEXT NOT NULL DEFAULT '[]',

    -- ▼ 回忆线索（LLM 通过 memory_summarize 写入）
    summary        TEXT DEFAULT '',
    tags           TEXT DEFAULT '',

    -- ▼ 时间
    created_at     TEXT NOT NULL,

    -- ▼ 背景
    channel        TEXT DEFAULT '',  -- 'human', 'wechat', or remote agent id
    who_helped     TEXT DEFAULT '',
    what_model     TEXT DEFAULT '',

    -- ▼ 用量
    token_count    INTEGER NOT NULL DEFAULT 0
);


-- ── 关键词搜索 ────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS diary_fts USING fts5(
    author,
    user_message,
    messages,
    summary,
    tags,
    channel,
    content='diary',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS diary_ai AFTER INSERT ON diary BEGIN
    INSERT INTO diary_fts(rowid, author, user_message, messages, summary, tags, channel)
    VALUES (new.rowid, new.author, new.user_message, new.messages, new.summary, new.tags, new.channel);
END;

CREATE TRIGGER IF NOT EXISTS diary_ad AFTER DELETE ON diary BEGIN
    INSERT INTO diary_fts(diary_fts, rowid, author, user_message, messages, summary, tags, channel)
    VALUES ('delete', old.rowid, old.author, old.user_message, old.messages, old.summary, old.tags, old.channel);
END;


-- ── 语义搜索 ──────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS diary_semantic USING vec0(
    author         TEXT PARTITION KEY,
    turn_embedding float[1536],
    +summary       TEXT,
    +tags          TEXT,
    +created_at    TEXT
);


-- ── 索引 ──────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_diary_author ON diary(author, rowid);
CREATE INDEX IF NOT EXISTS idx_diary_created ON diary(author, created_at);
