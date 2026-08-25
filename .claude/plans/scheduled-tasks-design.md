# 定时任务（Scheduled Tasks）实现 — 方案 E

## Goal

实现定时任务：调度循环复用 heartbeat 机制到点注入提示词，主 agent 创建 subagent 异步执行任务，worker 通过 `save_cron_report` 把报告存进 memfiles，push 模式让主 agent 感知完成。任务定义、每次 run 状态、报告索引持久化在 memfiles DB。

**核心**：主 agent 既知道任务开始（创建 worker + async 提交）也知道任务结束（worker 完成后 notify 回来）。

## 设计要点（已讨论收敛）

- 主进程 `schedule_loop` 只做报时 + 注入，不做硬编码 dispatch。
- 执行在主 agent 的短 turn：`spawn_subagent` → `subagent_send_task_async(mode=auto)`（现有工具）。
- 完成回环：worker 调 `save_cron_report` 写 memfiles，store 层反填 `scheduled_runs.report_id`；`on_task_complete` → 主 agent 收到"已完成"。
- 三张表放 memfiles DB：`reports`（报告本体）、`scheduled_tasks`（定义）、`scheduled_runs`（run 状态 + 报告索引）。

## 改动清单

### 1. memfiles schema.sql — 新表 + 独立迁移脚本

**新增表**（放 `slife/plugins/memfiles/schema.sql`，供全新库）：

```sql
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES scheduled_tasks(id),
    title        TEXT DEFAULT '',
    content      TEXT NOT NULL,
    tags         TEXT DEFAULT '',
    file_path    TEXT NOT NULL,
    period_start TEXT,
    period_end   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
-- reports_fts (FTS5) + reports_semantic (vec0) + reports_ai/ad/au 触发器，对齐 notes/diary

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    schedule    TEXT NOT NULL,
    timezone    TEXT DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_runs (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES scheduled_tasks(id),
    due_at     TEXT NOT NULL,
    status     TEXT NOT NULL,
    ran_at     TEXT,
    report_id  INTEGER,
    error      TEXT DEFAULT '',
    UNIQUE(task_id, due_at)
);
-- + idx_reports_task, idx_runs_task_due 索引
```

**迁移脚本**（老库已有 memfiles DB，按用户约定写成独立一次性脚本）：
- `scripts/migrate_memfiles_scheduled.py`：对已存在的 `{agent}.files/.index.db` 执行上述 CREATE IF NOT EXISTS（幂等，已迁移则跳过），不重复建已存在的 notes/diary/files 表。
- **slife 代码里不放迁移代码**——schema.sql 只服务全新库，老库升级跑脚本后重启。

**说明**：`reports` 需要 FTS/vec0 + 触发器 + 双写 md（同 notes/diary）。用与现有触发器相同的模式。新增 kind 进 `_KIND_SPECS`/`_KIND_NAMES`（`store.py:74`/`:109`）即自动纳入语义检索。

### 2. memfiles store.py — 新方法

- `upsert_report(task_id, title, content, tags, period_start, period_end)` — 对齐 `upsert_diary`（`:324`），双写 `reports/<slug>.md` + DB 行，返回 `{kind:"report", doc_id, key, file_path}`。
- `list_reports(task_id=None, limit, offset)` / `get_report(id)` — 对齐 `list_diary`/`get_diary`。
- **反填 `scheduled_runs.report_id`**：`upsert_report` 内按 `(task_id, due_at)` 定位 run（`due_at` 从调用方传入或取 `created_at`），UPDATE `report_id`。这就是"store 层反填，不依赖主 agent 第二次调用工具"。
- 扩展 `_KIND_SPECS` 加 `"report"` 项 + `_KIND_NAMES += ("report",)`，使 `count_unembedded`/`get_unembedded_docs`/`replace_embedding_chunks`/`_clear_kind_chunks`/search 自动覆盖 reports。
- `upsert_scheduled_task` / `list_scheduled_tasks` / `get_scheduled_task(name)` / `set_scheduled_task_enabled`。
- `record_scheduled_run` / `mark_run_missed` / `mark_run_failed` / `list_scheduled_runs(task_id=None, status=None)`。

### 3. memfiles server.py — 新 MCP 工具

- `save_cron_report(task_id, due_at, title, content, tags=None, period_start=None, period_end=None)` — LLM 可见（worker 侧经 `connect_memfiles_http` 注册可用），调 store.upsert_report。
- `report_list(task_id=None)` / `report_read(id)` — 对齐 `diary_list`/`diary_read`。
- `scheduled_task_list` / `scheduled_run_list(task_id=None, status=None)` — 主 agent / 用户可见的查询。
- `scheduled_task_create` / `run_schedule_now` — 见 §5，走 memfiles HTTP 由主 agent 工具调用。

**注意**：server 是共享插件进程，主 agent 和 subagent 都连同一个。`save_cron_report` 在 worker 侧可用（`connect_memfiles_http` 已注册 memfiles 工具）。内部 `__` 前缀工具保持程序化。

### 4. slife/agent/schedules.py — schedule_loop（新）

泛化自 heartbeat.py 的 asyncio 节奏 + 静默/过滤：

