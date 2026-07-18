# credstore

Cross-platform credential storage — OS keyring + AES-encrypted file backup.

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
**Dual-writes** to both the OS keyring and `credentials.crypt`
(encrypted backup).

### Retrieve a credential

```bash
credstore get DEEPSEEK_API_KEY   # → DEEPSEEK_API_KEY: sk-5f…b722
```

Reads from the OS keyring (no master password).
If not found, offers a cryptfile fallback:

```
Not found in keyring: DEEPSEEK_API_KEY
Master password to search encrypted backup (or press Enter to skip): ********
DEEPSEEK_API_KEY: sk-5f…b722
(auto-restored to system keyring)

⚠  This credential was only in the encrypted backup.
   Other credentials may also be missing from the keyring.
   Run:  credstore reset
   to restore ALL credentials from the backup.
```

### Delete a credential

```bash
credstore delete OLD_KEY
```

Removes from both the OS keyring and the cryptfile.
(Cryptfile delete does not need the master password.)

### Restore from backup

When the OS keyring loses data (e.g. after a Windows password change):

```bash
credstore reset
# Master password: ********
# Restored 5 credential(s) to system keyring.
```

Decrypts every credential in `credentials.crypt` and writes
them back to the OS keyring.

### Sync — populate cryptfile from keyring

Migrate existing credentials (stored before cryptfile dual-write was
enabled) into the encrypted backup:

```bash
credstore sync
# Master password: ********
# Found 12 credential(s) in system keyring:
#   DEEPSEEK_API_KEY
#   OPENAI_API_KEY
#   ...
# Synced 12 credential(s) to cryptfile.
```

Enumerates credentials from the system keyring (Windows Credential
Manager) and writes each one to `credentials.crypt`.  Run this once
after upgrading to enable `reset` to work.

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
| `set KEY` | enters it | Store a secret (keyring + cryptfile dual-write) |
| `get KEY` | optional | Retrieve from keyring; cryptfile fallback on miss |
| `delete KEY` | no | Remove from keyring + cryptfile |
| `reset` | enters it | Restore keyring from cryptfile backup |
| `sync` | enters it | Sync system keyring → cryptfile backup |
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

The Python API is **read-only** for credentials — secrets are only
written through the CLI.  All functions talk to the system keyring
(no master password, no cryptfile fallback, never prompt).

### Credential access

```python
credstore.get_credential("myapp/api_key")    # → str | None
credstore.exists_credential("myapp/api_key") # → bool
credstore.delete_credential("myapp/api_key") # → bool
```

- `get_credential` — retrieve from system keyring. Returns `None`
  if not found (no cryptfile fallback — use the CLI `get` command
  for that).
- `exists_credential` — check existence without pulling the secret
  into process memory.  Preferred for agent tools.
- `delete_credential` — remove from both keyring and cryptfile.

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
