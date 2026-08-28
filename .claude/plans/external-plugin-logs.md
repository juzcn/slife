# 外置插件日志遵从 slife 规范

## 问题

slife 启动外置插件（`local-embed`、`mcp_plugin`）时，日志文件不遵从 slife 规范：

1. **local-embed 完全没有文件日志** — 它只挂 stderr handler，从不写日志文件（本次 local-embed 启动失败时 `logs/` 下没有 `slife_local-embed.log`，失败根因无从查起）。
2. **mcp_plugin 日志落在 `~/.mcp-plugin/logs/`** — 命名固定为 `{ts}_slife_plugin.log`，service 后缀是写死的 `plugin` 而非插件名，目录也不在 slife 的日志目录下。
3. 两个外置插件都**禁止 import slife**（"must not import slife"），所以不能直接调用 `slife.server_utils.setup_server_logging`。

**根因**：内置插件直接调用 slife 的 `setup_server_logging`（→ `resolve_log_dir` → `<SLIFE_DATA_DIR>/logs`，命名 `{ts}_{agent}_{service}.log`）；外置插件各自带一份独立日志工具，无法获得 slife 的目录/命名。

## 方案：宿主侧 env 契约 + 外置插件侧尊重

沿用已有的 env 传递约定（`SLIFE_SESSION_ID`、`SLIFE_AGENT_NAME`、`SLIFE_{NAME}_PORT` 都是 slife 启动插件子进程时通过 env 注入的）——**slife 导出 `SLIFE_LOG_DIR`，外置插件解析出日志目录时优先尊重它**。standalone（CLI 直跑）时才回落各自默认目录。

---

### 1. slife 宿主侧：spawn 时注入 `SLIFE_LOG_DIR`（内置插件同步改为尊重它，保证一致）

**`slife/__init__.py`** — 主进程早于任何 spawn 设置 `os.environ["SLIFE_LOG_DIR"]`：
```python
os.environ["SLIFE_LOG_DIR"] = str(get_data_dir() / "logs")
```
（`__init__.py:72` `setup_logging` 之前；`get_data_dir()` 已算好 data_dir。）

**`slife/server_utils.py` `setup_server_logging`** — log_dir 解析改为：
```python
if log_dir is None:
    log_dir = Path(os.environ["SLIFE_LOG_DIR"]) if os.environ.get("SLIFE_LOG_DIR") else resolve_log_dir()
```
（`slife/paths.py` 也加一个 `get_logs_dir()` 尊重 `SLIFE_LOG_DIR` 的 override，`logfmt.resolve_log_dir` 的 docstring 同步说明。这样内置插件与主进程完全一致，且用户可用 `SLIFE_LOG_DIR` 自定义日志目录。）

> 本次失败场景（`wrapper_cleanup_failed pid=29016`）就是**根因日志缺失**的直接案例：local-embed 不写文件 + stderr 被 wrapper 丢弃 → 死因不明。本方案让它的失败日志落盘。

---

### 2. local-embed：补上文件日志，尊重 `SLIFE_LOG_DIR`，命名用插件名

**`local-embed/local_embed/logging.py`**
- 新增 `resolve_log_dir()`：优先 `SLIFE_LOG_DIR` env → 回落 `~/.local-embed/logs`（standalone 默认）。
- `setup_logging(level, service_name="local-embed")`：
  - 保留 stderr handler（slife 的 `_log_stderr` 过滤 + relay 依赖它）。
  - 新增 FileHandler → `resolve_log_dir() / f"{ts}_{agent}_{service_name}.log"`，其中 `agent = os.environ.get("SLIFE_AGENT_NAME", "slife")`，ts = `datetime.now().strftime("%Y%m%d_%H%M%S")`（与 slife 命名 `{ts}_{agent}_{service}.log` 一致）。
  - 文件用 `SessionFormatter` 风格格式（含 `[s=…] [r=…]`，s/rid 由进程内生成或继承 `SLIFE_SESSION_ID`）。
  - 保持幂等（可重复调用）。
