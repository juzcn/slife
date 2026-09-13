"""Structured logging for mcp_plugin.

Provides session/request correlation (``SessionFormatter``), subprocess
stderr relay helpers, secret sanitization, JSON response envelopes, and
root-logging configuration.  mcp-plugin is a built-in slife plugin, so its
log *directory* resolves like every built-in plugin's — see
:func:`resolve_log_dir`.

The formatting/relay layer is **not re-implemented here**: session/request
ids, ``SessionFormatter``, the noisy-logger policy, the stderr relay and
``configure_root_logging`` are the shared ``slife.logfmt`` implementations,
re-exported below so mcp-plugin binds to the same code the host uses (a fix
or a new silenced logger lands everywhere at once, never in two copies).
mcp-plugin keeps only what it adds: the ``ok_json`` / ``error_json``
response envelopes and its log-dir resolver.
"""

import json
from pathlib import Path

# Single binding to the shared implementation — module-level assignment
# re-exports, so ruff (F401) and pyright (reportUnusedImport) see each name
# as a public attribute while the import itself is used.
from slife import logfmt as _logfmt

FILE_LOG_FORMAT = _logfmt.FILE_LOG_FORMAT
SessionFormatter = _logfmt.SessionFormatter
configure_root_logging = _logfmt.configure_root_logging
get_request_id = _logfmt.get_request_id
get_session_id = _logfmt.get_session_id
init_session_id = _logfmt.init_session_id
read_stderr_lines = _logfmt.read_stderr_lines
sanitize_secrets = _logfmt.sanitize_secrets
set_session_id = _logfmt.set_session_id
silence_noisy_loggers = _logfmt.silence_noisy_loggers


# ── Log directory resolution ──────────────────────────────────────────


def resolve_log_dir() -> Path:
    """Return the log directory for mcp_plugin — the slife data-dir logs.

    Same resolution as every built-in plugin server: ``SLIFE_LOG_DIR`` when
    the host (slife) exported it (the per-session log then lands next to the
    main session log), else ``<data_dir>/logs`` (``~/.slife/logs`` in
    production).  File naming is unchanged (``{ts}_{agent}_{service}.log``) —
    mcp_plugin keeps its own plugin-named log file.
    """
    return _logfmt.resolve_log_dir()


# ── JSON response helpers ─────────────────────────────────────────────


def ok_json(**extra: object) -> str:
    """Render ``{"status": "ok", ...}`` — the standard success envelope.

    Keys with ``None`` values are omitted.  Output is indented and safe
    for display in TUI tool-result widgets.
    """
    payload: dict = {"status": "ok", **{k: v for k, v in extra.items() if v is not None}}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def error_json(message: str, **extra: object) -> str:
    """Render ``{"status": "error", "error": <message>, ...}``.

    The *message* parameter is required — every error must explain itself.
    Extra keys with ``None`` values are omitted.
    """
    payload: dict = {
        "status": "error",
        "error": message,
        **{k: v for k, v in extra.items() if v is not None},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)