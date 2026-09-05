"""mcp-plugin config — load/save ``mcp-plugin.json5``, path resolution, secrets.

Path precedence (one loader, every consumer) — mirrors credstore:
  1. ``$MCP_PLUGIN_FILE`` — explicit override (the host used to export this
     with its config dir; now it is a test/dev escape hatch only)
  2. slife project root (dev): CWD is the slife source root (``pyproject.toml``
     ``project.name == "slife"``) — ``./mcp-plugin.json5`` (credstore's
     ``is_slife_dev`` pattern)
  3. ``~/.mcp-plugin/mcp-plugin.json5`` (standalone default, credstore-style)

Server entries hold: ``command/args/env/url/headers/auth/description/enabled/source``
plus ``os_paths``.
``env`` and ``auth.client_id``/``client_secret`` support
``${VAR}`` / ``keyring:`` references resolved through **os.environ → credstore →
literal**.  REST APIs are ordinary ``npx anyapi-mcp-server`` entries tagged
``source.type == "rest_api"``.

The top-level ``embeddings`` section is the **fallback** embedding config: a
connecting host may pass its own endpoint via the standard ``initialize``
handshake's ``clientInfo``, which wins when present (see
:mod:`mcp_plugin.embeddings`).
"""

from __future__ import annotations

import json5
import logging
import os
import re
import tempfile
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Path resolution ────────────────────────────────────────────────────


def default_config_path() -> Path:
    """Standalone default: ``~/.mcp-plugin/mcp-plugin.json5``."""
    return Path.home() / ".mcp-plugin" / "mcp-plugin.json5"


def resolve_config_path() -> Path:
    """Return the mcp-plugin.json5 path for this process.

    Precedence (mirrors credstore's ``get_cryptfile_path``):
    ``$MCP_PLUGIN_FILE`` > slife project root (dev) > standalone default.
    """
    env = os.environ.get("MCP_PLUGIN_FILE")
    if env:
        return Path(env).expanduser()
    if is_slife_dev():
        return Path("mcp-plugin.json5")
    return default_config_path()


def is_slife_dev() -> bool:
    """Whether we're running from the slife source root (credstore-style).

    Returns True when the CWD contains a ``pyproject.toml`` with
    ``project.name == "slife"``.
    """
    try:
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("project", {}).get("name") == "slife"


# ── Reader / writer (atomic; parse failures surface) ───────────────────


class ConfigParseError(ValueError):
    """Raised when mcp-plugin.json5 exists but cannot be parsed.

    Distinct from ``FileNotFoundError`` (treated as a normal first-run state).
    A mutating caller that proceeded past a parse error would write back an
    empty dict via ``os.replace`` and destroy the whole config — so the
    parse failure must be surfaced, not swallowed.
    """


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def with_fetched_at(source: dict | None) -> dict | None:
    """Return a copy of *source* with a ``fetched_at`` timestamp added.

    Returns None if *source* is None or an empty dict.
    """
    if not source:
        return None
    result = dict(source)
    result.setdefault("fetched_at", now_iso())
    return result


def read_config(path: Path) -> dict:
    """Read and parse an mcp-plugin.json5 config file.

    Returns ``{}`` only when the file does not exist (first run).  A file
    that exists but cannot be parsed raises :class:`ConfigParseError`.
    """
    try:
        return json5.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("mcp_config_not_found path=%s", path)
        return {}
    except (ValueError, OSError) as e:
        logger.error("mcp_config_parse_error path=%s err=%s", path, e)
        raise ConfigParseError(f"Cannot parse config {path}: {e}") from e


_write_lock = threading.Lock()


def write_config(path: Path, raw: dict) -> None:
    """Atomically write *raw* to *path* (temp file + ``os.replace``).

    Creates the parent directory on first write.  Atomic replace means a
    reader never sees a truncated/interleaved file; the lock serializes
    in-process writers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json5.dumps(raw, indent=2, trailing_commas=False, ensure_ascii=False)
    with _write_lock:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            # Preserve the existing file's mode — mkstemp creates 0600, which
            # would silently tighten a previously readable config.
            if path.exists():
                try:
                    os.chmod(tmp, path.stat().st_mode & 0o7777)
                except OSError:
                    pass
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ── Secret resolution (os.environ → credstore → literal) ───────────────

_ENV_REF = re.compile(r"\$\{(\w+)\}")


def _is_env_ref(value: str) -> bool:
    """True if *value* is a pure ``${VAR}`` reference (no surrounding text)."""
    return bool(_ENV_REF.fullmatch(value))


def _try_credstore_lookup(key: str) -> str | None:
    """Look up an env-var name in the credential store (credstore).

    The env var name IS the credential-store key — e.g. ``GITHUB_TOKEN``.
    Returns the credential value, or None if not found / unavailable.
    """
    try:
        from credstore import get_credential
        return get_credential(key)
    except Exception:
        return None


def _resolve_embedded_refs(value: str) -> str:
    """Resolve embedded ``${VAR}`` refs through os.environ → credstore."""
    def _replace(m: re.Match) -> str:
        var = m.group(1)
        env_val = os.environ.get(var)
        if env_val:
            return env_val
        cred_val = _try_credstore_lookup(var)
        if cred_val:
            return cred_val
        return m.group(0)  # unresolved — leave as-is
    return _ENV_REF.sub(_replace, value)


def _resolve_secret(value: str, *, accept_keyring_uri: bool = False) -> str:
    """Resolve a secret value through the full resolution chain.

    1. ``keyring:`` URI → credstore (only when *accept_keyring_uri* is True)
    2. ``${VAR}`` → os.environ → credstore
    3. plaintext → as-is
    """
    if accept_keyring_uri:
        try:
            from credstore import is_keyring_uri, resolve_uri
            if is_keyring_uri(value):
                return resolve_uri(value)
        except ImportError:
            pass
    if value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        env_val = os.environ.get(var_name)
        if env_val:
            return env_val
        cred_val = _try_credstore_lookup(var_name)
        if cred_val:
            return cred_val
    return value


# ── Module-level current path + raw accessors ──────────────────────────

_CURRENT_PATH: Path | None = None


def set_config_path(path: Path | str | None = None) -> Path:
    """Pin the config path in use (e.g. from ``load_config(path)``).

    With no *path*, re-resolves from ``$MCP_PLUGIN_FILE`` / slife project /
    standalone default.
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
    servers = raw.get("servers", {})
    return servers if isinstance(servers, dict) else {}


