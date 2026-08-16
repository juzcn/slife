"""Environment variable resolution for config values.

Supports ${VAR} and ${VAR:-default} syntax in string values,
recursively resolving through dicts and lists.
"""

import os
import re
from typing import Any

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


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
