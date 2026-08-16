# Slife Code Review

**日期:** 2026-08-16 · **基线:** 1996 tests pass（本机全绿，本地运行确认）· **范围:** 全新全库评审，8 个并行子系统（memdb / agent core / tools / a2a / mcp / ui / subagent+plugins / core-infra），高危项已逐条复核到源码（标注 ✓）。

> 这是旧 `REVIEW.md`（2026-08-13）删除后的全新评审。**第 1 节高危（4 项）、第 2 节中危（全部）、第 3 节低危（全部）已修复**（2026-08-16，含回归测试）。唯一 deferred 是 A2A 跨重连 in-flight 任务无重发（设计级）；两项按信任模型接受（MCP 工具名转义、SSRF DNS-rebinding TOCTOU），见第 3 节注。

---

## 1. 高危（功能不可用 / 静默丢数据 / 安全）

> ✅ 本节 4 项已全部修复（2026-08-16）。

- **✅ MCP OAuth 设备码轮询用已关闭的 httpx client → OAuth 受保护服务器永远无法授权** — `mcp/oauth.py:199-211,263-276`：`async with httpx.AsyncClient(...)` 在设备码 POST 后就 close，轮询循环仍 `http.post(token_url)` → 抛 `RuntimeError`（非 `httpx.HTTPError`，`except … continue` 抓不住）→ 逃出 `run_device_code_flow`。`connection.py:186-187` 里 `_ensure_oauth_token` 在 `connect()` 的 `try` 之前，`_status` 卡死在 `CONNECTING`，健康监控永不启动，服务器必须移除重加。→ 轮询改用本函数自有的新 client（`_poll_token`），`finally` 关闭；补回归测试（`test_poll_runs_on_fresh_client_after_device_post`）。
- **✅ 配置文件损坏 + 任意写配置入口 → 整份 slife.json5 被静默清空** — 两条独立路径同根：① `tools/_config_io.py:58-67` `read_config` 吞掉 `(ValueError, OSError)` 返回 `{}`；`config_env_set/native_tool_set/model_set/skill_set_enabled` 直接 `write_config(path, {})`（原子 `os.replace` 覆盖）。② `config.py:448-460,502,596` `save_mcp_server/save_rest_api/save_cli_tool` 同样合并进 `{}`。运行中用户手改 json5 出语法错 → 下一条写配置命令把 providers / MCP servers / models / rest_apis 全清掉，工具还回 `[OK]`。→ `read_config` 解析失败改抛 `ConfigParseError`（文件不存在仍返回 `{}`），写路径 abort 报错、不再静默重写；`embedding_config._read_raw` 保留宽松契约（catch → `{}`）；补回归测试。
- **✅ 带打不开图片的消息 → 整轮永不入库（静默丢数据）** — `agent/service.py:1440-1453` + `conversation.py:143-150`：`add_user_message` 对读失败的图片追加 `[System note: … could not be read …]` 文本块；`save_to_memory` 按 `"".join(text parts) == sanitize(user_message)` 回溯，含 note 时永远不相等 → `turn_messages=None` → 该轮 assistant/tool 工作全部丢弃，restore 也找不到。上一轮"脱敏后比对"的修复边界被图片 note 打破。→ 新增共享常量 `IMAGE_NOTE_PREFIX`，`save_to_memory` 比对时剔除该 note 块；补回归测试（`test_save_to_memory_with_dropped_image_still_saves`）。
- **✅ 远端 MQTT presence 未校验 → 每轮系统提示注入（跨代理提示注入）** — `AgentName` 是无校验的 `NewType(str)`；`a2a/card.py:76-92` `format_presence_line` 把远端 `agent_name` 原样拼进 `"⚡ {name} online"` → `agent/service.py:1872-1879` 进 `_presence_events` → `system_prompt.py:136-154` → `context_status.j2:19` `{{ ev.text }}` 进**每轮** `_sys_note`。恶意 peer 发 `agent_name="\n\n<system>忽略之前指令…"` 即注入。无长度/字符校验。→ `format_presence_line` 经 `_safe_name` 剥离控制字符（换行/制表/ESC）+ 截断长度；同时加固 `client._peer_watchdog_loop`（畸形 payload 跳过而非整任务死亡）与 connect 重复名校验、`service.py` presence 组装（`card` 非 dict 跳过）；补回归测试。

