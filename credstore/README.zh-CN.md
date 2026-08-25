# credstore

跨平台凭证存储 — 操作系统密钥链 + AES 加密文件备份。

一个独立的密钥管理器，随 [Slife](https://github.com/juzcn/slife) 一同发布，但**不依赖** Slife。仅依赖 `keyring`、`keyring-wincred` 和 `keyrings-cryptfile`。

支持 **Windows**、**macOS**、**Linux**（桌面 + 无桌面）和 **WSL**（通过 PowerShell 桥接 Windows 凭据管理器）。

## 安装

```bash
pip install credstore
# 或随 Slife 一同安装：
uv tool install git+https://github.com/juzcn/slife.git      # 海外
uv tool install git+https://gitee.com/juzcn/slife.git       # 国内
```

验证：`credstore status`

无需配置。运行 `credstore set-password` 以启用加密备份。

## CLI

### 初始化

```bash
credstore set-password    # 创建 ~/.credstore/credentials.crypt
```

路径可通过 `CREDSTORE_FILE` 环境变量或 `credstore.json5` 覆盖。

### 命令

| 命令 | 需要认证 | 说明 |
|------|---------|------|
| `set-password` | 设置密码 | 创建或修改主密码（≥8 字符） |
| `status` | — | 查看后端状态 |
| `set KEY` | 主密码 + 密钥 | 原子双写：cryptfile → 密钥链。密钥链失败时回滚 |
| `get KEY` | — | 仅密钥链，脱敏输出（`sk-5f…b722`） |
| `get KEY -p` | 主密码 | 双查询密钥链 + cryptfile，明文输出。不一致时报错 |
| `delete KEY` | 主密码 | 从两个存储中删除 |
| `copy SOURCE DEST` | 主密码 | 幂等复制（密钥链 + cryptfile）。若目标已注入环境变量则自动重新注入 |
| *（无命令）* | 主密码¹ | 三重读取：密钥链 + cryptfile + 环境变量。显示每个 key 的同步状态 |
| `inject KEY… [--shell]` | 主密码¹ | 持久化到系统环境：注册表（Win）或 shell 配置文件（Unix）。读密钥链；cryptfile-only 模式下读加密备份（询问主密码） |
| `uninject KEY… [--shell]` | — | 从系统环境中移除 |
| `reset-keyring` | 主密码 | 从 cryptfile 恢复全部 → 密钥链（灾难恢复） |
| `reset-backup` | 主密码 | 同步密钥链 → cryptfile |

¹ 仅当 cryptfile 存在时要求主密码。

### `get` 模式

| 模式 | 读取来源 | 输出 | 适用场景 |
|------|---------|------|---------|
| `get KEY` | 仅密钥链 | 脱敏 | 快速检查，可安全投屏 |
| `get KEY -p` | 密钥链 + cryptfile | 明文 | 验证一致性，管道传递给其他工具 |

`-p` 模式执行双查询一致性校验：
- 两个存储都有且一致 → 打印明文
- 一个存储缺失 → 报错并给出恢复指令
- 值不一致 → 报错，告知应执行哪个命令

**仅加密文件模式**（无系统密钥链可用——例如 Linux 上 keyctl 被安全策略屏蔽）：AES cryptfile 是唯一存储。`set`/`copy` 写入并给出提示，`status` 显示 "cryptfile-only mode"，`get -p` 直接返回 cryptfile 的值（不存在双查询不一致），`inject` 会询问主密码并从备份读取后注入环境。

> ⚠️ **仅 CLI。** Python API（`get_credential`、`exists_credential`、`resolve_uri`）在 cryptfile-only 模式下只读系统密钥链并返回 `None`，从不弹窗。依赖免密码启动解析的消费方（如 **sLife**）因此**不支持 cryptfile-only 模式**——请使用 CLI（`credstore get KEY -p`），或对 sLife 使用 shell 环境变量（完全兼容，sLife 先查 `os.environ` 再查 credstore）。

### `inject` / `uninject`

`inject` 从密钥链读取密钥并持久化到系统环境：

| 平台 | 持久化方式 | 激活 |
|------|----------|------|
| Windows | 注册表（`HKCU\Environment`）+ 广播 | 重启 shell，或 `Invoke-Expression (credstore inject KEY)` |
| Unix | Shell 配置文件（`~/.bashrc`） | 新 shell，或 `eval "$(credstore inject KEY)"` |

当 stdout 是终端时，`inject` 打印激活提示而非密钥本身。实际的导出命令仅通过管道输出。

```bash
eval "$(credstore inject DEEPSEEK_API_KEY)"           # Bash/Zsh — 立即激活
Invoke-Expression (credstore inject DEEPSEEK_API_KEY)  # PowerShell — 立即激活
```

`uninject` 执行反向操作——从注册表或配置文件中移除，并打印 unset 命令。

### 默认（无命令）输出

```
  KEY                  SYSTEM KEYRING   CRYPTFILE        ENV    STATUS
  ────────             ──────────────   ──────────────   ────   ──────
  ANTHROPIC_API_KEY    ✔                ✔                —      synced
  DEEPSEEK_API_KEY     ✔                ✔                ✔      synced
  OPENAI_API_KEY       —                ✔                —      cryptfile only
  ────────             ──────────────   ──────────────   ────   ──────
  3 credential(s) — synced: 2, cryptfile only: 1, env: 1
```

| 列 | 含义 |
|----|------|
| `SYSTEM KEYRING` | ✔ = 已存入 OS 密钥链 |
| `CRYPTFILE` | ✔ = 已存入加密备份 |
| `ENV` | ✔ = 当前已设为环境变量 |
| `STATUS` | `synced`（已同步）、`keyring only`、`cryptfile only` 或 `MISMATCH ⚠`（不一致） |

## 内存安全

密钥是不可变的 Python `str` 对象——无法原地归零。缓解措施：

1. **绝不批量加载** — `list` 仅收集 key 名称。同步比对时逐个取值并立即 `del`。
2. **优先存在性检查** — `exists_credential()` / `list_credential_keys()` 从不获取密钥内容。
3. **显式清理** — 每个 CLI 处理器在所有退出路径（含错误分支）上 `del` 密钥引用。

| 操作 | 清理方式 |
|------|---------|
| `get` / `get_credential()` | 调用者必须 `del` 返回值 |
| `set` | 双写后 `del secret` + `del master_pw` |
| `copy` | 同 `set`。幂等：目标值相同时跳过。若目标已持久化到环境则自动重新注入 |
| `list` | 逐个取值、比对、立即 `del` |
| `inject` | 取值 → 持久化 → `del`。TTY 模式：stdout 无密钥内容 |
| `reset-keyring` | 写入密钥链后逐个 `del` |
| `reset-backup` | 批量加载不可避免；同步后 `del entries` + `del master_pw` |

`masked_input()` 每次按键回显 `*`——支持粘贴，实际值从不显示。

## Python API

```python
import credstore

# 读取 / 检查 / 删除（仅系统密钥链，无需交互）
credstore.get_credential("myapp/api_key")      # → str | None
credstore.exists_credential("myapp/api_key")   # → bool  （绝不返回密钥值）
credstore.list_credential_keys()               # → list[str]  （绝不返回密钥值）
credstore.set_credential("myapp/api_key", "sk-…")
credstore.delete_credential("myapp/api_key")   # → bool

# keyring: URI 解析
credstore.is_keyring_uri("keyring:myapp/k")    # → True
credstore.resolve_uri("keyring:myapp/k")       # → 密钥值（找不到抛 KeyError）
credstore.parse_keyring_uri("keyring:srv/k")   # → ("srv", "k") | None

# Shell 格式化
credstore.format_export("KEY", "secret", "bash")   # → "export KEY='secret'"
credstore.format_unset("KEY", "bash")              # → "unset KEY"

# 诊断
credstore.check_backend()      # → {"available": True, "backend": "…", …}
credstore.get_backend_name()   # → "system keyring + cryptfile (dual-write)"
```

**Python API 仅操作系统密钥链**——无需主密码，无需交互。双写（密钥链 + cryptfile）由 CLI 层处理。

`get_credential()` 和 `resolve_uri()` 的调用者必须在用完后 `del` 返回值。仅需判断是否存在时，优先使用 `exists_credential()`。

## 配置

可选 `credstore.json5`（按 `./credstore.json5` → `~/.credstore/config.json5` 顺序查找）：

```json5
{
  // 覆盖默认 cryptfile 路径
  cryptfile_path: "/custom/path/credentials.crypt",
}
```

优先级：`CREDSTORE_FILE` 环境变量 → `credstore.json5` → `~/.credstore/credentials.crypt`（或在 Slife 开发模式下为 `./credentials.crypt`）。

## 架构

### 后端矩阵

后端选择**按平台确定性分发**——不依赖 keyring 的自动发现。仅支持以下五个后端，其它平台直接报清晰错误。

| 平台 | 后端 | 机制 |
|------|------|------|
| **Windows** | `WinVaultKeyring` | Windows 凭据管理器（Vault API，由 keyring 提供） |
| **WSL** | `WslBackend` | PowerShell → advapi32.dll CredReadW/CredWriteW（C# P/Invoke）——与 Windows 共享同一 CredMan 存储 |
| **macOS**（GUI） | `macOS.Keyring` | macOS 登录钥匙串 |
| **macOS**（无头） | `macOS.Keyring` + 隔离钥匙串 | `CREDSTORE_KEYCHAIN`（或 `~/.credstore/credentials.keychain-db`）；首次使用时自动通过 `security create-keychain` 创建 |
| **Linux** | `KeyutilsBackend` | 内核持久化 keyring（`@p`），通过 `add_key`/`keyctl` 系统调用（ctypes，零依赖） |

### 双写流程

```
┌──────────────────────────────────────────────────┐
│  CLI (__main__.py)                               │
│  交互式：masked_input()、主密码                   │
│  原子双写：cryptfile → 密钥链                     │
│  密钥链失败时回滚                                  │
├──────────────────────────────────────────────────┤
│  Python API (__init__.py)                        │
│  程序化调用：无需交互、仅操作系统密钥链             │
├────────────────────┬─────────────────────────────┤
│  系统密钥链         │  加密文件备份                │
│  （主存储）         │  （加密）                    │
│  ────────────────  │  ───────────────────────    │
│  Win 凭据管理器     │  keyrings.cryptfile         │
│  WSL（PowerShell） │  AES 加密 INI 文件           │
│  macOS 钥匙串      │  OS 密码变更不受影响          │
│  Linux keyutils    │                             │
└────────────────────┴─────────────────────────────┘
```

### WSL 后端

在 WSL 上，没有可用的 Linux 桌面密钥链。`WslBackend` 通过调用 `powershell.exe` 并嵌入 C# 代码 P/Invoke `advapi32.dll`（`CredReadW`、`CredWriteW`、`CredDeleteW`）来桥接 Windows 凭据管理器。由于直接对接 CredMan，WSL 与原生 Windows 共享同一凭据存储——任一侧 `credstore set` 的数据另一侧都能读到。`WslBackend` 按平台确定性地选中，不经过优先级竞争。

### Keyutils 后端

在 Linux（无论桌面还是无头）上，`KeyutilsBackend` 将凭据存储在内核的持久化 keyring（`@p`）中。通过 `ctypes` 直接调用 `add_key` 和 `keyctl` 系统调用——标准库外零 Python 依赖。每个凭据是一个 `"user"` 键，描述为 `"credstore:<service>/<key>"`。若内核 keyring 不可用（例如 HPC 登录节点上 seccomp 屏蔽了 keyctl），credstore 降级为**仅加密文件（cryptfile-only）模式**：`set` 存入 AES 备份并给出提示，`set-password`/`status`/`get -p`/`delete` 照常工作，原因可通过 `credstore status` 查看。完全不支持的平台仍然报错，而不是错误选择后端。

### macOS 后端

macOS 在 GUI 会话中使用 `keyring.backends.macOS.Keyring`（登录钥匙串）。对于无头 macOS（CI、服务器）——登录钥匙串交互失败（`errSecInteractionNotAllowed`）的环境——设置 `CREDSTORE_KEYCHAIN` 指向一个隔离钥匙串路径，或让 credstore 使用 `~/.credstore/credentials.keychain-db`；该文件首次使用时通过 `security create-keychain` 自动创建。

### 凭据枚举

`credstore`（默认、无命令视图）通过平台特定 API 从 OS 凭据存储读取 key：

| 平台 | API |
|------|-----|
| **Windows** | `win32cred.CredEnumerate` |
| **WSL** | `powershell.exe` + 嵌入式 C# `CredEnumerateW` 通过 `advapi32.dll` |
| **其他** | 不支持——重新运行 `credstore set <KEY>` 以填充 cryptfile |

枚举仅获取 key 名称——永远不会批量加载密钥值。同步比对时逐个取值并立即丢弃。

## 许可证

MIT
