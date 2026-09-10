# cc-switch

根据保存的 provider/model 配置生成 `~/.claude/settings.json` ——
一个小型 CLI，镜像了 [credstore](https://github.com/juzcn/slife/blob/main/credstore/README.md) 的模式。

非敏感的 provider *形状*（shape）存放在 `~/.claude/cc-switch.json`。API
密钥**绝不**存在这里——配置只保留密钥的*名称*，实际值在 activate 时从
credstore 读取并作为 `ANTHROPIC_AUTH_TOKEN` 注入系统环境。生成的
`settings.json` 不包含任何凭据行。

## 安装

cc-switch 是独立的 PyPI 包——与 slife 分开安装（安装 slife **不会**带上
cc-switch，反之亦然）。两者都依赖 [credstore](https://github.com/juzcn/slife/blob/main/credstore/README.md)，会自动拉取。

```bash
uv tool install --force cc-switch
# 或在本仓库内（开发环境）：
uv sync
```

一键安装脚本（必要时先装 `uv`，再 `uv tool install --force cc-switch`）：

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.ps1 | iex"
```

安装脚本和 `--force` 形式都是**幂等**的——首次运行安装，重跑即**更新**到
PyPI 最新版。不带 `--force` 的 `uv tool install cc-switch` 在工具已安装时
是空操作。卸载用 `uninstall.sh` / `uninstall.ps1`（`~/.claude/cc-switch.json`
下的配置会保留）。

## 命令

### `cc-switch set <provider-name>`

创建或编辑一个 provider。依次提示：

- **Base URL**（必填）
- **API key name**——保存密钥的 credstore key（必填）
- **Supported models**（可选；逗号、空格或分号分隔）

provider 已存在则*编辑*，否则*新增*。编辑时空输入（回车）等于保留原值。
models 提示对当前列表做**对称差**（toggle）：输入中已存在的模型被
**移除**，不存在的被**加入**。空输入保留当前列表，再次输入相同列表即撤销
上次改动。

```bash
cc-switch set deepseek
# Base URL [..]: https://api.deepseek.com/anthropic
# API key name (credstore key): DEEPSEEK_API_KEY
# Supported models (comma separated; toggles against the current list): deepseek-chat,deepseek-reasoner
```

密钥值从不被询问或写入——先用下面的命令存入：

```bash
credstore set DEEPSEEK_API_KEY
```

### `cc-switch activate <provider-name/model-name>`

用标准的 Claude Code 默认值写入 `~/.claude/settings.json`，并把密钥从
credstore 注入系统环境为 `ANTHROPIC_AUTH_TOKEN`（镜像 `credstore inject`——
Windows 注册表，Unix shell 配置文件），新启动的 Claude Code 会话即可继承。
settings 文件中**不含**任何凭据。

先写入 `settings.json`，再从 credstore 解析密钥。若密钥缺失——或 credstore
本身未安装——`activate` 会大声报错并给出相应提示，settings 文件已留在磁盘上
但**不会**注入 token。

成功后，被激活的 `provider/model` 会记录在 `~/.claude/cc-switch.json` 顶层的
`active` 字段中，列表视图即可显示当前激活的是哪个模型。

```bash
cc-switch activate deepseek/deepseek-chat
```

省略模型时：provider 只有一个模型则直接使用；有多个则提示选择。

### `cc-switch activate <provider-name/model-name> --custom`

与 `activate` 相同，但会先让你逐个覆盖每个模型槽位——
`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、
`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_CLAUDE_CODE_SUBAGENT_MODEL`、
`ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL`——一次一个。输入值即覆盖，回车保留
默认。主模型槽位（`ANTHROPIC_MODEL`）在命令行上选定，不在此处覆盖。
覆盖是一次性的：从不改动存储的 provider 配置。

### `cc-switch`

不带任何命令时，每行列出所有已配置的 provider/model 对。当前激活的模型
标记为 `(active)`：

```
deepseek/deepseek-chat
deepseek/deepseek-reasoner
scnet/scnet-1m  (active)
```

### `cc-switch list`

显示 provider 元数据（base URL、API key name、models）——每个 provider
一块。

### `cc-switch remove <provider-name>`

删除一个 provider 配置。不影响 credstore 或 settings.json。

## 文件

| 路径 | 用途 |
|------|------|
| `~/.claude/cc-switch.json` | Provider/model 形状 + `active` 字段（无密钥） |
| `~/.claude/settings.json` | 由 `activate` 生成 |
| credstore（`DEEPSEEK_API_KEY`，…） | 实际的 API 密钥值 |

`CC_SWITCH_FILE` 可覆盖配置文件路径；settings 路径可为测试覆盖。

## 安全说明

- 配置文件只保存元数据——API 密钥按名称引用。
- `activate` 从 credstore 读取密钥并注入系统环境为 `ANTHROPIC_AUTH_TOKEN`
  （Windows 注册表 / Unix shell 配置文件），镜像 `credstore inject`；绝不
  写入 settings.json。
- provider 的 `extra_env`（通过 Python API 设置）不得包含
  `ANTHROPIC_MODEL`、`ANTHROPIC_BASE_URL` 或 `ANTHROPIC_AUTH_TOKEN`——
  这些槽位归 cc-switch 所有，密钥绝不能进入明文配置。
- 在 TTY 上，`activate` 打印激活提示而不回显密钥；stdout 被管道化时输出
  供 `eval` 使用的 shell 导出行。
- 激活后重启 shell（或新开终端）当前会话的环境变更才会生效。
- 密钥是不可变的 Python `str`——cc-switch 沿用 credstore 的做法，用完后立即
  `del` 引用。