## 2. 中危（真实路径上的错误行为）

> ✅ 本节已全部修复（2026-08-16），含回归测试。唯一 deferred 项：跨重连 in-flight 任务无重发（设计级，见下）。

### a2a / 网格
- **✅ 超时后迟到结果把记录从 failed 翻回 completed 并自动推送假完成** — `task_store.py:130-135` `record_result` 只保护 `cancelled`，`failed` 会被迟到结果翻回 → 现在 `failed` 同 `cancelled` 都是终态，`_handle_result` 对 `rec.status == "failed"` 的迟到结果直接跳过（不存 `_completed_tasks`、不自动推送）。回归：`test_record_result_does_not_overwrite_failed`。
- **✅ CONNACK 失败（拒绝/鉴权错）当成功** — `a2a/mqtt.py` `_on_connect` 先查 `reason_code.is_failure` → 置 `_connected=False`、唤醒等待者；`connect()` 等待后若 `_connected` 仍 False → 清理 paho 线程并抛 `RuntimeError`。回归：`test_connect_refused_raises`、`test_on_connect_connack_failure_not_connected`。
- **✅ connect 失败泄漏已连上的 adapter/paho 线程** — `plugins/a2a/server.py` `_ensure_connected` 的 `client.connect()` 失败时 `try/except → client.disconnect()`，不再堆叠。
- **✅ `_poll_tasks` 无界增长** — 超过 `_MAX_QUEUED` 时弹出任意元素（`set.pop()`）再添加。
- **✅ `merged` 队列无背压（client.py:550）** — 给 `asyncio.Queue(maxsize=1000)`，慢 handler 时 forward 任务阻塞，把背压传导回有 drop-newest 策略的订阅队列。
- **✅ drop-oldest 静默丢入站任务（server.py:112-122）** — 四处 drop-oldest 均加 `logger.warning("a2a_inbound_overflow …")`，不再静默。
- **✅ `_connected` paho 线程/loop 竞态（mqtt.py）** — `_on_connect` 前置 `if self._closed or client is not self._client: return`，关闭后的陈旧回调不再把 `_connected` 复活指向死 client。
- **⚠️ 跨重连无 in-flight 重发（client.py:466-475）** — 设计级（持久会话/消息重放会引入重复投递与 session-id 冲突风险），**deferred**，未在本次改动。

### memdb
- **✅ semantic drainer 任务静默死亡** — `semantic.py:_drain_loop` 包住 `_process_batch()` 的 `try/except`，异常计为 no-progress，M7 上限后响亮停转，不再无痕死任务。
- **✅ 空白 turn 永远跳过 → 索引永久卡死** — `count_unembedded`/`get_unembedded_docs` 新增 `_EMBEDDABLE_TEXT` 条件：user_message 与 messages 均为空/`[]` 的 turn 不再计入 unembedded，语义门能打开。回归：`test_count_unembedded_excludes_empty_turns`。
- **✅ `count_turns` fts5 分支不做 CJK 路由** — `mode == "fts5" and _contains_cjk(query)` → 改走 LIKE/grep 计数，与 `search_keyword` 一致。回归：`test_count_fts5_with_cjk_routes_to_like`。
- **✅ `load()` 在 cancel 时孤儿化** — `embeddings.py:load()` 改 `try/except/finally`：finally 必 resolve `_loading` 并置 None，CancelledError 不再锁死后续 enable()/load()。
- **✅ `replace_embedding_chunks` 原子性** — store 级 `asyncio.Lock`（`_write_lock`）串行化所有共享连接上的写者（`save_turn`/`update_summary`/`replace_embedding_chunks`/`_clear_chunks`/`clear_all_embeddings`），一个协程的 `commit()` 不再劈开另一个的多语句事务。

