"""mcp-gateway config — load/save ``tools.json5``, path resolution, secrets.

Path precedence (one loader, every consumer):
  1. ``$TOOLS_FILE`` — explicit override (a test/dev escape hatch only)
  2. slife data dir — ``<data_dir>/tools.json5`` via
     :func:`slife.paths.get_data_dir` (production ``~/.slife/tools.json5``,
     the checkout root in dev).  The mcp gateway is a built-in slife plugin —
     its config lives next to ``slife.json5``, like memdb/memfiles/wechat.

One section per tool category: ``mcp.servers`` (external MCP servers),
``rest-api``, ``cli``, ``builtin``, ``job``, ``skill`` (the host reads
``builtin`` + ``cli`` at startup; ``job``/``skill`` are reserved).  The
server view — ``servers()`` — is ``mcp.servers`` + ``rest-api``; a legacy
top-level ``servers`` (pre-section shape) reads as the mcp section and is
normalized on the first write.

Server entries hold: ``command/args/env/url/headers/auth/description/enabled/source``
plus ``os_paths``.
``env`` and ``auth.client_id``/``client_secret`` support ``${VAR}``
references resolved through **os.environ → credstore → literal**.  REST APIs
are ordinary ``uvx mcp-openapi-proxy`` entries tagged
``source.type == "rest_api"``.

The top-level ``embeddings`` section is the **fallback** embedding config: a
connecting host may pass its own endpoint via the standard ``initialize``
handshake's ``clientInfo``, which wins when present (see
:mod:`mcp_gateway.embeddings`).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from slife.tools._config_io import (
    config_read_modify_write,
    read_config,
    with_fetched_at,
    write_config,
)

logger = logging.getLogger(__name__)

# ── Path resolution ────────────────────────────────────────────────────


def default_config_path() -> Path:
    """Default config path: ``<slife data dir>/tools.json5``.

    The mcp gateway is a built-in slife plugin — its config sits next to
    ``slife.json5`` in the slife data dir (``~/.slife`` in production, the
    checkout root in dev).  ``get_data_dir()`` honours ``$SLIFE_DATA_DIR``,
    which the host exports so plugin children resolve the same directory.
    """
    from slife.paths import get_data_dir

    return get_data_dir() / "tools.json5"


def resolve_config_path() -> Path:
    """Return the tools.json5 path for this process.

    ``$TOOLS_FILE`` (test/dev override) > slife data dir default.
    """
    env = os.environ.get("TOOLS_FILE")
    if env:
        return Path(env).expanduser()
    return default_config_path()


# ── Reader / writer ──────────────────────────────────────────────────
# read_config / write_config / ConfigParseError / now_iso / with_fetched_at
# are the shared implementations in ``slife.tools._config_io`` (imported
# above) — atomic temp-file + os.replace with a config-dir mkdir, used by
# every slife config writer.  Secret resolution follows below.

# ── Secret resolution (os.environ → credstore → literal) ───────────────
# One shared chain: the parser (``slife.env.parse_env_ref``), the lenient
# resolver (``slife.env.resolve_secret_value``) and the credstore hook
# (``slife.config._try_credstore_lookup``) — the plugin uses the host
# resolver directly (``_resolve_secret`` / ``_resolve_embedded_refs`` are
# thin delegates).  The plugin's old ``\w+``-only regex silently lost
# dotted/hyphenated refs that the host resolver accepted.


def _is_env_ref(value: str) -> bool:
    """True if *value* is a pure ``${VAR}`` reference (no surrounding text)."""
    from slife.env import parse_env_ref

    return parse_env_ref(value) is not None


def _resolve_embedded_refs(value: str) -> str:
    """Resolve embedded ``${VAR}`` refs through os.environ → credstore."""
    from slife.env import resolve_secret_value

    return resolve_secret_value(value)


def _resolve_secret(value: str) -> str:
    """Resolve a secret value through the full resolution chain.

    1. ``${VAR}`` → os.environ → credstore
    2. plaintext → as-is
    """
    from slife.config import _resolve_secret as _host_resolve_secret

    return _host_resolve_secret(value)


# ── Module-level current path + raw accessors ──────────────────────────

_CURRENT_PATH: Path | None = None


def set_config_path(path: Path | str | None = None) -> Path:
    """Pin the config path in use (e.g. from ``load_config(path)``).

    With no *path*, re-resolves from ``$TOOLS_FILE`` / the slife
    data-dir default.
    """
    global _CURRENT_PATH
    if path is not None:
        resolved = Path(path).expanduser()
    else:
        resolved = resolve_config_path()
    _CURRENT_PATH = resolved
    return resolved


def load_config(path: Path | None = None) -> dict:
    """Read config, remembering *path* for the module-level accessors."""
    return read_config(set_config_path(str(path) if path else None))


def current_path() -> Path:
    """The config path in use (last :func:`load_config`, else resolved default)."""
    return _CURRENT_PATH or resolve_config_path()


def _servers_dict(raw: dict) -> dict:
    """All server entries (name → raw entry), merged over the sections.

    ``mcp.servers`` + the ``rest-api`` section (REST APIs are ordinary
    mcp-openapi-proxy servers in their own category).  A legacy top-level
    ``servers`` — the pre-section tools.json5 shape — reads as the mcp
    section, so an old file keeps working at the next start; a write
    normalizes it (see :func:`_normalize_legacy_servers`).
    """
    merged: dict = {}
    mcp_raw = raw.get("mcp")
    if isinstance(mcp_raw, dict):
        mcp_servers = mcp_raw.get("servers")
        if isinstance(mcp_servers, dict):
            merged.update(mcp_servers)
    rest_api = raw.get("rest-api")
    if isinstance(rest_api, dict):
        merged.update(rest_api)
    if not merged:
        legacy = raw.get("servers")
        if isinstance(legacy, dict):
            merged.update(legacy)
    return merged


def servers() -> dict:
    """All server entries (name → raw entry), merged over the sections."""
    return _servers_dict(load_config())


def get_server(name: str) -> dict | None:
    """A single raw server entry, or None."""
    return servers().get(name)


def count_servers() -> int:
    """Number of configured servers."""
    return len(servers())


# ── Server-entry persistence (shared by CLI + server management tools) ──


def _normalize_legacy_servers(raw: dict) -> None:
    """Lift a legacy top-level ``servers`` into ``mcp.servers`` (one-time).

    The pre-restructure tools.json5 held servers at the top level; the
    first write migrates the file so the sections are canonical from then
    on.  No-op when ``mcp`` already exists (a file with both sections is
    already current).
    """
    if "mcp" in raw or not isinstance(raw.get("servers"), dict):
        return
    raw["mcp"] = {"servers": raw.pop("servers")}
    logger.info("config_normalized_legacy_servers")


def _servers_section(raw: dict, section: str) -> dict:
    """The placement dict for *section* — ``"mcp"`` (``mcp.servers``) or
    ``"rest-api"`` — creating it when missing.

    A malformed existing value (not a dict) is replaced by ``{}`` so a
    writer never stack-traces on it.
    """
    if section == "rest-api":
        current = raw.get("rest-api")
        if not isinstance(current, dict):
            current = {}
            raw["rest-api"] = current
        return current
    mcp_raw = raw.get("mcp")
    if not isinstance(mcp_raw, dict):
        mcp_raw = {}
        raw["mcp"] = mcp_raw
    current = mcp_raw.get("servers")
    if not isinstance(current, dict):
        current = {}
        mcp_raw["servers"] = current
    return current


def _find_server_section(raw: dict, name: str) -> str | None:
    """The section (``"mcp"`` or ``"rest-api"``) holding *name*, else None."""
    mcp_raw = raw.get("mcp")
    if (
        isinstance(mcp_raw, dict)
        and isinstance(mcp_raw.get("servers"), dict)
        and name in mcp_raw["servers"]
    ):
        return "mcp"
    rest_api = raw.get("rest-api")
    if isinstance(rest_api, dict) and name in rest_api:
        return "rest-api"
    return None


def add_server_entry(name: str, entry: dict, *, section: str = "mcp") -> None:
    """Upsert *entry* for *name* into *section* with merge semantics.

    Existing fields not explicitly provided are preserved.  ``enabled: True``
    (the default) removes a stale ``enabled: false`` flag; ``None`` values
    are skipped.  The read→mutate→write window holds a cross-process lock so
    a second writer (e.g. the host's config tools) can't clobber the change
    (F8).
    """
    with config_read_modify_write(current_path()):
        raw = _load_raw()
        _normalize_legacy_servers(raw)
        servers = _servers_section(raw, section)
        existing = servers.get(name, {})
        server_entry: dict = dict(existing) if isinstance(existing, dict) else {}
        for key, value in entry.items():
            if value is None:
                continue
            if key == "enabled" and value is True:
                server_entry.pop("enabled", None)
                continue
            server_entry[key] = value
        servers[name] = server_entry
        write_config(current_path(), raw)


def remove_server_entry(name: str) -> bool:
    """Remove *name* from the config; True if it existed."""
    with config_read_modify_write(current_path()):
        raw = _load_raw()
        _normalize_legacy_servers(raw)
        section = _find_server_section(raw, name)
        if section is None:
            return False
        del _servers_section(raw, section)[name]
        write_config(current_path(), raw)
    return True


def set_server_enabled(name: str, enabled: bool) -> bool:
    """Persist the enabled flag for *name*; True if it existed.

    enabled=True removes the flag (enabled is the default); enabled=False
    writes ``"enabled": false``.
    """
    with config_read_modify_write(current_path()):
        raw = _load_raw()
        _normalize_legacy_servers(raw)
        section = _find_server_section(raw, name)
        if section is None:
            return False
        servers = _servers_section(raw, section)
        if enabled:
            servers[name].pop("enabled", None)
        else:
            servers[name]["enabled"] = False
        write_config(current_path(), raw)
    return True


def _load_raw() -> dict:
    return read_config(current_path())


# ── Raw json5 entry → ServerConfig ─────────────────────────────────────


def resolve_server_config(name: str, raw_entry: dict):
    """Build a :class:`~mcp_gateway.connection.ServerConfig` from a raw entry.

    Resolves ``${VAR}`` refs in ``env`` and ``auth.client_*`` fields.
    Args/url/headers keep their embedded refs — the connection layer
    resolves them at connect time (unchanged behaviour).
    """
    from slife.plugins.mcp_gateway.connection import ServerConfig

    env = raw_entry.get("env")
    if isinstance(env, dict):
        env = {k: _resolve_secret(str(v)) for k, v in env.items()}
    auth = raw_entry.get("auth")
    if isinstance(auth, dict):
        auth = dict(auth)
        for auth_key in ("client_id", "client_secret"):
            if auth_key in auth and isinstance(auth[auth_key], str):
                auth[auth_key] = _resolve_secret(auth[auth_key])
    return ServerConfig(
        name=name,
        command=str(raw_entry.get("command", "")),
        args=[str(a) for a in (raw_entry.get("args") or [])],
        env=env,
        url=str(raw_entry.get("url", "")),
        headers=_dict_copy(raw_entry.get("headers")),
        enabled=raw_entry.get("enabled", True) is not False,
        description=str(raw_entry.get("description", "")),
        auth=auth,
        source=_dict_copy(raw_entry.get("source")),
        os_paths=bool(raw_entry.get("os_paths", False)),
        # Config key is `autoload` — a valid json5 identifier, so it needs no
        # quotes in tools.json5 (a dash would require quoting).
        auto_load=raw_entry.get("autoload") is True,
    )


def _dict_copy(value):
    return dict(value) if isinstance(value, dict) else value


# ── REST-API helpers (rest APIs are ordinary uvx mcp-openapi-proxy entries) ─
# mcp-openapi-proxy (PyPI) takes NO CLI args — everything rides env vars.
# Low-Level Mode (the proxy's default; OPENAPI_SIMPLE_MODE is left unset)
# exposes one typed MCP tool per OpenAPI endpoint.  The rest_api_set params
# map 1:1 onto its env: spec_url → OPENAPI_SPEC_URL, base_url →
# SERVER_URL_OVERRIDE, api_key → API_KEY (sent as a Bearer auth header by
# default, matching the old ``Authorization: Bearer`` header).

OPENAPI_PROXY_COMMAND = "uvx"
OPENAPI_PROXY_MARKER = "mcp-openapi-proxy"
SPEC_URL_ENV = "OPENAPI_SPEC_URL"
BASE_URL_ENV = "SERVER_URL_OVERRIDE"
API_KEY_ENV = "API_KEY"


def build_rest_api_entry(
    spec_url: str,
    base_url: str,
    api_key: str = "",
    description: str = "",
    source: dict | None = None,
) -> dict:
    """Build a ``uvx mcp-openapi-proxy`` server entry for a REST API.

    Tool prefixing is the gateway's job (``<server_name>__tool`` from the
    config entry name) — the proxy needs no name of its own.  The api_key
    is referenced as ``${<api_key>}`` in the proxy's ``API_KEY`` env var so
    the secret itself stays in the credential store (credstore), never in
    the config file.
    """
    env = {SPEC_URL_ENV: spec_url, BASE_URL_ENV: base_url}
    if api_key:
        env[API_KEY_ENV] = f"${{{api_key}}}"
    entry: dict = {
        "command": OPENAPI_PROXY_COMMAND,
        "args": [OPENAPI_PROXY_MARKER],
        "env": env,
    }
    if description:
        entry["description"] = description
    src = {"type": "rest_api", **(source or {})}
    stamped = with_fetched_at(src)
    if stamped:
        entry["source"] = stamped
    return entry


def save_rest_api(
    name: str,
    spec_url: str = "",
    base_url: str = "",
    api_key: str = "",
    description: str = "",
    source: dict | None = None,
) -> bool:
    """Persist a REST API as a server entry in the ``rest-api`` section.

    Returns True when written.
    """
    entry = build_rest_api_entry(
        spec_url, base_url, api_key, description, source,
    )
    add_server_entry(name, entry, section="rest-api")
    logger.info("mcp_config_save_rest_api name=%s spec=%s", name, spec_url)
    return True


def remove_rest_api(name: str) -> bool:
    """Remove a REST API server entry. Returns True if it existed."""
    existed = remove_server_entry(name)
    if existed:
        logger.info("mcp_config_remove_rest_api name=%s", name)
    return existed


def _is_rest_api_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    source = entry.get("source")
    if isinstance(source, dict) and source.get("type") == "rest_api":
        return True
    command = entry.get("command")
    args = entry.get("args")
    env = entry.get("env")
    # Shape fallback for hand-edited entries that lost their source tag.
    if (
        command == OPENAPI_PROXY_COMMAND
        and isinstance(args, list) and OPENAPI_PROXY_MARKER in args
        and isinstance(env, dict) and SPEC_URL_ENV in env
    ):
        return True
    return False


def list_rest_apis() -> dict:
    """Server entries that are REST-API-backed (name → raw entry)."""
    return {
        name: entry
        for name, entry in servers().items()
        if _is_rest_api_entry(entry)
    }


def parse_rest_api_entry(entry: dict) -> dict:
    """Read ``spec_url/base_url/api_key`` back from a rest-api entry's env."""
    from slife.env import parse_env_ref

    result = {"spec_url": "", "base_url": "", "api_key": ""}
    env = entry.get("env") if isinstance(entry, dict) else None
    if not isinstance(env, dict):
        return result
    result["spec_url"] = str(env.get(SPEC_URL_ENV, ""))
    result["base_url"] = str(env.get(BASE_URL_ENV, ""))
    key_ref = env.get(API_KEY_ENV, "")
    if isinstance(key_ref, str):
        parsed = parse_env_ref(key_ref)
        if parsed is not None:
            result["api_key"] = parsed[0]
    return result