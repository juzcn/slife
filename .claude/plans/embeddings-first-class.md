# Embeddings 一级公民 — 顶层 `embeddings` 段 + 四个 native tools + 热加载

## Goal

把 embeddings 从 `memdb.embedding` 提升为 slife.json5 的**一级公民**：顶层
`embeddings` 段（OpenAI 兼容端点列表 + active），memdb/memfiles 共享读取；
新增四个 native tools（分类 `embeddings`）管理它，并在配置变更后热加载
（触发 memdb + memfiles 的语义 reindex）。

slife 只认 OpenAI 兼容形状（base_url + api_key），本地模型由 local-embed
插件承担（`[[local-embed-unified-openai-embeddings]]`）——slife 不碰 gguf/
transformer 后端配置。

## 新配置形状（顶层 `embeddings`，provider/model 两层级，镜像大模型）

```json5
embeddings: {
  providers: {
    "local-embed": {
      base_url: "http://127.0.0.1:8000/v1",
      api_key: "local",
      models: [ { model: "bge-m3", dim: 1024 } ]
    },
    "openai": {
      base_url: "https://api.openai.com/v1",
      api_key: "${OPENAI_API_KEY}"
    }
  },
  active_model: "local-embed/bge-m3",   // ref = provider/model，可只到 provider
  enabled: true
}
```

- **两层级镜像大模型**：provider = 端点（base_url + api_key），其下
  models 列表（每项 model + 可选 dim）。
- **`active_model` 配置权威**（端点常无 active 标记，如 OpenAI 官方
  `/v1/models` 只回 model id）：slife 用该 provider 的 base_url/api_key，
  POST `/v1/embeddings` 时带配置指定的 model。`"pid/model"` 精确到 model；
  `"pid"`（省略 model）→ fallback 端点 `/v1/models` 的 active（local-embed
  有 `active: true`）或第一个。
- **dim 获取三路径**：配置里带 → `/v1/models` 按 model id 匹配发现 →
  probe embed（`_probe_api_dim` 已有）。
- **models 列表自动发现**：`embeddings_model_list` 在线时显示端点
  `/v1/models` 真实列表 + 配置 active 标记；离线显示配置里的。
- `enabled` 全局开关（默认 true），由 `embeddings_enable` 工具管。
- 旧 `memdb.embedding` 段废弃，**全量切到顶层**（不留 fallback）。

## 五个 native tools（`slife/tools/embeddings.py`，分类 `embeddings`）

仿 `models.py` 的 `model_list/model_set/model_switch/model_remove` 镜像，
加一个全局开关工具（与大模型不同——embeddings 需要独立 enable/disable）：

- `embeddings_model_list` — 列出所有 provider + models（镜像
  `model_list`）：provider 显示 base_url/api_key 是否已设（不显示明文），
  models 在线时从端点 `/v1/models` 发现 + 配置 active 标记（★）。`NO_PARAMS`。
- `embeddings_probe` — 探测 **active provider**：同时列出配置里的
  models 和端点 `GET /v1/models` 实时返回的模型（含 dimension、active
  标记），active 模型标 ★。`NO_PARAMS`（只对 active provider）。
- `embeddings_model_set` — **upsert** 一个 provider 下的 model：参数
  `provider`（端点 id，新建时需 base_url+api_key）、`model`（api model id）、
  `base_url`、`api_key`、`dim?`（镜像 `model_set`）。
- `embeddings_model_switch` — 设 `embeddings.active_model = ref`
  （`"pid/model"` 或 `"pid"`，校验存在）。
- `embeddings_model_remove` — 删 provider 下的 model（或删整个 provider）；
  **不能删 active_model**（先 switch 到别的）。删空 providers 后整体移除
  `embeddings` 段。
- `embeddings_enable(enabled: bool)` — 全局开关：写 `embeddings.enabled`，
  **persist + 热加载**：true → 两插件 `manager.enable()`（重建）；false →
  两插件 `manager.disable()`（停 drainer、gate 关、向量保留）。

## 实现步骤

### 1. `slife/config.py` — `MemdbConfig` 瘦身 + `EmbeddingsConfig`

- `MemdbConfig` 删 `embedding_model` / `embedding_dim` / from_dict 的
  embedding 解析（memdb 段不再管 embedding）。
- 新增 `EmbeddingsConfig` dataclass（`providers: dict[str, dict]`,
  `active_model: str`, `enabled: bool`），`from_dict` 解析顶层 `embeddings`
  段（provider/model 两层级）；挂 `Config.embeddings_config`。

### 2. `slife/plugins/memdb/embedding_config.py` — 重写读顶层

五个函数按新语义重写（保留名字，换读顶层 `embeddings`）：