### agent 核心
- **✅ `_async` 后台工具打到错误会话** — `loop.py` 的 async 分支用闭包把 `_ctx.conversation` 钉到本回合会话（`_run_with_conv` 临时替换、finally 还原）。
- **✅ `count_tokens` 低估多模态** — list content 按 part 处理：text part 计字符、`image_url` part 计固定 200 token，base64 不再被当作文本占满窗口。
- **✅ 模型切换后 WeChat 持久会话仍用旧系统提示** — `ConversationStore.update_system_prompt()` 更新 `_system_prompt` 及全部现有持久会话；`reload_active_model` 调用之，并清零 `_last_usage`。
- **✅ 工具参数 JSON 损坏静默变 `{}`** — `_build_tool_calls_from_deltas` 的 `JSONDecodeError` 分支记 `warning("tool_args_malformed …")`，不再无声。
- **✅ 回滚 turn 重复保存上一条相同 turn** — `inbox.py` 内容过滤回滚时置 `rolled_back=True`，finally 的 `_on_turn_complete` 跳过保存。
- **✅ 空 assistant content 仍持久化并重发** — `openai.py` 新增 `_normalize_messages`：wire 副本中空 content 且无 tool_calls 的 assistant 消息补 `"…"` 占位（不改存储），OpenAI 格式不再 400。

### UI
- **✅ `_tool_widgets.clear()` 孤儿化进行中工具行** — clear 移到 `_process_message` 的 `finally`（回合真正结束后），跟随消息的 worker 不再清掉前一回合仍在流式的 widget。
- **✅ model picker 与审批框焦点互抢** — 双向防护：`on_tool_approval` 打开前若 picker 开着则 `_dismiss_model_picker()`（resolve None + 复位标志）；`action_switch_model` 打开 picker 前先 `_decide(False)` 关掉挂起的审批框。
- **✅ `image_utils.py:69` 文件名经 `Content.from_markup`** — `_fallback_widget` 用 `rich.markup.escape(path.name)`，`[` 不再抛 `MarkupError` 杀死整轮。
- **✅ 多条用户图片只画最后一张** — `ChatView._schedule_thumbnails` 逐 compositor 周期挂载（首个也延迟一个 gap），live 与 restore 共用 `add_user_message` 路径一并修复。
- **✅ 审批框可无限阻塞 loop；Esc 取消不清 pending 审批** — loop 新增 `_await_approval`：`asyncio.wait({approval, cancel_event})`，取消先到则 deny 并让取消流程继续；`on_tool_approval` 捕获 CancelledError 时 resolve 自身 prompt 为 denied，不留悬空 future。

