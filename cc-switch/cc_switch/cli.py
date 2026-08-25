"""cc-switch CLI — terminal commands for Claude Code settings generation.

Commands::

    cc-switch set <provider> [--name NAME] [--models M1,M2]
        Create or edit a provider config (base URL + API key name,
        plus optional models / display name).  Secrets are never stored.
    cc-switch remove <provider>
        Delete a provider config.
    cc-switch activate <provider>[/<model>]
        Write ~/.claude/settings.json from defaults, inject the API key
        from credstore into ANTHROPIC_AUTH_TOKEN (env only).
    cc-switch activate <provider>[/<model>] --custom
        Interactive override of every model slot, then write.
    cc-switch (no command)
        List providers and their models as provider/model.
    cc-switch list
        Show all providers and their metadata.

Non-secret interactive values use plain ``input()``.  The API key name
is stored in the config; the key value is read from credstore only at
activate time and never written to any file.
"""

from __future__ import annotations

import argparse
import json
import sys

from cc_switch import _activate, _api, _defaults


def _err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def _parse_provider_model(spec: str) -> tuple[str, str | None]:
    """Split 'provider/model' into (provider, model-or-None)."""
    if "/" in spec:
        provider, model = spec.split("/", 1)
        return provider.strip(), model.strip() or None
    return spec.strip(), None


def _prompt_required(prompt: str, current: str = "") -> str:
    """Prompt for a required value.

    When *current* is non-empty (editing), Enter keeps it — blank never
    re-prompts.  When adding (no current), blank re-prompts.
    """
    hint = f" [{current}]" if current else ""
    while True:
        value = input(f"{prompt}{hint}: ").strip()
        if value or current:
            return value or current
        print("(required — cannot be empty)", file=sys.stderr)


def _prompt_optional(prompt: str, current: str = "") -> str:
    """Prompt for an optional value; blank keeps the current / empty."""
    hint = f" [{current}]" if current else " [blank = empty]"
    value = input(f"{prompt}{hint}: ").strip()
    return value or current or ""


def _parse_models(text: str) -> list[str]:
    """Split a model list on commas, whitespace, and/or semicolons."""
    for sep in (",", ";"):
        text = text.replace(sep, " ")
    return [m.strip() for m in text.split() if m.strip()]


def _prompt_models(current: list[str]) -> list[str]:
    """Prompt for the models list.

    Unlike ``_prompt_optional`` this does NOT back-fill the current value
    on a blank answer: the models field is the symmetric difference
    against the stored list, so a blank (empty) answer must be a literal
    empty input — A △ ∅ = A keeps the list unchanged.  Back-filling the
    current value would instead toggle the whole list off.
    """
    hint = ", ".join(current) if current else ""
    text = input(
        "Supported models (comma, space, or semicolon separated; "
        "toggles against the current list)"
        f"{' [' + hint + ']' if hint else ' [blank = keep current]'}: "
    ).strip()
    return _parse_models(text)


# ── set ──────────────────────────────────────────────────────────────


def _cmd_set(args) -> int:
    """Create or edit a provider.  Exists → edit, missing → add."""
    name = args.provider
    providers = _api.load_config().get("providers", {})
    current = providers.get(name)
    is_edit = current is not None

    if is_edit:
        print(f"Editing provider '{name}' (press Enter to keep current value).")
    else:
        print(f"Adding provider '{name}'.")

    base_url = _prompt_required("Base URL", current["base_url"] if current else "")
    api_key_name = _prompt_required("API key name (credstore key)", current["api_key_name"] if current else "")
    models = _prompt_models(current["models"] if current else [])
    extra_env = current.get("extra_env") if current else None
    if is_edit:
        _api.update_provider(name, base_url=base_url, api_key_name=api_key_name, extra_env=extra_env)
    else:
        # New provider: models start empty, so the symmetric-difference
        # path below just adds the input (empty default list).
        _api.add_provider(name, base_url, api_key_name, [], extra_env=extra_env)
    # Every models entry is the symmetric difference against the stored
    # list — a blank input is the difference with the empty list, which
    # is the current list itself (i.e. skip / no change).
    models = _api.set_provider_models(name, models)
    print(f"Provider '{name}' saved.")
    if models:
        print(f"  models: {', '.join(models)}")
    print(f"  Activate with: cc-switch activate {name}/<model>")
    return 0


# ── remove ───────────────────────────────────────────────────────────


def _cmd_remove(args) -> int:
    if _api.remove_provider(args.provider):
        print(f"Provider '{args.provider}' removed.")
        return 0
    _err(f"provider '{args.provider}' not found.")
    return 1


# ── activate ─────────────────────────────────────────────────────────


