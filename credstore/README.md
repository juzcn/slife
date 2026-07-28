# credstore

Cross-platform credential storage — OS keyring + AES-encrypted file backup.

A standalone secret manager that ships with [Slife](https://github.com/juzcn/slife)
but does **not** depend on it.  Dependencies: `keyring` and `keyrings-cryptfile`.

## Install

```bash
pip install credstore
# or
uv tool install credstore
```

Bundled with Slife — no extra step:

```bash
uv tool install git+https://github.com/juzcn/slife.git
```

Verify:

```bash
credstore status
```

No configuration needed.  Run `credstore set-password` to enable encrypted backup.

---

## CLI

### Setup

```bash
credstore set-password
```

Creates `~/.credstore/credentials.crypt` (or `./credentials.crypt` in dev).
Path overridable via `CREDSTORE_FILE` env var or `credstore.json5` config.

### Commands

| Command | Needs master key | Description |
|---|---|---|
| `set-password` | sets it | Create or change master key |
| `set KEY` | enters it | Atomic dual-write (cryptfile → keyring). Rolls back on keyring failure |
| `get KEY` | no | Keyring only, masked output (`sk-5f…b722`) |
| `get KEY -p` | enters it | Dual-query, plaintext output. Fails on mismatch between stores |
| `delete KEY` | enters it | Remove from keyring + cryptfile |
| `list` | enters it if cryptfile exists | Triple-read: system keyring + cryptfile + env vars |
| `inject KEY… [--shell bash\|pwsh\|cmd]` | no | Persist to system env (Windows: registry; Unix: shell profile) |
| `uninject KEY… [--shell bash\|pwsh\|cmd]` | no | Remove from system env |
| `reset-keyring` | enters it | Restore all credentials from cryptfile → system keyring |
| `reset-backup` | enters it | Sync system keyring → cryptfile backup |
| `status` | no | Show backend status |

### Examples

```bash
credstore set-password                      # first-time setup
credstore set DEEPSEEK_API_KEY              # store (masked, atomic dual-write)
credstore get DEEPSEEK_API_KEY              # retrieve, masked output
credstore get DEEPSEEK_API_KEY -p           # retrieve plaintext (dual-query, consistency check)
credstore list                              # see all keys + sync status
credstore delete OLD_KEY                    # delete from both stores

# Environment injection (persistent across shells)
credstore inject DEEPSEEK_API_KEY           # Windows → registry, Unix → shell profile
eval "$(credstore inject DEEPSEEK_API_KEY)" # Bash/Zsh: activate in current shell
Invoke-Expression (credstore inject KEY)    # PowerShell: activate in current shell
credstore uninject DEEPSEEK_API_KEY         # remove from registry/profile

# Disaster recovery
credstore reset-keyring                     # restore keyring from cryptfile backup
credstore reset-backup                      # sync keyring → cryptfile
```

### How `inject` Works

`inject` reads a secret from the keyring and persists it to the system environment:

- **Windows**: writes directly to `HKCU\Environment` (no shell profile needed)
- **Unix**: appends an `eval "$(credstore inject KEY)"` line to `~/.bashrc`

New shells load it automatically.  When run in a TTY, `inject` prints an activation
hint instead of the secret — the actual export command only flows through a pipe
(via `eval` / `Invoke-Expression`).

### List Output

`credstore list` checks all three stores simultaneously:

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
|---|---|
| `SYSTEM KEYRING` | ✔ = stored in OS keyring |
| `CRYPTFILE` | ✔ = stored in encrypted backup |
| `ENV` | ✔ = currently set as environment variable (checked via `os.environ`, no secrets decoded) |
| `STATUS` | `synced` (both match), `keyring only`, `cryptfile only`, or `MISMATCH ⚠` |

---

## Memory Safety

Secrets are immutable Python `str` objects — they cannot be zeroed in place.
credstore mitigates memory leaks through three design rules:

1. **Never batch-load** — `list` collects only key names. Sync comparison fetches one value at a time and immediately `del`-s it.
2. **Prefer existence checks** — `exists_credential()` / `list_credential_keys()` never retrieve secret content.
3. **Explicit cleanup** — Every CLI handler `del`-s secret references on all exit paths (including error branches).

### Secret Transport

| Operation | Cleanup |
|---|---|
| `get` / `get_credential()` | Caller must `del` the returned value |
| `set` | `del secret` + `del master_pw` after dual-write |
| `list` | Values fetched one-at-a-time, compared, `del`-ed immediately |
| `inject` | Value read → persisted → `del`-ed. TTY: no secret on stdout |
| `reset-keyring` | Each value `del`-ed after writing to keyring |
| `reset-backup` | Batch load unavoidable; `del entries` + `del master_pw` after sync |
| `set-password` | `old_data` dict `del`-ed after re-encryption; passwords cleaned up |

`masked_input()` echoes `*` per keystroke — paste works, actual value never displayed.

---

## Python API

```python
import credstore

# Read / check / write / delete (system keyring only, no prompt)
credstore.get_credential("myapp/api_key")      # → str | None
credstore.exists_credential("myapp/api_key")   # → bool  (NEVER returns secret)
credstore.list_credential_keys()               # → list[str]  (NEVER returns values)
credstore.set_credential("myapp/api_key", "sk-…")
credstore.delete_credential("myapp/api_key")   # → bool

# Shell formatting (for env var injection)
credstore.format_export("MY_KEY", "secret", "bash")   # → "export MY_KEY='secret'"
credstore.format_unset("MY_KEY", "bash")              # → "unset MY_KEY"

# keyring: URI resolution
credstore.is_keyring_uri("keyring:myapp/k")    # → True
credstore.resolve_uri("keyring:myapp/k")       # → the secret value

# Backend info
credstore.check_backend()       # → {"available": True, "backend": "…", …}
credstore.get_backend_name()    # → "system keyring + cryptfile (dual-write)"
```

Format: `keyring:<service>/<key>` — resolves the value from the credential store.
Non-URIs pass through unchanged.

Callers of `get_credential()` and `resolve_uri()` MUST `del` the returned value
after use.  Prefer `exists_credential()` or `list_credential_keys()` when you only
need to know whether a credential exists.

The Python API talks to the system keyring only — no master password, no prompt.
Dual-write (keyring + cryptfile) is handled by the CLI.
