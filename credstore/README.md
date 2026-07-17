# credstore

Cross-platform credential storage — OS keyring + encrypted file backup.

## Setup (one time)

```bash
credstore set-password
```

This creates `./credentials.crypt` — an AES-encrypted backup file.
The default path can be changed via `CREDSTORE_FILE` env var or
`credstore.json5` config.

## Store

```bash
credstore set DEEPSEEK_API_KEY
credstore set OPENAI_API_KEY
credstore set GITHUB_TOKEN
```

Each command reads the secret from stdin with masked input (`*` echo).
Requires `set-password` to have been run first. Secrets are stored in
the OS keyring (Windows Credential Manager / macOS Keychain / Linux
Secret Service).

## Retrieve (system keyring only, no master key)

```bash
credstore get DEEPSEEK_API_KEY   # → DEEPSEEK_API_KEY: sk-5f…b722
credstore list                   # → list all stored key names
credstore delete OLD_KEY         # → remove from keyring
```

## Reset — restore system keyring from backup

When the OS keyring loses data (e.g. Windows password change), restore
all credentials from the encrypted backup file:

```bash
credstore reset
# Master password: ********
# Restored 5 credential(s) to system keyring.
```

Enter the master key → reads every credential from `credentials.crypt`
→ writes each one back to the OS keyring via `keyring.set_password()`.

## Commands

| Command | Needs master key | Description |
|---------|:---:|-------------|
| `set-password` | sets it | Create/change master key, init encrypted backup |
| `set KEY` | checks it's set | Store a secret in OS keyring |
| `get KEY` | no | Retrieve from OS keyring (masked output) |
| `delete KEY` | no | Remove from OS keyring |
| `list` | no | List all stored key names |
| `reset` | must enter it | Restore OS keyring from encrypted backup |

## Config file

`credstore.json5` (current dir, or `~/.credstore/config.json5`):

```json5
{
  // Custom path for the encrypted backup file.
  // Default: ./credentials.crypt
  cryptfile_path: "credentials.crypt",
}
```

Env var `CREDSTORE_FILE` takes highest priority.
