"""keyring: URI resolution.

Format::

    keyring:<service>/<key>

Examples::

    "keyring:slife/provider/deepseek"
    "keyring:myapp/github"

The ``keyring:`` prefix signals that the value should be resolved
from the credential store. Everything else passes through unchanged.
"""

from __future__ import annotations

import re

# Pattern: keyring:<service>/<key>
# service: alphanumeric + dots/hyphens/underscores
# key: anything after the first slash (non-greedy, up to end)
_KEYRING_URI_RE = re.compile(r"^keyring:([\w.-]+)/(.+)$")


def is_keyring_uri(value: str) -> bool:
    """Check if a string is a keyring: URI reference.

    >>> is_keyring_uri("keyring:slife/deepseek")
    True
    >>> is_keyring_uri("sk-plaintext-key")
    False
    >>> is_keyring_uri("${DEEPSEEK_API_KEY}")
    False
    """
    if not isinstance(value, str):
        return False
    return _KEYRING_URI_RE.match(value) is not None


def parse_keyring_uri(value: str) -> tuple[str, str] | None:
    """Parse a keyring: URI into (service, key).

    Returns None if the value is not a valid keyring URI.

    >>> parse_keyring_uri("keyring:slife/provider/deepseek")
    ("slife", "provider/deepseek")
    >>> parse_keyring_uri("not-a-uri")
    None
    """
    if not isinstance(value, str):
        return None
    m = _KEYRING_URI_RE.match(value)
    if m is None:
        return None
    return m.group(1), m.group(2)


def resolve_uri(value: str) -> str:
    """Resolve a value that may be a keyring: URI.

    If *value* starts with ``keyring:``, resolve it from the
    credential store. Otherwise return *value* unchanged.

    Raises:
        KeyError: If the URI is valid but the credential is not found.

    >>> # With mock backend:
    >>> resolve_uri("keyring:slife/deepseek")
    "sk-..."  # actual secret from keyring
    >>> resolve_uri("sk-plaintext-key")
    "sk-plaintext-key"  # passed through unchanged
    """
    if not isinstance(value, str):
        return value

    parsed = parse_keyring_uri(value)
    if parsed is None:
        return value  # Not a keyring URI — pass through

    service, key = parsed
    full_key = f"{service}/{key}"
    from credstore._store import get_credential

    result = get_credential(full_key)
    if result is None:
        raise KeyError(
            f"Credential '{full_key}' not found in keyring. "
            f"Store it with: credstore set {full_key}"
        )
    return result


def resolve_uri_recursive(value):
    """Resolve keyring: URIs recursively in strings, dicts, and lists.

    Used to process entire config trees after env var resolution.
    """
    if isinstance(value, str):
        return resolve_uri(value)
    elif isinstance(value, dict):
        return {k: resolve_uri_recursive(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_uri_recursive(item) for item in value]
    else:
        return value