def _collect_overrides(main_model: str) -> dict:
    """Interactive override pass for ``activate --custom``.

    Main-model slots default to *main_model* (the CLI-chosen model);
    the effort level defaults to its static value.  A blank response
    keeps the default; a value of ``-`` clears the field to an empty
    string.
    """
    overrides: dict[str, str] = {}
    print("Custom activation — enter a new value, or Enter to keep the default.")
    for key in _defaults.DEFAULT_OVERRIDE_KEYS:
        current = _defaults.default_value(key, main_model)
        hint = ""
        if key == "ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL":
            hint = " (low|medium|high|xhigh|max)"
        value = input(f"{key} [{current}]{hint}: ").strip()
        if value:
            # '-' clears the field to an empty string; anything else overrides.
            overrides[key] = "" if value == "-" else value
    return overrides


def _cmd_activate(args) -> int:
    provider_name, model_name = _parse_provider_model(args.provider_model)
    providers = _api.load_config().get("providers", {})
    provider = providers.get(provider_name)
    if provider is None:
        _err(f"provider '{provider_name}' not found. Add it first: cc-switch set {provider_name}")
        return 1

    if model_name is None:
        models = provider.get("models") or []
        if len(models) == 1:
            model_name = models[0]
        elif not models:
            model_name = _prompt_required("Model name")
        else:
            print("Available models:")
            for i, m in enumerate(models, 1):
                print(f"  {i}. {m}")
            try:
                choice = int(input("Select a model by number: ").strip())
                model_name = models[choice - 1]
            except (ValueError, IndexError):
                _err("invalid selection.")
                return 1
    elif model_name not in (provider.get("models") or []):
        print(f"Note: model '{model_name}' is not in the provider's model list.", file=sys.stderr)

    overrides = _collect_overrides(model_name) if args.custom else None

    # --custom never touches the stored config; the overrides are one-shot.
    try:
        settings = _activate.activate(
            provider, model_name, overrides=overrides,
            shell=args.shell,
        )
    except _activate.SecretNotFoundError as exc:
        _err(str(exc))
        _err("settings.json was not updated — store the key, then re-run activate.")
        return 1

    print(f"Activated {provider_name}/{model_name}.")
    print(f"  Wrote: {_activate.SETTINGS_PATH}")
    print(f"  Injected ANTHROPIC_AUTH_TOKEN from credstore '{provider['api_key_name']}'.")
    return 0


# ── list ─────────────────────────────────────────────────────────────


def _cmd_list(args) -> int:
    """List providers and their models as provider/model rows."""
    providers = _api.load_config().get("providers", {})
    if not providers:
        print("No providers configured.")
        print("Add one with: cc-switch set <provider-name>")
        return 0
    for name in sorted(providers):
        provider = providers[name]
        models = provider.get("models") or []
        if not models:
            print(f"{name}/<no models — run 'cc-switch set {name}' to add>")
            continue
        for model in models:
            print(f"{name}/{model}")
    return 0


def _cmd_list_providers(args) -> int:
    """Show providers with their metadata (base URL, API key name) for ``list``."""
    providers = _api.load_config().get("providers", {})
    if not providers:
        print("No providers configured.")
        return 0
    for name in sorted(providers):
        provider = providers[name]
        models = provider.get("models") or []
        print(f"{name}:")
        print(f"  base_url:      {provider.get('base_url', '')}")
        print(f"  api_key_name:  {provider.get('api_key_name', '')}")
        print(f"  models:        {', '.join(models) if models else '(none)'}")
        extra = provider.get("extra_env") or {}
        if extra:
            print(f"  extra_env:     {json.dumps(extra)}")
    return 0


# ── dispatch ─────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc-switch",
        description="Generate ~/.claude/settings.json from saved provider/model configs.",
    )
    sub = parser.add_subparsers(dest="command")

    set_p = sub.add_parser("set", help="Create or edit a provider config")
    set_p.add_argument("provider", help="Provider name, e.g. deepseek")

    rem_p = sub.add_parser("remove", help="Delete a provider config")
    rem_p.add_argument("provider", help="Provider name")

    act_p = sub.add_parser("activate", help="Write ~/.claude/settings.json and inject the API key into the env")
    act_p.add_argument("provider_model", help="Provider/model, e.g. deepseek/deepseek-chat")
    act_p.add_argument("--custom", action="store_true",
                       help="Interactively override every model slot before writing")
    act_p.add_argument("--shell", choices=["auto", "bash", "powershell", "cmd"],
                       default="auto",
                       help="Shell format for the exported env var (default: auto-detect)")

    sub.add_parser("list", help="Show providers with their base URL and API key name")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command is None:
            return _cmd_list(args)
        elif args.command == "set":
            return _cmd_set(args)
        elif args.command == "remove":
            return _cmd_remove(args)
        elif args.command == "activate":
            return _cmd_activate(args)
        elif args.command == "list":
            return _cmd_list_providers(args)
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        _err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
