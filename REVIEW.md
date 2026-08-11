# Slife Code Review

**日期:** 2026-08-11 · **基线:** 1776 tests pass (~25s)

只列**当前仍存在的问题**。带 *(speculative)* 的未经实机复现。

**正确性清单已清零**：14 项正确性 + 12 项一致性/死代码全部处理完（修复/清理/接受），并已回归测试。

---

## 1. 测试与 CI

- **审批对话框的 Esc 测试没挂真实 app** —— 只断言绑定解析顺序，没实际按键。→ 补端到端。
- **subagent 真实子进程从未被测** —— ready 握手 / 取消抢占运行中任务 / 响应路由 / 关闭顺序全是 mock。
- **后端线格式只单元测试** —— 未对真实 Anthropic/Responses 端点验证。
- `tests/test_tools_shell.py:144` 断言同义反复；`test_main.py:41`、`test_ui_app.py:361` 无断言（只测不抛）。
- `ci.yml` 构建 wheel 但测试从源码同步 → **wheel 从未被真正执行**；`pytest-cov`/`pytest-xdist` 装了不用；无 deselect、无覆盖率门槛；安装脚本从未冒烟；`publish.yml` 用未锁版本的 twine。

---

## 2. 值得保持的模式

- `_ensure_turn_consistent` 是正确的不变量单点，保存/加载两侧都成立；持久层可依赖它。
- 后端线格式正确（Anthropic 严格交替 + 最后 system 块缓存标记；Responses 发原生工具项）。
- `cancel_correlation` 同时抢占排队的与运行中的任务；subagent 读取循环防御到位。
- memdb 搜索全参数化（无 SQL 注入）；`sanitize_secrets` 接在入站/工具参数/出站三道口——工具结果统一过出站脱敏，known-shape 凭据到不了 LLM。
- `run_daemon` 约定整体干净；配置写入走原子 `os.replace`（`_config_io.write_config`）。
- 第三方插件现在与内置插件一样有 watchdog + 生命周期管理。
- UI 除审批对话框参数预览外全部 `markup=False`，无注入面。

---

## 3. Repo 卫生

- `Jack.db` / `slife.db`（含 `-wal`/`-shm`）是未跟踪的本地数据，保持不提交。`.coverage`、`logs/` 已忽略。
- 文档已与代码同步（工具数 52、`notify_user` 在 Display、8 个 subagent 工具）；今后改工具面时保持 README/DESIGN 同步。
- 写配置的测试必须把目标路径指向 tmp 文件（原子写入绕过 `Path.write_text` mock，直接写文件系统）——测试隔离用 patch 配置路径，不 mock 写调用。
