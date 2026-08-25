# credstore

Cross-platform credential storage — OS keyring with AES-encrypted file backup.

A standalone secret manager that ships with [Slife](https://github.com/juzcn/slife) but has **no dependency on it**. Depends only on `keyring`, `keyring-wincred`, and `keyrings-cryptfile`.

Supports **Windows**, **macOS**, **Linux** (desktop + headless), and **WSL** (Windows Credential Manager via PowerShell bridge).

## Install

```bash
pip install credstore
# or bundled with Slife:
uv tool install git+https://github.com/juzcn/slife.git
```

Verify: `credstore status`

No configuration needed. Run `credstore set-password` to enable encrypted backup.

## CLI

### Setup

```bash
credstore set-password    # creates ~/.credstore/credentials.crypt
```

Path overridable via `CREDSTORE_FILE` env var or `credstore.json5`.

### Commands

| Command | Auth | Description |
|---------|------|-------------|
| `set-password` | sets it | Create or change master key (≥8 chars) |
| `status` | — | Show backend health |
| `set KEY` | master + secret | Atomic dual-write: cryptfile → keyring. Rolls back on keyring failure |
| `get KEY` | — | Keyring only, masked output (`sk-5f…b722`) |
| `get KEY -p` | master | Dual-query keyring + cryptfile, plaintext. Fails on mismatch |
| `delete KEY` | master | Remove from both stores |
| `copy SOURCE DEST` | master | Idempotent copy (keyring + cryptfile). Re-injects dest to env if previously injected |
| *(no command)* | master¹ | Triple-read: keyring + cryptfile + env. Shows sync status per key |
| `inject KEY… [--shell]` | master¹ | Persist to system env: registry (Win) or shell profile (Unix). Reads keyring; in cryptfile-only mode reads the backup (prompts master pw) |
| `uninject KEY… [--shell]` | — | Remove from system env |
| `reset-keyring` | master | Restore all from cryptfile → keyring (disaster recovery) |
| `reset-backup` | master | Sync keyring → cryptfile |

¹ Master password required only if cryptfile exists.

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

## Memory Safety

Secrets are immutable Python `str` objects — they cannot be zeroed in place. Mitigations:

1. **Never batch-load** — `list` collects only key names. Sync comparison fetches one value at a time and immediately `del`s it.
2. **Prefer existence checks** — `exists_credential()` / `list_credential_keys()` never retrieve secret content.
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

# Diagnostics
credstore.check_backend()      # → {"available": True, "backend": "…", …}
credstore.get_backend_name()   # → "system keyring + cryptfile (dual-write)"
```

**Python API talks to system keyring only** — no master password, no prompt. Dual-write (keyring + cryptfile) is handled by the CLI.

Callers of `get_credential()` and `resolve_uri()` must `del` the returned value after use. Prefer `exists_credential()` when you only need to know if a credential exists.

## Configuration

Optional `credstore.json5` (searched at `./credstore.json5` then `~/.credstore/config.json5`):

```json5
{
  // Override default cryptfile path
  cryptfile_path: "/custom/path/credentials.crypt",
}
```

Priority: `CREDSTORE_FILE` env var → `credstore.json5` → `~/.credstore/credentials.crypt` (or `./credentials.crypt` in Slife dev mode).

## Architecture

### Backend Matrix

Backend selection is **deterministic by platform** — no keyring auto-discovery. Exactly five backends are supported; anything else is rejected with a clear error.

| Platform | Backend | Mechanism |
|----------|---------|-----------|
| **Windows** | `WinVaultKeyring` | Windows Credential Manager (Vault API, via keyring) |
| **WSL** | `WslBackend` | PowerShell → advapi32.dll CredReadW/CredWriteW (C# P/Invoke) — same CredMan store as Windows |
| **macOS** (GUI) | `macOS.Keyring` | macOS login keychain |
| **macOS** (headless) | `macOS.Keyring` + isolated keychain | `CREDSTORE_KEYCHAIN` (or `~/.credstore/credentials.keychain-db`); auto-created via `security create-keychain` |
| **Linux** | `KeyutilsBackend` | Kernel persistent keyring (`@p`) via `add_key`/`keyctl` syscalls (ctypes, zero deps) |

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

On WSL, no Linux desktop keyring is available. `WslBackend` bridges to Windows Credential Manager by calling `powershell.exe` with embedded C# that P/Invokes `advapi32.dll` (`CredReadW`, `CredWriteW`, `CredDeleteW`). Because it targets CredMan directly, WSL and native Windows share the same credential store — `credstore set` on either side is visible on the other. Unlike the platform auto-discovery it replaces, `WslBackend` is selected deterministically (no priority roulette).

### Keyutils Backend

On Linux (desktop and headless alike), `KeyutilsBackend` stores credentials in the Linux kernel's persistent keyring (`@p`). Calls `add_key` and `keyctl` syscalls directly through `ctypes` — zero Python dependencies beyond stdlib. Each credential is a `"user"` key with description `"credstore:<service>/<key>"`. If the kernel keyring is unavailable (e.g. keyctl blocked by seccomp on an HPC login node), credstore degrades to **cryptfile-only** mode: `set` stores in the AES backup with a notice, `set-password`/`status`/`get -p`/`delete` keep working, and the reason is visible in `credstore status`. A fully unsupported platform still raises rather than picking a wrong backend.

### macOS Backend

macOS uses `keyring.backends.macOS.Keyring` (the login keychain) in GUI sessions. For headless macOS (CI, servers) — where login-keychain interaction would fail with `errSecInteractionNotAllowed` — set `CREDSTORE_KEYCHAIN` to an isolated keychain path, or let credstore use `~/.credstore/credentials.keychain-db`; the file is created automatically via `security create-keychain` on first use.

### Credential Enumeration

`credstore` (the default, no-command view) reads keys from the OS credential store using platform-specific APIs:

| Platform | API |
|----------|-----|
| **Windows** | `win32cred.CredEnumerate` |
| **WSL** | `powershell.exe` + inline C# `CredEnumerateW` via `advapi32.dll` |
| **Other** | Unsupported — re-run `credstore set <KEY>` to populate cryptfile |

Enumeration retrieves key names only — secret values are never batch-loaded. Sync comparison fetches one value at a time and immediately discards it.

## License

MIT
