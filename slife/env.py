"""Environment variable resolution for config values.

Supports ${VAR} and ${VAR:-default} syntax in string values,
recursively resolving through dicts and lists.
"""

import os
import re

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def resolve_env(value):
    """Resolve ${ENV_VAR} and ${ENV_VAR:-default} references recursively.

    Args:
        value: A str, dict, list, or scalar to resolve.

    Returns:
        The value with all env var references replaced.

    Raises:
        KeyError: If a referenced env var is not set and no default is given.
    """
    if isinstance(value, str):
        def _replace(m):
            var_name = m.group(1)
            default = m.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
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
