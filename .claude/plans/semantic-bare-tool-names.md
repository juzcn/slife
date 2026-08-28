# 内置插件工具统一为语义自洽裸名

## Goal

把所有内置插件 LLM-visible 工具统一注册为**语义自洽的裸名**——名字反映工具实际操作的
对象,不靠插件前缀(`memdb__` / `memfiles__` / `sharefile__` / `media__`)消歧,也去掉
误导性的 `memory_*` 词。只有**外部 MCP server**(EXTERNAL route)的工具保留
`{server}__{tool}` 前缀。

用户的原则:"裸名要像原生工具名称一样",且 `memory_search` 实际是 `turn_search`——
名字必须表达真实语义。

## 命名映射(最终)

### memdb — turns 操作(A 类, `memory_*` → `turn_*`)
| 现名 | 新名 | 语义 |
|---|---|---|
| `memory_list_turns` | `turn_list` | 列 turns |
| `memory_search` | `turn_search` | 搜 turns |
| `memory_open` | `turn_read` | 打开一个 turn 完整内容 |
| `memory_count` | `turn_count` | 统计 turns |
| `memory_token_usage` | `turn_token_usage` | 每 turn token 用量 |
| `memory_turn_summarize` | `turn_summarize` | 给 turn 加摘要 |

### memdb — 语义索引管理(B 类, `semantic_*` 前缀)
| 现名 | 新名 | 语义 |
|---|---|---|
| `memory_check_embedding` | `semantic_index_status` (later `memdb_semantic_status`) | 查语义索引状态 |
| `memory_set_embedding` | `semantic_index_config` | 配置嵌入后端 |
| `memory_set_enabled` | `semantic_search_enable` | 开关语义搜索 |

### memfiles
| 现名 | 新名 | 语义 |
|---|---|---|
| `search` | `cabinet_search` | 搜笔记/日记/文件柜 |
| `read` | `cabinet_read` | 按相对路径读文件柜文件 |
| `embedding_check` | `cabinet_embedding_check` (later `memfiles_semantic_status`) | 文件柜索引嵌入状态 |
| `note_save` `diary_write` `file_save` `url_save` | 保持 | |
| `note_list` `diary_list` `note_read` `diary_read` `list_files` | 保持 | |

### sharefile / media / mcp / wechat / a2a
- `share_file` / `generate_image` `generate_video` `text_to_speech` `transcribe_audio`
  / `mcp_set` … / `wechat_login` … / `a2a_send_task` … — 全部保持(已自洽)。

## 实现要点

### 1. 注册层 — `slife/mcp/tool_adapter.py`
`MCPProxyTool.__init__` 的命名逻辑改为:
- `ProxyRoute.EXTERNAL` → `{server}__{tool}`(保持)
- `ProxyRoute.DIRECT` / `WRAPPER`(内置插件)→ **裸名**(`tool_name` 原样,不再加
  `{server}__`,也不再依赖"已带 `{server}_` 前缀"判断)
- 删掉/简化现有的 `startswith(f"{self._server}_")` 分支

### 2. 插件注销机制 — `slife/agent/plugins.py` + `slife/agent/service.py`
裸名后 `unregister_by_prefix(f"{self.name}_")` 失效(memdb/memfiles/media/sharefile
的工具名不再含 `{plugin}_` 前缀)。改为**按插件记录工具名集合**:
- `PluginLifecycle` 增加 `registered_tools: set[str]`
- 注册时(`_register_plugin_tools` / `PluginLifecycle.spawn`)把 `proxy_tools` 的
  `tool.name` 记入 `self._plugins[name].registered_tools`
- watchdog 死进程清理 + stop 时按**精确集合** `unregister(name)` 逐个删,不再用前缀
- mcp/wechat/a2a 的工具名本身带 `{plugin}_` 前缀(mcp_set / wechat_login /
  a2a_send_task),`unregister_by_prefix(f"{self.name}_")` 仍能匹配它们——但统一改为
  集合删除更干净。外部 MCP server 的 `{name}__` 前缀注销(service.py 1349/1358/1373/1380)
  **保持不动**(它们处理外部 server)。

