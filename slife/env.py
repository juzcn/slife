"""Environment variable resolution for config values.

Supports ${VAR} and ${VAR:-default} syntax in string values,
recursively resolving through dicts and lists.
"""

import os
import re
from typing import Any

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def parse_env_ref(value: str) -> tuple[str, str | None] | None:
    """Parse a pure ``${VAR}`` or ``${VAR:-default}`` reference.

    Returns ``(var, default_or_None)``, or None when *value* is not a pure
    reference (plaintext, or a ref embedded in surrounding text).  The one
    shared parser — nobody re-slices ``[2:-1]`` or re-writes the regex, so
    the variant implementations (config vs the gateway's tools config)
    cannot drift (e.g. a
    ``\\w+``-only regex losing dotted/hyphenated names).
    """
    m = _ENV_PATTERN.fullmatch(value)
    if m is None:
        return None
    return m.group(1), m.group(2)


def is_env_ref(value: str) -> bool:
    """True if *value* is a pure ``${VAR}`` / ``${VAR:-default}`` reference."""
    return parse_env_ref(value) is not None


def resolve_secret_value(value: str) -> str:
    """Resolve ``${VAR}`` / ``${VAR:-default}`` refs in *value*, leniently.

    Resolution order is shell env → credstore → literal default (the
    documented chain); an unbound ref with no default stays as its literal
    text.  Works for both pure refs and refs embedded in a larger string.
    Never raises — callers that need strictness use :func:`resolve_env`.
    """
    def _replace(m: re.Match) -> str:
        var = m.group(1)
        env_val = os.environ.get(var)
        if not env_val:
            from slife.config import _try_credstore_lookup
            env_val = _try_credstore_lookup(var)
        if env_val:
            return env_val
        default = m.group(2)
        return default if default is not None else m.group(0)
    return _ENV_PATTERN.sub(_replace, value)


def resolve_env(value: Any) -> Any:
    """Resolve ``${ENV_VAR}`` and ``${ENV_VAR:-default}`` references recursively.

    Accepts str, dict, list, or scalar — dicts and lists are traversed
    and every string value is resolved.  Scalars pass through unchanged.

    Raises:
        KeyError: If a referenced env var is not set and no default is given.
    """
    if isinstance(value, str):
        def _replace(m):
            var_name = m.group(1)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            # credstore BEFORE the literal default — the documented order is
            # "shell env > credstore > literal".  Without this, ${VAR:-default}
            # resolves to the default even when the key is held in credstore,
            # so the stored secret never wins.
            from slife.config import _try_credstore_lookup
            cred_val = _try_credstore_lookup(var_name)
            if cred_val is not None:
                return cred_val
            default = m.group(2)
            if default is not None:
                return default
            raise KeyError(
                f"Environment variable '{var_name}' is not set."
            )
        return _ENV_PATTERN.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: resolve_env(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env(item) for item in value]
    else:
        return value
