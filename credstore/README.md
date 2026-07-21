# credstore

Cross-platform credential storage — OS keyring + AES-encrypted file backup.

A standalone secret manager that ships with [Slife](https://github.com/juzcn/slife)
but does **not** depend on it.  Only two lightweight dependencies: `keyring` and
`keyrings-cryptfile`.  Available both as a standalone PyPI package and bundled
with Slife.

## Install

### Standalone (PyPI)

```bash
pip install credstore
```

Or with uv:

```bash
uv tool install credstore
```

### Bundled with Slife

Installing Slife gives you the `credstore` command automatically — no extra step.

```bash
uv tool install git+https://github.com/juzcn/slife.git
```

In a development checkout, run via uv:

```bash
uv run credstore <command>
```

### Verify

```bash
credstore status
```

No configuration needed — the OS keyring is used automatically.
Run `credstore set-password` to enable encrypted backup.

---

## CLI

### Setup (first time)

```bash
credstore set-password
```

Creates `~/.credstore/credentials.crypt` and sets a master password (dev: `./credentials.crypt`). Path configurable via `CREDSTORE_FILE` env var.

### Command Reference

| Command | Master key | Description |
|---------|:----------:|-------------|
| `set-password` | sets it | Create/change master key, init cryptfile |
| `set KEY` | enters it | Atomic dual-write (cryptfile + keyring). Rolls back on keyring failure |
| `get KEY` | no | Keyring only, masked output (`sk-5f…b722`) |
| `get KEY -p` | enters it | Dual-query keyring + cryptfile, plaintext. Fails on mismatch |
| `delete KEY` | enters it | Remove from keyring + cryptfile |
| `list` | enters it | Triple-read: keyring + cryptfile + env vars |
| `inject KEY...` | no | Persist to system env: registry (Windows) or shell profile (Unix) |
| `uninject KEY...` | no | Remove from system env: registry (Windows) or shell profile (Unix) |
| `reset-keyring` | enters it | Restore keyring from cryptfile backup |
| `reset-backup` | enters it | Sync system keyring → cryptfile |
| `status` | no | Show backend status |

```bash
credstore set-password                   # first-time setup
credstore set DEEPSEEK_API_KEY           # store (masked, atomic dual-write)
credstore get DEEPSEEK_API_KEY           # retrieve, masked output
credstore get DEEPSEEK_API_KEY -p        # retrieve plaintext (dual-query)
credstore list                           # triple-read: keyring + cryptfile + env
credstore delete OLD_KEY                 # delete from keyring + cryptfile

# System environment injection (persistent)
credstore inject DEEPSEEK_API_KEY        # Windows → registry, Unix → shell profile
Invoke-Expression (credstore inject KEY) # PowerShell: activate in current shell
eval "$(credstore inject KEY)"           # Bash/Zsh: activate in current shell
credstore uninject DEEPSEEK_API_KEY      # remove from registry/profile

credstore reset-keyring                  # restore keyring from cryptfile backup
credstore reset-backup                   # sync keyring → cryptfile
credstore status                         # show backend status
```

### List Output

`credstore list` performs a **triple-read** — checking the system keyring, encrypted
cryptfile backup, and current environment variables simultaneously:

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
| `SYSTEM KEYRING` | ✔ = stored in OS keyring, — = missing |
| `CRYPTFILE` | ✔ = stored in encrypted backup, — = missing |
| `ENV` | ✔ = currently set as environment variable, — = not set |
| `STATUS` | `synced` (both stores match), `keyring only`, `cryptfile only`, or `MISMATCH ⚠` |

The `ENV` column uses `os.environ` — it checks whether the credential key is
currently exported as an environment variable. No secret values are decoded
during the check.

### Environment Variable Injection

`inject` reads a secret from the keyring and persists it to the system
environment.  On Windows, it writes directly to the registry (`HKCU\Environment`);
on Unix, it appends to the shell profile.  New shells load it automatically.

```bash
# Persist + activate in current shell
credstore inject DEEPSEEK_API_KEY        # persist to registry/profile
Invoke-Expression (credstore inject KEY) # PowerShell: activate now
eval "$(credstore inject KEY)"           # Bash/Zsh: activate now

# Remove
credstore uninject DEEPSEEK_API_KEY      # remove from registry/profile
```

Secret never appears on the terminal — `inject` detects TTY and prints only
a hint.  The actual export command flows through a pipe when wrapped in
`Invoke-Expression` / `eval`.

No master password required (keyring only).

### Disaster Recovery

When the OS keyring loses data (e.g. after a Windows password change):

```bash
credstore reset-keyring               # restore all from cryptfile backup
```

## Design: Memory Safety

Secrets are immutable Python `str` objects — they cannot be zeroed in place.
credstore mitigates memory leaks through three design rules:

| Rule | Mechanism |
|------|-----------|
| **Never batch-load** | `list` collects only key names from keyring, cryptfile, and `os.environ`. Sync comparison fetches one value at a time and immediately `del`-s it |
| **Prefer existence checks** | `exists_credential()` / `list_credential_keys()` never retrieve secret content |
| **Explicit cleanup** | Every CLI handler `del`-s secret references after last use. `set`, `get`, `reset`, `set-password` all clean up on every exit path — including error branches |

### Operations that read/transport secrets

| Operation | How memory is cleaned |
|-----------|----------------------|
| `get` | Caller is responsible for `del`-ing the returned value |
| `set` | `del secret` + `del master_pw` after dual-write (all code paths) |
| `list` | Values fetched one-at-a-time, compared, `del`-ed immediately. Env vars checked via `os.environ` — no secret values decoded |
| `inject` | Value read → persisted → `del`-ed. TTY: no secret on stdout. Pipe: secret through pipe only |
| `uninject` | No secrets involved — registry/profile cleanup only |
| `reset-keyring` | Each value `del`-ed after writing to keyring |
| `reset-backup` | Batch load unavoidable, `del entries` + `del master_pw` after sync |
| `set-password` | `old_data` dict `del`-ed after re-encryption; all password strings cleaned up |

The `masked_input()` terminal helper echoes `*` for each keystroke — paste works but the
actual value is never displayed or logged.

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
credstore.is_cryptfile_ready()  # → True if master key is set
```

Memory rule: callers of `get_credential()` and `resolve_uri()` MUST `del` the
returned value after use.  Prefer `exists_credential()` or `list_credential_keys()`
when you only need to know whether a credential exists.

The Python API talks to the system keyring only — no master password, no prompt. Dual-write (keyring + cryptfile) is handled by the CLI.