### 3. 工具定义改名 — 插件 server.py
- `slife/plugins/memdb/server.py`:9 个 `@mcp.tool(name=...)` + 函数名 + 内部 docstring
  引用 + 顶部模块 docstring(工具列表)+ `instructions` 里的工具名
- `slife/plugins/memfiles/server.py`:`search`→`cabinet_search`,
  `read`→`cabinet_read`, `embedding_check`→`cabinet_embedding_check` (later `memfiles_semantic_status`)
  (函数名、docstring、`instructions` 同步)
- 函数名也一并改(`async def memory_search` → `async def turn_search`),保持 py 内一致。

### 4. harness 内部识别 — `slife/agent/service.py`
- `_extract_turn_annotation`:`name.split("__")[-1] != "memory_turn_summarize"` →
  `!= "turn_summarize"`(裸名后 `split("__")[-1]` 仍等于 `turn_summarize`,逻辑不变,
  只更新匹配串)
- 其他注释/日志里的 `memory_*` / `memdb__*` / `memfiles__*` 引用同步。
- `_register_plugin_tools` docstring(提到 `server__tool` 前缀)更新。

### 5. 系统提示词 — `slife/agent/templates/slife.j2`
- `memdb__memory_search` → `turn_search`
- `memfiles__note_save` / `memfiles__diary_write` / `memfiles__file_save` /
  `memfiles__url_save` → 裸名
- `memfiles__search` → `cabinet_search`
- `sharefile__share_file` → `share_file`

### 6. 其他 slife/ 引用
- `slife/tools/system.py`:`memory_set_embedding` → `semantic_index_config` 等引用
- `slife/tools/vision.py`:`sharefile__share_file` → `share_file`
- `slife/tools/factory.py`:注释里的 `memfiles__* / sharefile__*`
- `slife/server_utils.py`:`sharefile__share_file` 例子 → `share_file`
- `slife/plugins/memdb/embedding_config.py` / `semantic.py`:docstring/提示里的
  `memory_set_embedding` 等
- `slife/ui/restore.py`:`memory_search` → `turn_search`
- `slife/plugins/memfiles/server.py` / `__init__.py`:docstring 里的 `memfiles__*`
- `slife/plugins/sharefile/server.py` / `__init__.py`:`sharefile__share_file` → `share_file`
- `slife/plugins/__init__.py` docstring

### 7. 测试
- `tests/test_memdb_server.py`:调用名更新(`srv.memory_search` → `srv.turn_search` 等)
- `tests/test_memfiles_plugin.py`:`plugin.search` → `plugin.cabinet_search` 等
- `tests/test_agent_service.py`:`memdb__memory_turn_summarize` /
  `memdb__memory_search` → `turn_summarize` / `turn_search`
- 新增/更新:验证内置插件注册为裸名,外部 MCP 仍带 `{server}__` 前缀
- watchdog 注销测试:改为集合删除

### 8. 文档
- `README.md` 工具表:`memdb__*` / `memfiles__*` / `sharefile__*` → 裸名
- `DESIGN.md` 工具表 + memfiles/sharefile 章节

## 验证

- `uv run pytest` 全绿
- `grep -rn "memdb__\|memfiles__\|sharefile__\|media__\|memory_search\|memory_open" slife/ tests/`
  只剩:内部 `__memory_*` 工具、`memory_*` 历史注释/脚本(不涉及运行时契约)
- 手动确认:内置插件工具裸名注册,外部 MCP server 工具带 `{server}__` 前缀

## 风险

- **LLM 调用契约变更**:模型记住的 `memdb__memory_search` 变成 `turn_search`。系统提示词
  同步更新后,新对话自然用新名。历史对话/用户习惯需接受一次性迁移。
- **命名冲突**:已确认 memdb/memfiles/media/sharefile 裸名与原生工具、mcp/wechat/a2a
  工具零冲突。但 `turn_list` / `turn_read` / `turn_count` / `turn_search` 是新的全局名,
  未来插件避免再用 `turn_*` 前缀。
- **watchdog 注销**:从前缀匹配改为集合删除,需确保所有注册路径都记录集合。