### tools / 配置
- **✅ `config_env_set` 把字面 `${VAR}` 写进 `os.environ`** — 新增 `_immediate_env_value`：裸 `${VAR}` 经 `_resolve_secret`（os.environ → credstore）解析后写入 env；不可解析返回 None（不动 env）；混插走 `resolve_env`。
- **✅ `install_python_package` 无 `--` 守卫** — `create_subprocess_exec(…, "--", *packages)`，`--index-url` 之类不再被当 uv 旗标。
- **✅ sanitize 覆盖缺口：Basic auth / `github_pat_` / `Authorization: Token`** — 头模式改 `(?:Authorization\s*:\s*)?(?:Basic|Bearer|Token)\s+<token{8,}>`；新增 `\bgithub_pat_[A-Za-z0-9_]{20,}\b`。回归：`test_basic_auth_header_redacted` 等 4 个。
- **✅ sanitize 误伤合法内容** — URL 凭据模式密码类排除 `/`（`host:8080/user@domain` 不再被吞）；key=value 值类排除引号/括号/花括号（JSON 不再被截烂）。回归：`test_port_url_not_corrupted`、`test_json_api_key_not_mangled`。
- **✅ config env 值未经 sanitize 直接进日志** — `__init__.py` 全部 env 值先过 `sanitize_secrets`，再对未命中的凭证命名键（KEY/SECRET/TOKEN/PASSWORD）回退 key-name 掩码；连接串（`DATABASE_URL=postgres://user:pass@…`）只露 `<MASKED>` 密码。
- **✅ `${VAR:-default}` 绕过 credstore 回退** — `env.py:resolve_env` 的 `${VAR:-default}` 替换在默认值之前先查 credstore，文档序 "shell > credstore > literal" 成立。
- **✅ `python -m slife myconf.json5` 自定义路径被忽略** — 新增 `parse_cli_config_path`（跳过 `--headless`/`--agent <v>`）；`main()` 对显式路径（位置参数或参数）解析相对 CWD 并用其父目录作数据目录。
- **✅ POSIX 下播种配置 0644 世界可读** — `_seed_first_run_config` 复制模板后 `os.chmod(path, 0o600)`。