- `read_embedding_config()` → 读顶层 `embeddings` 段 dict
  （`{models, active, enabled}`），无则 None。
- `write_embedding_config(cfg)` → 写顶层 `embeddings` 段。
- `make_check_report()` → 基于 active 端点 + `EmbeddingClient` 探测生成状态
  （backend 恒 `api`）。
- `get_first_provider_api_key()` / `validate_gguf_path()` → **删除**
  （OpenAI 统一后无 provider 回退、无 gguf 校验）。

### 3. `slife/plugins/memdb/embeddings.py` — `from_config` 读顶层

- `from_config` 改为：读顶层 `embeddings.models[active]` →
  `{base_url, api_key, dim}`。
- 删除 gguf/transformer 分支解析、provider 回退、`_KNOWN_MODELS` 猜测逻辑
  （api 后端统一）。`_check_runtime("api")` 保留（openai 包存在性）。
- `_discover_model()` 保留（/v1/models → active model + dim）。
- `__init__` 的 gguf/transformer 分支保留为 legacy fallback（`slife[gguf]`
  extras 文档同步说明），但 `from_config` 不再产生它们。

### 4. 新 native tools — `slife/tools/embeddings.py`

- 共享 `_EmbeddingsConfigTool`（仿 `_ModelConfigTool`）：`_ConfigPathMixin`
  + `read_config`/`write_config` + `_ctx`。
- 工具各自的 `execute`：读顶层 `embeddings` → 增删改 → `write_config` →
  返回 JSON/text 结果。
- **热加载 hook**：持久化成功后，经 `_ctx` 的插件 client 触发两插件 reindex
  （见步骤 5），失败降级为"重启后生效"，不阻断持久化。

### 5. 热加载 — 触发 memdb + memfiles reindex

难点确认：两个插件都有 `SemanticManager.enable()`（停 drainer → vec0 原地
迁移 → 重启 drainer 重建）和 `disable()`（停 drainer、gate 关、向量保留），
但没有 harness 可触达的 internal 入口。方案：

- memdb：新增 `@mcp.tool(name="__memory_reload_semantic")`，参数 `enabled: bool`
  （True → `await _manager.enable()` 重读配置；False → `await _manager.disable()`）。
- memfiles：新增 `@mcp.tool(name="__memfiles_reload_semantic")` → 同上。
- native tool 持久化后，通过 client 调用这两个入口：
  - `ToolContext` 加 `memdb_client` 字段（仿 `memfiles_client`），
    `service.py` 在 spawn/restart 时接线（561/598 附近仿照 local-embed）。
  - memfiles 已有 `_ctx.memfiles_client`。
  - 调用失败降级为提示"重启后生效"，不阻断持久化。

### 6. 引用同步

- `slife/tools/system.py` `check_memdb`：hint 里删 `semantic_index_config`，
  改指新工具（`embeddings_model_*`）；读顶层配置。
- `slife/plugins/memdb/server.py` / `semantic.py`：docstring/hint 里已删的
  工具名清理；`SemanticManager.start()` 经 `read_embedding_config` 自动读顶层。
- `slife/plugins/memfiles/server.py` `memfiles_semantic_status`：hint 改指
  新工具。
- `slife.json5` / `slife.template.json5`：`memdb.embedding` → 顶层
  `embeddings` 段。
- README / README.zh-CN / DESIGN.md：工具表 + Embedding 章节更新。

### 7. 测试

- `tests/test_embedding_config.py`：改写为读顶层 `embeddings`；
  删 `get_first_provider_api_key` / `validate_gguf_path` 用例。
- `tests/test_memdb_embeddings.py`：`from_config` 用例改顶层形状；
  gguf/transformer/provider-fallback 用例删除或改写。
- `tests/test_system_health.py`：`check_memdb` patch 点改 `read_embedding_config`
  返回值形状。
- 新增 `tests/test_embeddings_tools.py`：list/set/switch/remove/enable 行为 + 热加载调用。
- `tests/test_memdb_semantic.py` / `test_memdb_server.py`：确认不受影响。

## 验证

- `uv run pytest` 全绿。
- `grep -rn "memdb.embedding\|semantic_index_config\|semantic_search_enable" slife/ tests/`
  只剩历史注释/文档。
- 手动：`embeddings_model_set` → 两插件 reindex → `memdb_semantic_status`
  显示新端点 active。

## 风险

- **配置形状迁移**：老 `memdb.embedding` 用户需手动搬一次（全量切，不留
  fallback）——与"一级公民"决策一致。
- **热加载竞态**：enable() 是 blocking，native tool 串行调用两个 client，
  失败降级为重启生效。
- **`EmbeddingClient` 精简**：删 gguf/transformer 解析会动现有测试面，需
  同步删改；`slife[gguf]` legacy extras 文档同步说明。
