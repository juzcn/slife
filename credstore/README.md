# credstore

Cross-platform credential storage — OS keyring + AES-encrypted file backup.

All commands require an interactive terminal (tty).

## CLI

### Setup

```bash
credstore set-password
```

Creates `./credentials.crypt` and sets a master password.
Path configurable via `CREDSTORE_FILE` env var or `credstore.json5`.

### Store a credential

```bash
credstore set DEEPSEEK_API_KEY
```

Prompts for the secret (masked, `****`), then the master password.
**Atomic dual-write**: writes to cryptfile first, then system keyring.
If keyring write fails, rolls back the cryptfile entry — both stores
stay consistent.

### Retrieve a credential

```bash
credstore get DEEPSEEK_API_KEY        # → DEEPSEEK_API_KEY: sk-5f…b722
credstore get DEEPSEEK_API_KEY -p     # → sk-5f1b...b722  (plaintext)
```

Default mode: reads from the OS keyring (no master password), outputs
masked value (`sk-5f…b722`).

`--password` / `-p` mode: prompts for the master password, then
queries **both** keyring and cryptfile.  If both match, prints the
secret in plaintext.  Fails on mismatch, missing entry, or read error.

### Delete a credential

```bash
credstore delete OLD_KEY
```

Prompts for the master password, then removes from both the OS keyring
and the cryptfile.

### Restore keyring from backup

When the OS keyring loses data (e.g. after a Windows password change):

```bash
credstore reset-keyring
# Master password: ********
# Restored 5 credential(s) to system keyring.
```

Decrypts every credential in `credentials.crypt` and writes
them back to the OS keyring.

### Reset backup from keyring

Migrate existing credentials (stored before cryptfile dual-write was
enabled) into the encrypted backup:

```bash
credstore reset-backup
# Master password: ********
# Found 12 credential(s) in system keyring:
#   DEEPSEEK_API_KEY
#   OPENAI_API_KEY
#   ...
# Reset 12 credential(s) in cryptfile backup.
```

Enumerates credentials from the system keyring (Windows Credential
Manager) and writes each one to `credentials.crypt`.  Run this once
after upgrading to enable `reset-keyring` to work.

### List stored credentials

```bash
credstore list
# Master password: ********
# 3 credential(s) stored:
#   DEEPSEEK_API_KEY
#   GITHUB_TOKEN
#   OPENAI_API_KEY
```

Reads the cryptfile to list all stored credential keys.  Secret values
are never shown.  Requires the master password.

### Backend status

```bash
credstore status
# Backend: system keyring + cryptfile (dual-write)
# Available: True
# Cryptfile: ready (dual-write active)
```

### Command reference

| Command | Master key | Description |
|---------|:----------:|-------------|
| `set-password` | sets it | Create/change master key, init cryptfile |
| `set KEY` | enters it | Atomic dual-write (cryptfile + keyring) |
| `get KEY` | no | Keyring only, masked output |
| `get KEY -p` | enters it | Dual-query keyring + cryptfile, plaintext |
| `delete KEY` | enters it | Remove from keyring + cryptfile |
| `list` | enters it | List all stored credential keys |
| `reset-keyring` | enters it | Restore keyring from cryptfile backup |
| `reset-backup` | enters it | Overwrite cryptfile from system keyring |
| `status` | no | Show backend status |

### Config

`credstore.json5` (current dir, or `~/.credstore/config.json5`):

```json5
{
  cryptfile_path: "credentials.crypt",   // custom path
}
```

Env var `CREDSTORE_FILE` takes highest priority.

---

## Developer API

```python
import credstore
```

The Python API talks to the system keyring only — no master password,
no cryptfile fallback, never prompt.  Dual-write (keyring + cryptfile)
is handled by the CLI layer.

### Credential access

```python
credstore.get_credential("myapp/api_key")    # → str | None
credstore.exists_credential("myapp/api_key") # → bool
credstore.set_credential("myapp/api_key", "sk-…")  # → None (raises on failure)
credstore.delete_credential("myapp/api_key") # → bool
```

- `get_credential` — retrieve from system keyring. Returns `None`
  if not found (no cryptfile fallback — use the CLI `get` command
  for that).
- `exists_credential` — check existence without pulling the secret
  into process memory.  Preferred for agent tools.
- `set_credential` — write to system keyring only.  Requires cryptfile
  to exist (run `credstore set-password` first).  For full dual-write,
  use the CLI `set` command.
- `delete_credential` — remove from system keyring.

### keyring: URI resolution

```python
credstore.is_keyring_uri("keyring:myapp/key")   # → True
credstore.parse_keyring_uri("keyring:myapp/key") # → ("myapp", "key")
credstore.resolve_uri("keyring:myapp/key")       # → the secret value
```

`resolve_uri` takes a `keyring:` URI and returns the credential.
Non-`keyring:` values pass through unchanged.  Useful for config
values that may reference the keyring:

```json5
// slife.json5
{ api_key: "keyring:slife/deepseek" }
```

### Backend info

```python
credstore.check_backend()
# → {"available": True, "backend": "system keyring + cryptfile …", …}

credstore.get_backend_name()   # → "system keyring + cryptfile (dual-write)"
credstore.is_cryptfile_ready() # → True if cryptfile exists / master key set
```
