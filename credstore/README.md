# credstore

Cross-platform credential storage — OS keyring + AES-encrypted file backup.

A standalone secret manager that ships with [Slife](https://github.com/juzcn/slife) but does not depend on it.

## CLI

### Setup (first time)

```bash
credstore set-password
```

Creates `~/.slife/credentials.crypt` and sets a master password. Path configurable via `CREDSTORE_FILE` env var.

### Command Reference

| Command | Master key | Description |
|---------|:----------:|-------------|
| `set-password` | sets it | Create/change master key, init cryptfile |
| `set KEY` | enters it | Atomic dual-write (cryptfile + keyring). Rolls back on keyring failure |
| `get KEY` | no | Keyring only, masked output (`sk-5f…b722`) |
| `get KEY -p` | enters it | Dual-query keyring + cryptfile, plaintext. Fails on mismatch |
| `delete KEY` | enters it | Remove from keyring + cryptfile |
| `list` | enters it | List all stored credential keys |
| `reset-keyring` | enters it | Restore keyring from cryptfile backup |
| `reset-backup` | enters it | Sync system keyring → cryptfile |
| `status` | no | Show backend status |

```bash
credstore set DEEPSEEK_API_KEY        # store (masked, atomic dual-write)
credstore get DEEPSEEK_API_KEY        # retrieve, masked output
credstore get DEEPSEEK_API_KEY -p     # retrieve plaintext (dual-query)
credstore list                        # list all stored keys
credstore delete OLD_KEY              # delete from keyring + cryptfile
credstore reset-keyring               # restore keyring from cryptfile backup
credstore reset-backup                # sync keyring → cryptfile
credstore status                      # show backend status
```

### Disaster Recovery

When the OS keyring loses data (e.g. after a Windows password change):

```bash
credstore reset-keyring               # restore all from cryptfile backup
```

## Python API

```python
import credstore

# Read / check / write / delete (system keyring only, no prompt)
credstore.get_credential("myapp/api_key")      # → str | None
credstore.exists_credential("myapp/api_key")   # → bool
credstore.set_credential("myapp/api_key", "sk-…")
credstore.delete_credential("myapp/api_key")   # → bool

# keyring: URI resolution
credstore.is_keyring_uri("keyring:myapp/k")    # → True
credstore.resolve_uri("keyring:myapp/k")       # → the secret value

# Backend info
credstore.check_backend()       # → {"available": True, "backend": "…", …}
credstore.is_cryptfile_ready()  # → True if master key is set
```

The Python API talks to the system keyring only — no master password, no prompt. Dual-write (keyring + cryptfile) is handled by the CLI.
