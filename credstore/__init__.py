"""credstore — secure credential storage via OS keyring.

Cross-platform API for retrieving secrets stored in the OS keyring
with keyrings.cryptfile encrypted backup.  Secrets are *stored* only
via the CLI (``credstore set``) which reads from masked stdin — never
through Python function arguments.

Usage::

    import credstore

    # Retrieve a secret
    value = credstore.get_credential("myapp/api_key")

    # Resolve keyring: URIs in config values
    resolved = credstore.resolve_uri("keyring:myapp/api_key")

    # Delete
    credstore.delete_credential("myapp/api_key")

    # Check backend
    info = credstore.check_backend()
"""

from credstore._store import (
    get_credential,
    exists_credential,
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
    "set_credential",
    "delete_credential",
    # URI resolution
    "is_keyring_uri",
    "parse_keyring_uri",
    "resolve_uri",
    # Diagnostics
    "get_backend_name",
    "check_backend",
    "init_store",
]
__version__ = "0.3.6"
