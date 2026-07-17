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
    delete_credential,
    list_credentials,
    get_backend_name,
    check_backend,
    init_store,
)
from credstore._backend import is_cryptfile_ready
from credstore._resolver import (
    resolve_uri,
    is_keyring_uri,
    parse_keyring_uri,
)

__all__ = [
    # Read / delete / list
    "get_credential",
    "delete_credential",
    "list_credentials",
    # URI resolution
    "is_keyring_uri",
    "parse_keyring_uri",
    "resolve_uri",
    # Diagnostics
    "get_backend_name",
    "check_backend",
    "is_cryptfile_ready",
    "init_store",
]
__version__ = "0.1.0"
