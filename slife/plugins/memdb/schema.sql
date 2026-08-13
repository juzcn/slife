-- ═══════════════════════════════════════════════════════════════
--  Slife 记忆库 — 以 turn 为单位的永久记忆
--
--  一个 turn = 一次用户消息 + assistant 的完整响应
--  （包括 thinking、tool calls、tool results、最终回复）
--
--  每一行是独立的——没有 session 分组，没有生命周期。
--  恢复时按 rowid 倒序取最近 N 个 turns 重组上下文。
--
--  Agent 隔离在文件级别：每个 agent_id 拥有独立的 .db 文件。
-- ═══════════════════════════════════════════════════════════════


CREATE TABLE IF NOT EXISTS diary (

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

    -- ▼ 用户图片附件（JSON 数组：本地路径或 https URL）
    images         TEXT NOT NULL DEFAULT '',

    -- ▼ 时间
    created_at     TEXT NOT NULL,   -- 用户输入时间（输入框回车时刻）
    completed_at   TEXT,            -- assistant 完成时间（旧库经 scripts/migrate_memdb_completed_at.py 回填）

    -- ▼ 背景
    channel        TEXT DEFAULT '',  -- 'human', 'wechat', or remote agent id
    who_helped     TEXT DEFAULT '',
    what_model     TEXT DEFAULT '',

    -- ▼ 用量
    token_count    INTEGER NOT NULL DEFAULT 0
);


-- ── 关键词搜索 ────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS diary_fts USING fts5(
    user_message,
    messages,
    summary,
    tags,
    channel,
    content='diary',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS diary_ai AFTER INSERT ON diary BEGIN
    INSERT INTO diary_fts(rowid, user_message, messages, summary, tags, channel)
    VALUES (new.rowid, new.user_message, new.messages, new.summary, new.tags, new.channel);
END;

CREATE TRIGGER IF NOT EXISTS diary_ad AFTER DELETE ON diary BEGIN
    INSERT INTO diary_fts(diary_fts, rowid, user_message, messages, summary, tags, channel)
    VALUES ('delete', old.rowid, old.user_message, old.messages, old.summary, old.tags, old.channel);
END;

-- memory_summarize writes summary/tags via UPDATE — the FTS5 external-content
-- index must track those updates or the summary stays invisible to keyword
-- search (only the insert-time empty row was indexed).
CREATE TRIGGER IF NOT EXISTS diary_au AFTER UPDATE ON diary BEGIN
    INSERT INTO diary_fts(diary_fts, rowid, user_message, messages, summary, tags, channel)
    VALUES ('delete', old.rowid, old.user_message, old.messages, old.summary, old.tags, old.channel);
    INSERT INTO diary_fts(rowid, user_message, messages, summary, tags, channel)
    VALUES (new.rowid, new.user_message, new.messages, new.summary, new.tags, new.channel);
END;


-- ── 语义搜索 ──────────────────────────────────────────────────
-- One turn → one or more chunks (long turns are split by paragraph).
-- diary_rowid references diary.rowid; chunk_index is 0-based within a turn.
-- search_semantic groups results by diary_rowid (best chunk wins).
CREATE VIRTUAL TABLE IF NOT EXISTS diary_semantic USING vec0(
    turn_embedding float[1536],
    +diary_rowid   INTEGER,
    +chunk_index   INTEGER,
    +summary       TEXT,
    +tags          TEXT,
    +created_at    TEXT
);


-- ── 元数据 ────────────────────────────────────────────────────
-- Tracks which embedding model produced the diary_semantic vectors.
-- When the model changes (even same-dimension, e.g. ada-002 → 3-small),
-- old vectors are dropped because they live in a different vector space.
CREATE TABLE IF NOT EXISTS diary_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── 索引 ──────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_diary_created ON diary(created_at);