- `schedule_loop(service)`：主 agent only（`is_subagent` 不启动，同 heartbeat）。启动时读 `scheduled_tasks`（enabled=1），为每个任务起一个循环；每 tick 解析 `schedule` 表达式算下次触发。
- 到点：写 `scheduled_runs(due_at)` → `inbox.post(source=AgentName("schedule_<name>"), content=触发提示词, handler=_SilentHandler, on_reply=...)`。**不做硬编码 dispatch**。
- busy 时 **pending**（到点该发生，复用 inbox 串行；不同于 heartbeat 的 skip）。
- 醒来/启动检查错过：`due_at` 已过未 dispatch → `mark_run_missed`。
- `run_schedule_now(name)`：立即注入（补做，due_at=now）。
- `start_schedules()` / `stop_schedules()` 挂到 `AgentService.start_inbox` / `stop_inbox`（同 heartbeat 挂点）。

### 5. slife/tools/schedules.py — 主 agent 薄封装工具

复用现有 subagent manager + memfiles client，不引入新机制：

- `scheduled_task_set(name, description="", schedule="", timezone="", enabled=true)` — 幂等：创建或更新（按 name upsert）`scheduled_tasks`。
- `scheduled_task_remove(name)` — 删除任务（连带清理该任务的 runs；报告本体保留）。
- `scheduled_task_list` — 列定义（含 enabled、next_run）。
- `scheduled_run_list(status=None)` — 列 run 历史（含 missed/failed）。
- `run_schedule_now(name)` — 立即触发（补做入口）。
- `scheduled_run_confirm(name, due_at)` — missed → confirmed_done（用户确认"不用补了"）。

**触发提示词**（到点注入，主 agent 收到后执行）：
```
[Schedule <name>] 请创建 subagent 异步执行这个定时任务：{description}，
执行结束后调用 save_cron_report 保存结果，notify 主agent 定时任务已完成信息。
```

### 6. 系统提示词（agent.j2 / system_prompt）— 契约

- 主 agent 理解 `[Schedule <name>]` 触发：创建 worker + async 提交 + 短 turn 让位。
- `save_cron_report` 用途、`scheduled_run_confirm` 用于 missed 确认补做。
- 边界契约：slife 不运行则任务不触发（用户应知晓）。
- TUI 过滤：`restore.py`/`service.py` 对 `[Heartbeat]` 前缀的过滤，扩展 `[Schedule ` 前缀（静默触发、非 `.` 回复才显示）。复用 `HEARTBEAT_MARK` 的识别模式，新增 `SCHEDULE_MARK`。

### 7. 测试

- `tests/test_schedules.py`：cron 表达式解析（`*`/`*/n`/列表/dom/dow OR）、下次触发时间、时区。
- `tests/test_memfiles_reports.py`：upsert_report 双写 md + 反填 report_id + FTS/vec0 检索覆盖 + kind 扩展。
- `tests/test_scheduled_runs.py`：missed 判定、run_schedule_now、scheduled_run_confirm 状态机。
- `tests/test_schedule_loop.py`：到点注入、busy pending、主 agent 短 turn 调用 `spawn_subagent`+`send_task_async`。

## 关键决策（已定）

1. **方案 E**（heartbeat 注入 + 主 agent 建 worker），非调度循环硬编码 dispatch。
2. **执行在 worker**（subagent，不 save turn），产出靠返回值 + `save_cron_report` 落 memfiles。
3. **三表放 memfiles DB**，`scheduled_runs.report_id` 由 store 层反填。
4. **schedule 定义存 DB**（运行时可变，对话里可加任务），非 json5 静态。
5. **B2 常驻 worker**（每 schedule 一个，共享 source history 保证跨次上下文连续）；首版若任务少可退化 B1。
6. **cron 解析器手写**（`slife/schedules.py`，不引 apscheduler/croniter），表驱动测试锁语义。
7. **busy 时 pending**（到点该发生），不同于 heartbeat 的 skip。
8. **保留 period_start/end**（周报/日报时间范围）。
9. **不改文档**：本实现不修改 DESIGN.md；设计文档 `docs/scheduled-tasks-design.md` 已为定稿，若实现偏离，实现后同步。

## 顺序

1. `slife/schedules.py` cron 解析器 + 测试。
2. memfiles schema（新表 + 新 kind）+ store 方法 + `scripts/migrate_memfiles_scheduled.py` 迁移脚本。
3. memfiles server 工具（save_cron_report / report_list / report_read / scheduled_*_list）。
4. `slife/agent/schedules.py` schedule_loop + service 挂点。
5. 主 agent 工具 + 触发提示词 + 系统提示词契约。
6. TUI 过滤扩展 + 测试。

## 风险 / 注意

- **reports 加入 `_KIND_SPECS`**：影响 memfiles 的语义检索 drainer（count/get_unembedded/replace），需同步 `schema.sql` 建 reports_fts/reports_semantic + 触发器，否则 `get_unembedded_docs` 会查不存在的表报错。测试覆盖。
- **server 是共享插件进程**：`save_cron_report` 写入 memfiles 的同时写 `scheduled_runs`——同一 store，事务一致。
- **主 agent 短 turn 的成本**：每 tick 一个提交 turn，高频任务才明显；首版低频为主，可接受。
- **状态依赖 LLM 调用工具**：提交后主 agent 写 ran；`report_id` 由 store 层反填兜底，不依赖主 agent 第二次调用。
- **`scheduled_tasks.schedule` 解析**：cron 5 字段；`every` 语法（`every: Ns/m/h`）或 `at`（一次性）是否加，首版先 cron + `manual`。