- `_NOISY` 增加 mcp/fastmcp 噪音抑制（可选，保持一致）。

**`local-embed/local_embed/server.py`** — 模块级 `setup_logging()` 改为 `setup_logging(service_name="local-embed")`（文件后缀=插件名）。文档字符串同步。

> 效果：slife 启动时 → `logs/20260828_110848_slife_local-embed.log`；standalone → `~/.local-embed/logs/…`。

---

### 3. mcp_plugin：尊重 `SLIFE_LOG_DIR`，service 后缀用插件名

**`mcp-plugin/mcp_plugin/logging.py` `resolve_log_dir`**
```python
override = os.getenv("SLIFE_LOG_DIR") or os.getenv("MCP_PLUGIN_LOG_DIR")
if override:
    return Path(override)
return Path.home() / ".mcp-plugin" / "logs"
```
（slife 导出 `SLIFE_LOG_DIR` → 日志进 slife 日志目录；保留 `MCP_PLUGIN_LOG_DIR` 作为 standalone override；`~/.mcp-plugin/logs` 仍是 standalone 默认。）

**`mcp-plugin/mcp_plugin/server_runtime.py` `create_plugin_server`** — 当前 `service_suffix = name.split("-", 1)[-1]`，对 `"mcp-plugin"` 得 `"plugin"`。改为：外部可通过环境变量指定服务名，使文件命名 `{ts}_{agent}_{name}.log`（`mcp`）。具体：`service_suffix` 优先 `os.environ.get("MCP_PLUGIN_SERVICE", …)`，否则保持 `name.split("-",1)[-1]`（`"mcp-plugin"` → `"plugin"` 不变；slife 启动时导出 `MCP_PLUGIN_SERVICE=mcp` 覆盖）。命名即 `20260828_110848_slife_mcp.log`。

> mcp_plugin 的 `setup_server_logging` 已用 `configure_root_logging(..., file_path=...)`，只要 `resolve_log_dir` 尊重 `SLIFE_LOG_DIR`、service 后缀正确即可，无需改 file handler 逻辑。

---

### 4. 测试

- **slife**：`test_logfmt.py` `resolve_log_dir` 增加 `SLIFE_LOG_DIR` override 用例；`test_agent_service.py` spawn 测试不破坏（MCPWrapperProcess 已整体 mock）。
- **local-embed**：`test_logging.py`（新增或扩展）验证 `resolve_log_dir` 尊重 `SLIFE_LOG_DIR`、`setup_logging` 创建文件 handler、文件命名 `{ts}_{agent}_{service}.log`；现有 `test_server.py` / `test_engine.py` 不破坏（patch `setup_logging`）。
- **mcp-plugin**：`test_config`/`test_mcp_server` 不破坏（`setup_server_logging` 已被 patch）。`resolve_log_dir` 增加 `SLIFE_LOG_DIR` 优先级用例（如有对应测试文件）。

---

## 兼容性与边界

- **standalone 不变**：CLI 直跑 local-embed / mcp_plugin，无 `SLIFE_LOG_DIR` → 回落各自默认（`~/.local-embed/logs`、`~/.mcp-plugin/logs`）。
- **老 env 兼容**：`MCP_PLUGIN_LOG_DIR` 仍是 mcp_plugin 的 override（优先级：`SLIFE_LOG_DIR` > `MCP_PLUGIN_LOG_DIR` > 默认）。
- **不破坏外置插件独立性**：不 import slife，仅读 env（与 `SLIFE_SESSION_ID` 等同一机制）。
- **`_log_stderr` 过滤**：local-embed 的 stderr 格式 `HH:MM:SS [LEVEL] name | …` 已匹配 wrapper 的 `_SUBPROCESS_LOG` 正则（`^\d{2}:\d{2}:\d{2}\s+\[(?:DEBUG|INFO…)\]`），结构化行不会重复 relay；新增文件日志不改变 stderr 格式，无影响。
- **Windows 文件锁**：每个外置插件独立进程、独立文件，无锁竞争。
