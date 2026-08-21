"""Activation — materialise a provider/model into ``~/.claude/settings.json``.

Plain ``activate`` fills every model slot with the stock Claude Code
defaults from :mod:`cc_switch._defaults`; ``--custom`` lets the user
override each env key interactively before writing.

The secret is read from credstore (by the *api_key_name* referenced in
the provider config) and injected into the **system environment** as
``ANTHROPIC_AUTH_TOKEN`` — mirroring ``credstore inject`` (registry on
Windows, shell profile on Unix).  The generated settings.json never
contains a credential line, and the env injection survives the transient
cc-switch process so a new Claude Code session picks it up.

If the secret is missing from credstore, activation fails loudly instead
of writing an unusable settings.json.

Memory safety: the keyring value is fetched, used, then ``del``-ed
immediately.
"""

from __future__ import annotations

import os
import sys

from cc_switch import _defaults

# Output path — overridable for tests via monkeypatch.
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")


class SecretNotFoundError(RuntimeError):
    """Raised when a provider's api_key_name is not stored in credstore."""


def resolve_secret(api_key_name: str) -> str | None:
    """Read *api_key_name* from credstore (system keyring).

    Returns the secret, or None when the key is not stored or credstore
    is unavailable.  The caller must ``del`` the returned value after
    use.  This is the only place cc-switch touches secret material.
    """
    try:
        from credstore import get_credential
    except Exception:
        return None
    return get_credential(api_key_name)


def build_env(provider: dict, model_name: str, overrides: dict | None = None) -> dict:
    """Build the env block for settings.json.

    *overrides* maps env key → value and is used by ``--custom``; a
    missing key falls back to ``default_value`` — the main-model slots
    inherit *model_name* (never empty), the rest keep their static
    default.  Always + ``ANTHROPIC_BASE_URL`` + model — never a secret.
    """
    env: dict[str, str] = {}
    # 1. Every override slot (with the override value where provided)
    for key in _defaults.DEFAULT_OVERRIDE_KEYS:
        if overrides and key in overrides:
            env[key] = str(overrides[key])
        else:
            env[key] = _defaults.default_value(key, model_name)
    # 2. Provider-specific env overrides (applied after the slots)
    env.update(provider.get("extra_env") or {})
    # 3. Provider base URL + chosen model
    env["ANTHROPIC_BASE_URL"] = provider["base_url"]
    env["ANTHROPIC_MODEL"] = model_name
    return env


def build_settings(provider: dict, model_name: str, overrides: dict | None = None) -> dict:
    """Return the full settings.json dict (no secret material)."""
    settings = dict(_defaults.DEFAULT_SETTINGS)
    settings["env"] = build_env(provider, model_name, overrides)
    return settings


def write_settings(settings: dict) -> None:
    """Write the settings dict to ``~/.claude/settings.json`` (overrides path)."""
    import json

    target = SETTINGS_PATH
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def inject_token(secret: str, shell: str = "auto", output=None) -> None:
    """Persist *secret* as ``ANTHROPIC_AUTH_TOKEN`` in the system environment.

    Mirrors ``credstore inject``: writes the value to the system env via
    ``persist_key`` (registry on Windows, shell profile on Unix) so a
    freshly launched Claude Code session inherits it.  When *output* is
    a TTY it prints an activation hint (no secret); otherwise it prints
    the export line for the current shell.
    """
    from credstore._shell import format_export, persist_key

    persist_key("ANTHROPIC_AUTH_TOKEN", secret, shell)

    out = output if output is not None else sys.stdout
    if out.isatty():
        print(
            "Injected ANTHROPIC_AUTH_TOKEN into the system environment.",
            file=out,
        )
        print(
            "Restart your shell (or start a new terminal) for it to take effect.",
            file=out,
        )
    else:
        print(format_export("ANTHROPIC_AUTH_TOKEN", secret, shell), file=out)


def activate(provider: dict, model_name: str, overrides: dict | None = None,
             shell: str = "auto", output=None) -> dict:
    """Generate, write settings.json, and inject the secret into the system env.

    Writes settings.json first (so the config shape is in place even if
    the token lookup then fails), then resolves the API key from
    credstore.  A missing key raises :class:`SecretNotFoundError` —
    never a silent skip.  Returns the settings dict that was written.
    """
    settings = build_settings(provider, model_name, overrides)
    write_settings(settings)

    secret = resolve_secret(provider["api_key_name"])
    if secret is None:
        raise SecretNotFoundError(
            f"'{provider['api_key_name']}' is not in credstore.\n"
            f"Store it first: credstore set {provider['api_key_name']}"
        )
    try:
        inject_token(secret, shell=shell, output=output)
    finally:
        del secret
    return settings
