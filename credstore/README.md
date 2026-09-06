# credstore

Cross-platform credential storage — OS keyring with AES-encrypted file backup.

A standalone secret manager that ships with [Slife](https://github.com/juzcn/slife) but has **no dependency on it**. Declares three runtime dependencies: `keyring`, `keyring-wincred`, and `keyrings-cryptfile`.

Supports **Windows**, **macOS** (GUI + headless), **Linux** (desktop + headless), and **WSL** (Windows Credential Manager via a PowerShell bridge).

## Install

Requires **Python ≥ 3.13**.

```bash
pip install credstore
# or, in an isolated environment:
uv tool install credstore
```

One-click installers (install `uv` if needed, then `uv tool install credstore`):

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.ps1 | iex"
```

Uninstall with `uninstall.sh` / `uninstall.ps1` (user data under `~/.credstore/` is left in place).

Verify: `credstore status`

No configuration needed. Run `credstore set-password` to enable encrypted backup.

## CLI

All secret entry uses masked input — each keystroke echoes `*`, paste works, the actual value is never displayed or logged.

> **Interactive terminal required.** Every command except `status` and `uninject` reads from an interactive TTY (`@requires_tty`); run them from a real terminal, not a piped shell or CI step.

### Setup

```bash
credstore set-password    # creates ~/.credstore/credentials.crypt (or change the master key)
```

Path overridable via the `CREDSTORE_FILE` env var.

### Commands

| Command | Master key | Description |
|---------|-----------|-------------|
| `set-password` | sets it | Create or change the master key (≥8 chars) |
| `status` | — | Show backend health |
| `set KEY` | required¹ | Atomic dual-write: cryptfile → keyring. Rolls back on keyring failure |
| `get KEY` | — | Keyring only, masked output (`sk-…b722`) |
| `get KEY -p` | prompted | Dual-query keyring + cryptfile, plaintext. Fails on mismatch |
| `remove KEY` | prompted² | Remove from both stores (best-effort) |
| `copy SOURCE DEST` | required¹ | Idempotent copy (keyring + cryptfile). Re-injects dest to env if previously injected |
| *(no command)* | prompted³ | Triple-read: keyring + cryptfile + env. Shows sync status per key |
| `inject KEY… [--shell]` | prompted³ | Persist to system env: registry (Win) or shell profile (Unix). Reads keyring; in cryptfile-only mode reads the backup (prompts master pw) |
| `uninject KEY… [--shell]` | — | Remove from system env |
| `reset-keyring` | prompted | Restore all from cryptfile → keyring (disaster recovery) |
| `reset-backup` | required¹ | Sync keyring → cryptfile |

¹ `set`, `copy`, and `reset-backup` are gated up front — they refuse to run until the master key has been set (`credstore set-password`).
² `remove` prompts for the master key to clean the cryptfile copy; the keyring copy is removed regardless.
³ Master password prompted only if a cryptfile exists.

`--shell` accepts `auto` (default), `bash`, `powershell`, or `cmd`.

### `get` Modes

| Mode | Reads from | Output | Use case |
|------|-----------|--------|----------|
| `get KEY` | Keyring only | Masked | Quick check, safe for screen sharing |
| `get KEY -p` | Keyring + cryptfile | Plaintext | Verify consistency, pipe to another tool |

`-p` mode performs a dual-query consistency check:

- Both stores have the value AND they match → prints plaintext
- One store missing → error with recovery instructions
- Values differ → error, tells you which tool to run

**Cryptfile-only mode** (no system keyring available — e.g. Linux where keyctl is blocked by policy): the AES cryptfile is the sole store. `set`/`copy` write there with a notice, `status` reports "cryptfile-only mode", and `get -p` returns the cryptfile value directly (no dual-query mismatch possible).

> ⚠️ **CLI-only.** The Python API (`get_credential`, `exists_credential`, `resolve_uri`) reads the system keyring only and returns `None` in cryptfile-only mode — it never prompts for the master password. Consumers that rely on password-free startup resolution (like **sLife**) therefore **do not support cryptfile-only mode** — use the CLI (`credstore get KEY -p`) or, for sLife, shell environment variables (fully supported, since sLife checks `os.environ` before credstore).

### `inject` / `uninject`

`inject` reads a secret from the keyring and persists it to the system environment:

| Platform | Persistence | Activation |
|----------|-------------|------------|
| Windows | Registry (`HKCU\Environment`) + broadcast | Restart shell, or `Invoke-Expression (credstore inject KEY)` |
| Unix | Shell profile (`~/.bashrc`) | New shell, or `eval "$(credstore inject KEY)"` |

When stdout is a TTY, `inject` prints an activation hint instead of the secret. The actual export command only flows through a pipe.

```bash
eval "$(credstore inject DEEPSEEK_API_KEY)"           # Bash/Zsh — activate now
Invoke-Expression (credstore inject DEEPSEEK_API_KEY)  # PowerShell — activate now
```

`uninject` reverses the operation — removes from registry or profile and prints the unset command.

### Default (bare) Output

```
  KEY                  SYSTEM KEYRING   CRYPTFILE        ENV    STATUS
  ────────             ──────────────   ──────────────   ────   ──────
  ANTHROPIC_API_KEY    ✔                ✔                —      synced
  DEEPSEEK_API_KEY     ✔                ✔                ✔      synced
  OPENAI_API_KEY       —                ✔                —      cryptfile only
  ────────             ──────────────   ──────────────   ────   ──────
  3 credential(s) — synced: 2, cryptfile only: 1, env: 1
```

| Column | Meaning |
|--------|---------|
| `SYSTEM KEYRING` | ✔ = stored in OS keyring |
| `CRYPTFILE` | ✔ = stored in encrypted backup |
| `ENV` | ✔ = currently set as environment variable |
| `STATUS` | `synced`, `keyring only`, `cryptfile only`, or `MISMATCH ⚠` |

> On **macOS** and **Linux** the system keyring cannot be enumerated (no list API), so the `SYSTEM KEYRING` column is derived from the cryptfile and env. Only **Windows** and **WSL** enumerate Credential Manager directly.

## Memory Safety

Secrets are immutable Python `str` objects — they cannot be zeroed in place. Mitigations:

1. **Never batch-load** — `list` collects only key names. Sync comparison fetches one value at a time and immediately `del`s it.
2. **Prefer existence checks** — `exists_credential()` / `list_credential_keys()` never return secret content.
3. **Explicit cleanup** — every CLI handler `del`s secret references on all exit paths including error branches.

| Operation | Cleanup |
|-----------|---------|
| `get` / `get_credential()` | Caller must `del` the returned value |
| `set` | `del secret` + `del master_pw` after dual-write |
| `copy` | Same as `set`. Idempotent: skips if dest matches source. Re-injects dest to env if previously persisted |
| `list` | Values fetched one-at-a-time, compared, `del`ed immediately |
| `inject` | Value read → persisted → `del`ed. TTY: no secret on stdout |
| `reset-keyring` | Each value `del`ed after writing to keyring |
| `reset-backup` | Batch load unavoidable; `del entries` + `del master_pw` after sync |

`masked_input()` echoes `*` per keystroke — paste works, actual value never displayed.

## Python API

```python
import credstore

# Read / check / delete (system keyring only, no prompt)
credstore.get_credential("myapp/api_key")      # → str | None
credstore.exists_credential("myapp/api_key")   # → bool  (NEVER returns secret)
credstore.list_credential_keys()               # → list[str]  (NEVER returns values)
credstore.set_credential("myapp/api_key", "sk-…")
credstore.delete_credential("myapp/api_key")   # → bool

# keyring: URI resolution
credstore.is_keyring_uri("keyring:myapp/k")    # → True
credstore.resolve_uri("keyring:myapp/k")       # → the secret value (or KeyError)
credstore.parse_keyring_uri("keyring:srv/k")   # → ("srv", "k") | None

# Shell formatting
credstore.format_export("KEY", "secret", "bash")   # → "export KEY='secret'"
credstore.format_unset("KEY", "bash")              # → "unset KEY"

# Environment persistence (programmatic form of `inject` / `uninject`)
credstore.persist_key("KEY", "secret", "bash")     # registry (Win) / profile (Unix)
credstore.unpersist_key("KEY", "bash")             # remove from system environment

# Diagnostics
credstore.init_store()              # → CredentialStore (explicit lazy init)
credstore.check_backend()           # → {"available": True, "backend": "…", …}
credstore.get_backend_name()        # → "system keyring + cryptfile (dual-write)"
```

**Python API talks to system keyring only** — no master password, no prompt. Dual-write (keyring + cryptfile) is handled by the CLI. Two exceptions worth noting:

- `set_credential()` writes only to the system keyring, but it still **requires the master key to have been set** (`credstore set-password` run once — the cryptfile must exist) and raises `RuntimeError` otherwise. This is deliberate: a secret is never written to the keyring without an encrypted backup to survive OS password changes. It never prompts.
- `get_credential()` and `resolve_uri()` return `None` / raise `KeyError` in cryptfile-only mode; only the CLI can read the encrypted backup.

Callers of `get_credential()` and `resolve_uri()` must `del` the returned value after use. Prefer `exists_credential()` when you only need to know if a credential exists. `reset_credentials()` exists internally for the CLI (`reset-keyring`) but is not part of the public package API.

## Configuration

The encrypted credential file's path is resolved by precedence:

1. `CREDSTORE_FILE=<path>` (env var)
2. `./credentials.crypt` (dev — when the current directory is the Slife source
   root, i.e. its `pyproject.toml` has `project.name == "slife"`)
3. `~/.credstore/credentials.crypt` (default, standalone use)

## Architecture

### Backend Matrix

Backend selection is **deterministic by platform** — no keyring auto-discovery. Exactly five platform configurations are supported; anything else is rejected with a clear error.

| Platform | Backend | Mechanism |
|----------|---------|-----------|
| **Windows** | `WinVaultKeyring` | Windows Credential Manager (Vault API, via keyring) |
| **WSL** | `WslBackend` | PowerShell → advapi32.dll CredReadW/CredWriteW (C# P/Invoke) — same CredMan store as Windows |
| **macOS** (GUI) | `macOS.Keyring` | macOS login keychain |
| **macOS** (headless) | `macOS.Keyring` + isolated keychain | `CREDSTORE_KEYCHAIN` (or `~/.credstore/credentials.keychain-db`); auto-created via `security create-keychain` |
| **Linux** | `KeyutilsBackend` | Kernel persistent keyring (`@p`) via `add_key`/`keyctl` syscalls (ctypes, zero extra deps) |

`WslBackend` (priority 9.5) and `KeyutilsBackend` (priority 1.5) are also registered as standard `keyring.backends` entry points, so they participate correctly in keyring's own priority chain for external consumers — but credstore's own dispatch selects them directly, never by discovery.

### Dual-Write Flow

```
┌──────────────────────────────────────────────────┐
│  CLI (__main__.py)                               │
│  Interactive: masked_input(), master password     │
│  Atomic dual-write: cryptfile → keyring           │
│  Rollback on keyring failure                      │
├──────────────────────────────────────────────────┤
│  Python API (__init__.py)                        │
│  Programmatic: no prompt, system keyring only     │
├────────────────────┬─────────────────────────────┤
│  System Keyring    │  Cryptfile Backup           │
│  (primary)         │  (encrypted)                │
│  ────────────────  │  ───────────────────────    │
│  Win CredMan       │  keyrings.cryptfile         │
│  WSL (PowerShell)  │  AES-encrypted INI          │
│  macOS Keychain    │  Survives OS pw changes     │
│  Linux keyutils    │                             │
└────────────────────┴─────────────────────────────┘
```

### WSL Backend

On WSL, no Linux desktop keyring is available. `WslBackend` bridges to Windows Credential Manager by calling `powershell.exe` with embedded C# that P/Invokes `advapi32.dll` (`CredReadW`, `CredWriteW`, `CredDeleteW`). Because it targets CredMan directly, WSL and native Windows share the same credential store — `credstore set` on either side is visible on the other. It also reads the native-Windows layout (`TargetName = service`, key in the `UserName` field) so credentials written by `WinVaultKeyring` resolve correctly on WSL. Unlike platform auto-discovery, `WslBackend` is selected deterministically (no priority roulette).

### Keyutils Backend

On Linux (desktop and headless alike), `KeyutilsBackend` stores credentials in the Linux kernel's persistent keyring (`@p`). Calls `add_key` and `keyctl` syscalls directly through `ctypes` — zero Python dependencies beyond stdlib. Each credential is a `"user"` key with description `"credstore:<service>/<key>"`. Unsupported CPU architectures are rejected rather than guessed. If the kernel keyring is unavailable (e.g. keyctl blocked by seccomp on an HPC login node), credstore degrades to **cryptfile-only** mode: `set` stores in the AES backup with a notice, `set-password`/`status`/`get -p`/`remove` keep working, and the reason is visible in `credstore status`. A fully unsupported platform still raises rather than picking a wrong backend.

### macOS Backend

macOS uses `keyring.backends.macOS.Keyring` (the login keychain) in GUI sessions. For headless macOS (CI, servers) — where login-keychain interaction would fail with `errSecInteractionNotAllowed` — set `CREDSTORE_KEYCHAIN` to an isolated keychain path, or let credstore use `~/.credstore/credentials.keychain-db`; the file is created automatically via `security create-keychain` on first use.

### Credential Enumeration

`credstore` (the default, no-command view) reads keys from the OS credential store using platform-specific APIs:

| Platform | API |
|----------|-----|
| **Windows** | `win32cred.CredEnumerate` |
| **WSL** | `powershell.exe` + inline C# `CredEnumerateW` via `advapi32.dll` |
| **Other** | Unsupported — only cryptfile + env are listed |

Enumeration retrieves key names only — secret values are never batch-loaded. Sync comparison fetches one value at a time and immediately discards it.

## License

MIT
