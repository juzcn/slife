"""Persistent provider/model configuration storage.

The non-secret *shape* of a Claude Code provider setup lives in
``~/.claude/cc-config.json`` (path overridable via ``CC_CONFIG_FILE``).
Only provider metadata is stored here — never API keys.  The secret is
referenced by *name* (the ``api_key_name`` field) and resolved through
credstore at activate time.

File format::

    {
      "providers": {
        "deepseek": {
          "base_url": "https://api.deepseek.com/anthropic",
          "api_key_name": "DEEPSEEK_API_KEY",
          "models": ["deepseek-chat", "deepseek-reasoner"],
          "extra_env": {}
        }
      }
    }
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CC_CONFIG_FILE", str(Path.home() / ".claude" / "cc-config.json")))


def load_config() -> dict:
    """Load provider configs from ``~/.claude/cc-config.json``.

    Returns a dict with a ``"providers"`` key (possibly empty).  A
    missing or unparseable file yields ``{"providers": {}}``.
    """
    import json

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"providers": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        return {"providers": {}}
    return raw


def save_config(data: dict) -> None:
    """Persist the whole config dict to ``~/.claude/cc-config.json``."""
    import json

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _get_providers() -> dict:
    return load_config().get("providers", {})


def add_provider(name: str, base_url: str, api_key_name: str, models: list[str], extra_env: dict | None = None) -> None:
    """Add a new provider, or replace an existing one with the same name."""
    data = load_config()
    data.setdefault("providers", {})[name] = {
        "base_url": base_url,
        "api_key_name": api_key_name,
        "models": list(models),
        "extra_env": dict(extra_env or {}),
    }
    save_config(data)


def update_provider(name: str, base_url: str | None = None, api_key_name: str | None = None,
                    models: list[str] | None = None, extra_env: dict | None = None) -> None:
    """Merge changes into an existing provider.  Raises KeyError if unknown."""
    providers = _get_providers()
    if name not in providers:
        raise KeyError(name)
    provider = providers[name]
    if base_url is not None:
        provider["base_url"] = base_url
    if api_key_name is not None:
        provider["api_key_name"] = api_key_name
    if models is not None:
        provider["models"] = list(models)
    if extra_env is not None:
        provider["extra_env"] = dict(extra_env)
    save_config({"providers": providers})


def remove_provider(name: str) -> bool:
    """Delete a provider.  Returns True if it existed."""
    providers = _get_providers()
    if name not in providers:
        return False
    del providers[name]
    save_config({"providers": providers})
    return True


def list_providers() -> list[str]:
    """Return provider names, sorted."""
    return sorted(_get_providers().keys())