def servers() -> dict:
    """All server entries (name → raw entry)."""
    return _servers_dict(load_config())


def get_server(name: str) -> dict | None:
    """A single raw server entry, or None."""
    return servers().get(name)


def count_servers() -> int:
    """Number of configured servers."""
    return len(servers())


# ── Server-entry persistence (shared by CLI + server management tools) ──


def add_server_entry(name: str, entry: dict) -> None:
    """Upsert *entry* for *name* with merge semantics.

    Existing fields not explicitly provided are preserved.  ``enabled: True``
    (the default) removes a stale ``enabled: false`` flag; ``None`` values
    are skipped.
    """
    raw = _load_raw()
    servers = _servers_dict(raw)
    if not isinstance(raw.get("servers"), dict):
        raw["servers"] = servers
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
    raw = _load_raw()
    servers = _servers_dict(raw)
    if name not in servers:
        return False
    del servers[name]
    write_config(current_path(), raw)
    return True


def set_server_enabled(name: str, enabled: bool) -> bool:
    """Persist the enabled flag for *name*; True if it existed.

    enabled=True removes the flag (enabled is the default); enabled=False
    writes ``"enabled": false``.
    """
    raw = _load_raw()
    servers = _servers_dict(raw)
    if name not in servers:
        return False
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
    """Build a :class:`~mcp_plugin.connection.ServerConfig` from a raw entry.

    Resolves ``${VAR}``/``keyring:`` refs in ``env`` and ``auth.client_*``
    fields.  Args/url/headers keep their embedded refs — the connection
    layer resolves them at connect time (unchanged behaviour).
    """
    from mcp_plugin.connection import ServerConfig

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
        auto_load=raw_entry.get("auto-load") is True,
    )


def _dict_copy(value):
    return dict(value) if isinstance(value, dict) else value


# ── REST-API helpers (rest_apis are ordinary npx anyapi server entries) ─

_ANYAPI_MARKER = "anyapi-mcp-server"


def build_rest_api_entry(
    name: str,
    spec_url: str,
    base_url: str,
    api_key: str = "",
    description: str = "",
    source: dict | None = None,
) -> dict:
    """Build a server entry that serves *name* via ``npx anyapi-mcp-server``.

    The api_key is referenced as ``Authorization: Bearer ${<api_key>}`` so
    the secret itself stays in the credential store (credstore), never in
    the config file.
    """
    args = [
        "-y", _ANYAPI_MARKER,
        "--name", name,
        "--spec", spec_url,
        "--base-url", base_url,
    ]
    if api_key:
        args.append("--header")
        args.append(f"Authorization: Bearer ${{{api_key}}}")
    entry: dict = {"command": "npx", "args": args}
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
    """Persist a REST API as a server entry. Returns True when written."""
    entry = build_rest_api_entry(
        name, spec_url, base_url, api_key, description, source,
    )
    add_server_entry(name, entry)
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
    if (
        isinstance(command, str) and command == "npx"
        and isinstance(args, list) and _ANYAPI_MARKER in args
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


def parse_anyapi_args(entry: dict) -> dict:
    """Re-parse ``--spec/--base-url/--header`` from an anyapi arg vector."""
    result = {"spec_url": "", "base_url": "", "api_key": ""}
    args = entry.get("args") if isinstance(entry, dict) else []
    if not isinstance(args, list):
        return result
    for i, arg in enumerate(args):
        if arg == "--spec" and i + 1 < len(args):
            result["spec_url"] = str(args[i + 1])
        elif arg == "--base-url" and i + 1 < len(args):
            result["base_url"] = str(args[i + 1])
        elif arg == "--header" and i + 1 < len(args):
            header = str(args[i + 1])
            if header.startswith("Authorization: Bearer ${") and header.endswith("}"):
                result["api_key"] = header[len("Authorization: Bearer ${"):-1]
    return result