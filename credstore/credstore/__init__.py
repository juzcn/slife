"""credstore — secure credential storage via OS keyring.

Cross-platform API for retrieving secrets stored in the OS keyring
with keyrings.cryptfile encrypted backup.  Secrets are *stored* only
via the CLI (``credstore set``) which reads from masked stdin — never
through Python function arguments.

Modules::

    _store.py        CredentialStore + module-level API
    _shell.py        Shell formatting helpers
    _backend.py      Dual-write backends (system keyring + cryptfile)
    _enumerate.py    Platform-specific credential enumeration
    _config.py       Config file loading
    _resolver.py     keyring: URI resolution
    _tty.py          Masked terminal input
    __main__.py      CLI (entry point)

Usage::

    import credstore

    # Retrieve a secret
    value = credstore.get_credential("myapp/api_key")

    # Resolve keyring: URIs in config values
    resolved = credstore.resolve_uri("keyring:myapp/api_key")

    # Delete
    credstore.delete_credential("myapp/api_key")

    # Shell formatting
    credstore.format_export("KEY", "value", "bash")  # → export statement

    # Check backend
    info = credstore.check_backend()
"""

from credstore._shell import format_export, format_unset
from credstore._store import (
    get_credential,
    exists_credential,
    list_credential_keys,
    set_credential,
    delete_credential,
    get_backend_name,
    check_backend,
    init_store,
)
from credstore._resolver import (
    resolve_uri,
    is_keyring_uri,
    parse_keyring_uri,
)

__all__ = [
    # Read / write / delete
    "get_credential",
    "exists_credential",
    "list_credential_keys",
    "set_credential",
    "delete_credential",
    # URI resolution
    "is_keyring_uri",
    "parse_keyring_uri",
    "resolve_uri",
    # Shell formatting
    "format_export",
    "format_unset",
    # Diagnostics
    "get_backend_name",
    "check_backend",
    "init_store",
]
__version__ = "0.3.6"