### subagent / wechat / memfiles
- **✅ `subagent_name` 无字符校验 → 身份注入 + 路径穿越** — `spawn` 用 `_SAFE_SUBAGENT_NAME = ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` 校验，注入的父代理既不能伪造身份行也不能 `..\` 穿出日志目录。
- **✅ 超时同步任务的迟到结果被丢弃，与承诺矛盾** — 超时改加 `_late_results`（不再进 `_cancelled`），迟到响应存入 `_async_results` 可经 `get_task_result` 取回、不自动推送；工具文案改为如实说明（preempted、不自动送达）。
- **✅ reader EOF 未扣 `_inflight` → worker 永久 busy** — `_read_stdout` finally 清零 `_inflight`，死 worker 不再永久 busy。
- **✅ 任务超时从不预占；被弃任务阻塞串行队列** — 抽 `_send_child_cancel()`（`worker/cancel` 通知），`send_task` 超时与 `cancel_task` 共用，预占卡住的任务释放队列。
- **✅ wechat 去重误删跨轮真实重复消息** — `_seen_keys` 由 `set` 改 `dict[key→monotonic]`，仅在 `_DEDUP_WINDOW=30s` 内视为重投；跨轮真实重复不再被丢弃。回归：`test_non_text_does_not_burn_key` 更新。
- **✅ memfiles SSRF 不复查重定向跳** — `url_save` 手动跟随重定向（`allow_redirects=False` + `urljoin`），每跳都重跑 `_reject_non_public_url`，链上限 5；`raw is None` 报 too many redirects。（DNS-rebinding TOCTOU 仍为已知限制。）
- **✅ memfiles tunnel 掉线后仍报 active** — `_run_monitor` 改持续健康循环：每 `_HEALTH_INTERVAL=30s` 经 `_tunnel_alive`（`ngrok.get_tunnels()`）探活，掉线则清 `_public_url` 并重启。回归：`test_restarts_when_tunnel_dropped`。

### MCP
- **✅ `disconnect()` 不串行化在途 `connect()` → 孤儿进程 + 泄漏监控** — 新增 `_disconnecting` 标志：`disconnect()` 置位后持 `_connect_lock`（等在途 connect 在其检查点中止），再取消 monitor + cleanup；`connect()` 在锁内、OAuth 后、起 monitor 前各查一次标志，中止则置 DISCONNECTED 返回，不再 spawn 孤儿 transport/monitor。

## 3. 低危（边角 / 资源增长 / 展示 / 卫生）

> ✅ 本节已全部修复（2026-08-16）。两项按信任模型接受未改：`tool_adapter` 服务器工具名/描述无转义（MCP 服务器即信任边界，名称由运维配置，描述走 JSON 不经 markup）；memfiles SSRF 的 DNS-rebinding TOCTOU（检查与抓取间的解析窗口，无法在单次 fetch 内完全消除）。

- **✅ 无界增长全部加帽** — `_usage_by_conv`（`_MAX_USAGE_CACHE=1000`，超出弹最旧，id 复用也更可能 miss 而非读脏）；`_context_turn_dates`（`_MAX_CONTEXT_DATES=5000`，保留最旧即 trim 所需）；subagent `_task_records`（500）/`_async_results`（200）/`_cancelled`/`_late_results`（各 500）；`health._entries`（200）；mcp `_stderr_buffer`（500，只读尾 20 行）。`_poll_tasks`/`merged`/memfiles `_inbound_tasks` 本轮已在上轮加帽。
- **✅ `display._show_url` / memfiles `url_save` 无字节上限** — 均改流式读取 + 50MB 硬帽；`display` 图片缓存超 1000 个文件时按 mtime 清理最旧。
- **✅ `exec._parse_input` 按第一个 `[`/`{` 切分** — 只认空格前的 `{`/`[` 为 args 分隔，路径含 `C:\code\my[2024]\run.py` 不再碎。
- **✅ `tool_display` 折叠头主参数值** — 显示前过 `sanitize_secrets`，密钥不再常显。
- **✅ mcp `client.py` 图片临时文件** — 登记 `_temp_image_files`，`disconnect()` 时删除。
- **✅ mcp `process._log_stderr` 从不 cancel** — 存 `_stderr_task`，`stop()` cancel+await。
- **✅ `connection.py:404-408` 非 SSE 探测响应不读即关** — 有界 drain body 再 aclose，连接回池，不再每轮重连漏一个。
- **✅ `connection.py` connect 无握手超时 / SSE 硬 30s** — `_request`/`_request_sse` 加 `timeout` 参数（None = 调用方治理）；`initialize` 用 `asyncio.wait_for(_CONNECT_HANDSHAKE_TIMEOUT=30s)`；SSE 响应等待不再硬 30s。
- **✅ `detect_current_shell` 过度上报 PowerShell** — cmd.exe 设 `PROMPT`、PowerShell 不设；`PROMPT` 存在 → cmd，否则看 `PSModulePath`。回归：`test_windows_cmd_launched_with_powershell_installed`。
- **✅ `tools` 段严格 `resolve_env` KeyError 中止启动** — 改 `_resolve_env_lenient`，缺 `${VAR}` 不再中止。
- **✅ `paths.is_dev()` CWD 脆弱** — 改为检查已加载 slife 包的 `__file__` 是否在 CWD 之下（源码树 = dev，site-packages = prod，即使用户在 checkout 目录里跑 wheel）。回归重写 TestIsDev。
- **✅ `bootstrap.setup_logging` 去重返回无人写的路径** — 返回已存在 FileHandler 的 `baseFilename`。
- **✅ config `mcp: "foo"` 非 dict AttributeError** — 新增 `_mcp_servers_section`（非 dict 重置为 `{}`），三处 save/remove/set_enabled 共用。
- **✅ `config.to_dict` 明文 api_key 进子代理 env** — 改经 0600 临时文件（`SLIFE_CONFIG_FILE`）传递，不再进 `/proc/<pid>/environ`；headless 优先读文件。
- **✅ wechat token 恢复不校验** — 新增 `client.validate_session()`（getupdates 探测，错误信封判无效）；恢复路径校验失败则报 not_logged_in。
- **✅ wechat typing keepalive 无回复时泄漏** — `_TYPING_MAX_LIFETIME=300s` 封顶，代理永不回复也自动停。
- **✅ memdb FTS5 snippet 取第 3 列（tags 恒空）** — 改列 0（user_message）。
- **✅ memdb schema 错误吞 DEBUG** — 改 `logger.error`。
- **✅ memdb `_call_api` 懒初始化无锁** — `_client_init_lock` 双检锁。
- **✅ memdb `get_first_provider_api_key` 把 `${VAR}` 当有效 key** — 未解析占位跳过，报"set a real API key"。
- **✅ memdb CJK `max_tokens*4` 高估** — `_char_limit_for_tokens` 按 CJK≈1 char/token、其余≈4 chars/token 估算，bge-large 的 2000 字符 CJK 不再超 512 token。
- **✅ 测试质量** — `test_ui_chat.py:18` 同义反复改为真断言（prefix/自定义 prefix/timestamp 三个用例）。

## 4. 测试与 CI

- **基线:** 1994 tests 本地全绿（修复后，1976 → 1994，新增 ~18 条回归测试）。CI 在 Ubuntu/macOS/Windows × Python 3.13 上 build wheel 后对 wheel 跑测试（wheel 确被真实执行，非旧评审的悬置项）。
- **仍未覆盖:** 无覆盖率门槛（`[tool.coverage]` 已配置但 CI 不跑 `--cov`）；无 subagent 真实子进程集成测试；无 OAuth/wechat/ngrok 真实链路集成测试；安装脚本无冒烟测试。
- **本轮新确认的"单元测试没拦住"的重灾区:** OAuth 设备码轮询用已关闭 client（HIGH）、配置读失败清空文件（HIGH）、图片 note 破坏 save_to_memory 匹配（HIGH）——都缺回归测试。

## 5. 值得保持的模式（复核未回归）

- memdb SQL 全参数化；FTS5 `diary_ai/ad/au` 触发器完整（UPDATE 存在、external-content 语法正确）；LIKE `ESCAPE '\'` 在 `search_grep`/`_search_like`/`count_turns` 三处一致（M5 修复确实到位）。
- 工具结果在 `_run_one` 全分支（含 async/denied）先 `sanitize_secrets` 再进对话与 UI；live 20% 截断与 `compact_tool_results` head/tail 数学正确。
- approval_prompt / model_picker / tool_display / chat 的 markup 处理全部走 `_lit`/`from_text(markup=False)`（UI 注入面已收口，除 image_utils 遗留）。
- MCP SSE 探测的"有响应无 endpoint 事件 → 5s 回退 Streamable"正确（C9 已修）；stdio JSON-RPC 行帧 + `_lock` 串行化安全；子进程 argv 无 shell。
- 配置写原子（temp + `os.replace` + fsync + 进程内锁 + 异常清理）；token 存 credstore（OS keyring）非明文。
- subagent 身份/连接/权限模型正确（read 线程有 EOF 守卫、超时 kill 全树、headless 共享插件双载防护）。

## 6. Repo 卫生

- `REVIEW.md` 已按本轮全新重写；README/DESIGN 中旧评审引用已全部清除。
- 源码里 ~70 处 `(REVIEW §…/C…/H…/M…)` 悬空注释已全部清理（2026-08-16），`grep REVIEW slife/` 无残留（仅 `MAX_PREVIEW_LEN` 变量名含 PREVIEW）。
- `slife.db(-wal/-shm)`、`.coverage`、`credentials.crypt`、`logs/`、`Jack.db*` 均为本地未跟踪数据，保持不提交。

## 修复进度（2026-08-16）

- **第 1 节高危：4/4 已修复** —— OAuth 轮询、配置读失败清空、坏图片 turn 不落库、presence 注入。
- **第 2 节中危：全部已修复**（a2a 8 项含 deferred 说明、memdb 5、agent core 6、UI 5、tools/config 8、subagent/wechat/memfiles 7、MCP 1），每项带回归测试。
- **第 3 节低危：全部已修复** —— 无界增长加帽、下载大小帽、exec/tool_display、MCP 临时文件/SSE/握手、shell 探测、is_dev、bootstrap 去重、config mcp 类型守卫、子代理密钥经 0600 文件、wechat 会话校验/typing 封顶、memdb snippet/schema/懒锁/占位/CJK 上限、tautological 测试。仅两项按信任模型接受未改（见第 3 节注）。
