"""Code execution tools.

Tools:
    execute_shell          — run shell commands (default timeout 30s)
    run_python_script      — run Python scripts with JSON arguments
    install_python_package — install PyPI packages into slife's environment
"""

import asyncio
import base64
import locale
import logging
import math
import os
import sys

from slife.platform import _resolve_skill_script, kill_process_tree
from slife.logfmt import sanitize_secrets
from slife.tools.base import Tool
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)

#: Read-side output budget for exec tools.  ``communicate()`` accumulated the
#: ENTIRE stdout+stderr before the downstream truncation policy applied — the
#: LLM can ask for a big file dump and the tool would buffer all of it in
#: memory.  These cap how much is retained while streaming: head + tail with
#: the dropped middle reported.  Chosen well above normal tool output but
#: small enough that a runaway `yes`/log-flood can't grow the process
#: unboundedly.  (F7)
_STREAM_HEAD = 100 * 1024
_STREAM_TAIL = 20 * 1024


async def _read_bounded(stream, head: int = _STREAM_HEAD, tail: int = _STREAM_TAIL):
    """Read *stream* to EOF keeping the first *head* and last *tail* bytes.

    Returns ``(head_bytes, tail_bytes, dropped)`` — the middle beyond
    ``head + tail`` is discarded and the byte count reported so the caller
    can append a truncation marker inside the tool output (the downstream
    truncation stays intact; this is purely a memory bound on the read side).
    """
    head_buf = bytearray()
    tail_buf = bytearray()
    total = 0
    async for chunk in stream:
        total += len(chunk)
        # fill the head up to its cap, keeping leftover for the tail window
        if len(head_buf) < head:
            take = min(head - len(head_buf), len(chunk))
            head_buf += chunk[:take]
            chunk = chunk[take:]
        tail_buf += chunk
        if len(tail_buf) > tail:
            del tail_buf[: len(tail_buf) - tail]
    retained = len(head_buf) + min(len(tail_buf), tail)
    return bytes(head_buf), bytes(tail_buf), total - retained


def _merge_text(
    head: bytes, tail: bytes, dropped: int, *, codec: str | None = None,
) -> str:
    """Decode a bounded head+tail pair into one output string, marking the
    dropped middle explicitly inside the tool result (the tool-result policy
    requires truncation to be visible, not silent).  *codec* ``None`` picks
    the shell output codec (OEM on Windows); script streams pass ``"utf-8"``.
    """
    if codec is None:
        codec = _shell_output_codec()
    text = (head + tail).decode(codec, errors="replace")
    if dropped > 0:
        text += f"\n… (truncated: {dropped} bytes of streamed output not retained)"
    return text


async def _read_stdout_stderr(process):
    """Read both subprocess pipes bounded, returning their head/tail/dropped.

    ``communicate()`` buffers everything; this drains both streams with the
    read-side cap, concurrently, so a big-file dump can't OOM the host and a
    full pipe can't deadlock the read.
    """
    (out_h, out_t, out_d), (err_h, err_t, err_d) = await asyncio.gather(
        _read_bounded(process.stdout),
        _read_bounded(process.stderr),
    )
    return out_h, out_t, out_d, err_h, err_t, err_d


class _CapturedRun:
    """Result of :func:`_run_captured` — merged bounded streams + exit code."""

    __slots__ = ("stdout", "stderr", "returncode")

    def __init__(self, stdout: str, stderr: str, returncode: int | None) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


async def _run_captured(
    argv: list[str],
    *,
    timeout: float | None = None,
    codec: str | None = None,
) -> _CapturedRun:
    """Spawn *argv* and read both pipes with the shared bounded head+tail spine.

    The three execution tools used to spell this outline out individually:
    spawn in its own process group, stream with a read-side memory bound
    (never ``communicate()``'s buffer-everything), and kill the WHOLE tree on
    timeout OR cancel so no child (yt-dlp/ffmpeg mid-download, uv mid-install)
    survives as an orphan writing to the console/TUI.  ``TimeoutError`` /
    ``CancelledError`` are re-raised AFTER the kill; each caller renders its
    own message.

    Args:
        argv: Command vector (spawned via ``create_subprocess_exec``).
        timeout: Optional overall bound on the stream read (``wait_for``);
                 ``None`` relies on the loop's tool-timeout cancelling the read.
        codec: Stream decode codec — ``None`` → the shell output codec
               (:func:`_shell_output_codec`), ``"utf-8"`` for scripts.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group on POSIX so timeout/cancel can kill the whole
        # tree (sh + children, a mid-install uv) — see kill_process_tree.
        start_new_session=True,
    )
    try:
        read = _read_stdout_stderr(proc)
        if timeout is not None:
            read = asyncio.wait_for(read, timeout=timeout)
        out_h, out_t, out_d, err_h, err_t, err_d = await read
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Kill the whole tree — a bare process.kill() only kills the shell
        # and orphans children that keep writing to the console.
        await kill_process_tree(proc)
        raise
    return _CapturedRun(
        stdout=_merge_text(out_h, out_t, out_d, codec=codec),
        stderr=_merge_text(err_h, err_t, err_d, codec=codec),
        returncode=proc.returncode,
    )


def _shell_argv(command: str) -> list[str]:
    """Build argv that runs *command* in the shell the prompt claims.

    ``asyncio.create_subprocess_shell`` runs ``COMSPEC`` (cmd.exe) on Windows
    even when the detected shell is PowerShell, so the LLM's PS commands
    failed while the prompt said ``powershell``.  Run the detected shell
    explicitly so annotation and behaviour agree everywhere (native Windows,
    WSL, POSIX).
    """
    if os.name == "nt":
        from slife.platform import detect_current_shell
        if detect_current_shell() == "powershell":
            # -EncodedCommand (UTF-16LE base64) sidesteps all quoting issues
            # with arbitrary command strings.  Prepend $ProgressPreference so
            # PowerShell's "preparing module for first use" progress record is
            # not serialized as CLIXML noise on stderr when stdout/stderr are
            # pipes (no console) — it pollutes every command's stderr.
            #
            # Force redirected output to UTF-8 regardless of the console code
            # page: Windows PowerShell 5.1 echoes the OEM page (cp936 on a
            # zh-CN box) to a pipe, PowerShell 7 writes UTF-8 — making the
            # decode side locale-dependent.  Pin OutputEncoding here so the
            # caller can always decode UTF-8.
            script = (
                "$ProgressPreference = 'SilentlyContinue'; "
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; " + command
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            return [
                "powershell", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", encoded,
            ]
        return ["cmd", "/c", command]
    # POSIX (incl. WSL): $SHELL — the same value the prompt reports.
    return [os.environ.get("SHELL", "/bin/sh"), "-c", command]


def _shell_output_codec() -> str:
    """Codec for decoding shell output bytes.

    On Windows, cmd.exe writes the console/OEM code page to a pipe (GBK/cp936
    on a zh-CN locale) — decoding as UTF-8 produces mojibake.
    ``locale.getpreferredencoding(False)`` returns the right codec for that
    path.  The PowerShell invocation in :func:`_shell_argv` pins its child to
    UTF-8 output, so the shell that launches here is never a PowerShell;
    POSIX shells emit UTF-8.
    """
    if os.name == "nt":
        return locale.getpreferredencoding(False) or "utf-8"
    return "utf-8"


# ═══════════════════════════════════════════════════════════════════════
# execute_shell
# ═══════════════════════════════════════════════════════════════════════

class ShellTool(Tool):
    """Execute a shell command via the detected shell (PowerShell/cmd on
    Windows, $SHELL on POSIX incl. WSL) — matching what the system prompt
    reports, so PS commands like ``Get-Date`` actually work."""

    name = "execute_shell"
    category = "Execution"
    description = "Run a shell command. Returns stdout + stderr. Default timeout from the registry (work.shell)."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Omit to use the default (registry work.shell); ≤0 = default (never instant)."},
        },
        "required": ["command"],
    }

    def __init__(self, timeout: int | None = None):
        # Call-time lookup (registry work.shell); ≤0 means "the tool default"
        # — the same contract the loop's `_timeout` mapping enforces.
        if timeout is None or timeout <= 0:
            # Registry value may be fractional (allowed; only >= 0 required) —
            # ceil so 30.5 → 31, and floor at 1 so a sub-1.0 or vanished
            # (0) value can't reach ``wait_for`` as timeout=0 → instant
            # TimeoutError on every call.
            timeout = max(1, math.ceil(_timeouts.timeouts.work.shell))
        self.timeout: int = timeout

    @classmethod
    def from_config(cls, cfg, config, ctx=None):
        # ``cfg.get("timeout")`` is the per-tool USER override (slife.json5
        # tools section); absent → registry default via the ctor.
        raw = cfg.get("timeout")
        tool = cls(timeout=int(raw) if raw is not None else None)
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool

    async def execute(self, **kwargs) -> str:
        command: str = kwargs["command"]
        timeout: int = kwargs.get("timeout", self.timeout)
        # A 0/negative timeout would reach the read loop as
        # ``wait_for(..., timeout=0)`` → INSTANT TimeoutError, the opposite
        # of the "0 = no timeout" intent (B2/B3).  Treat ≤0 as "use the
        # tool default" — same contract the loop's native mapping enforces
        # and the a2a tools document ("≤0 = default").  Defense in depth:
        # a direct caller who bypasses the loop gets the same semantics.
        if timeout <= 0:
            timeout = self.timeout
        logger.debug("shell_exec cmd=%.200s timeout=%d", sanitize_secrets(command), timeout)

        # Run the detected shell (not COMSPEC=cmd.exe on Windows) so the
        # command executes in the same shell the system prompt reports.
        argv = _shell_argv(command)
        try:
            # Shared spine: spawn in its own group, bounded stream read, and
            # tree-kill on timeout/cancel (see _run_captured).  Shell output
            # keeps the OEM codec — the default when no codec is passed.
            run = await _run_captured(argv, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("shell_timeout timeout=%ds cmd=%.200s", timeout, sanitize_secrets(command))
            return f"Error: Command timed out after {timeout}s"

        result = run.stdout
        if run.stderr:
            result += f"\n[stderr]\n{run.stderr}"
        if not result.strip():
            result = f"Command completed with exit code {run.returncode} (no output)"
        elif run.returncode:
            # Non-zero exit WITH output — surface the code so the LLM can tell
            # the difference (F7); the plain-output shape silently hid it.
            result += f"\n[exit {run.returncode}]"

        logger.debug("shell_done exit=%d out_len=%d err_len=%d",
                     run.returncode or 0, len(run.stdout), len(run.stderr))
        return result


# ═══════════════════════════════════════════════════════════════════════
# run_python_script
# ═══════════════════════════════════════════════════════════════════════

def _parse_input(input_str: str) -> tuple[str, str]:
    """Split input into (script_or_code, json_args).

    JSON args follow the script path after whitespace (``script.py {"a": 1}``).
    Only a ``{``/``[`` preceded by whitespace starts the args block — a ``[``
    *inside* the script path (``C:\\code\\my[2024]\\run.py``) is not a
    delimiter and must not split the path.
    """
    for i, ch in enumerate(input_str):
        if ch in ("{", "["):
            if i == 0 or input_str[i - 1].isspace():
                return input_str[:i].strip(), input_str[i:].strip()
    return input_str.strip(), ""


class RunPythonScriptTool(Tool):
    """Run a Python script with arguments, or inline code with -c."""

    name = "run_python_script"
    category = "Execution"
    description = (
        "Run a Python script (path + JSON args) or inline code ('-c <code>')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Script path [+ JSON args], or '-c <code>'.",
            },
        },
        "required": ["script"],
    }

    async def execute(self, **kwargs) -> str:
        input_str = kwargs["script"]

        if input_str.startswith("-c ") or input_str.startswith("-c"):
            code = input_str[2:].strip()
            # LLMs naturally write shell-style '-c "code"'.  Strip the
            # wrapping quotes, otherwise python -c gets a bare string-literal
            # expression and silently does nothing (exit 0, no output).
            if len(code) >= 2 and code[0] == code[-1] and code[0] in "\"'":
                code = code[1:-1]
            argv = [sys.executable, "-X", "utf8", "-c", code]
            logger.debug("run_python_script argv=%s", sanitize_secrets(str(argv)))
        else:
            script, args = _parse_input(input_str)
            script = _resolve_skill_script(script)
            argv = [sys.executable, "-X", "utf8", script]
            if args:
                argv.append(args)
            logger.debug("run_python_script argv=%s", sanitize_secrets(str(argv)))

        # The shared spine handles spawn, the bounded read, and the tree-kill on
        # the loop's tool-timeout cancel (see _run_captured).  Script streams
        # decode as UTF-8 (the script codec, not the shell OEM codec).
        run = await _run_captured(argv, codec="utf-8")
        out = run.stdout.strip()
        err = run.stderr.strip()

        if run.returncode != 0:
            if out:
                return out
            return f"Error (exit {run.returncode}): {err}" if err else f"Error (exit {run.returncode})"
        return out if out else f"Script completed with no output. stderr: {err}" if err else "Script completed with no output."


# ═══════════════════════════════════════════════════════════════════════
# install_python_package
# ═══════════════════════════════════════════════════════════════════════

class InstallPythonPackageTool(Tool):
    """Install Python packages into slife's environment via uv pip install."""

    name = "install_python_package"
    category = "Execution"
    description = "Install PyPI packages into slife's environment via uv pip install."
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Package specs, e.g. ['requests', 'beautifulsoup4>=4.12'].",
            },
            "timeout": {
                "type": "integer",
                "description": "Install deadline in seconds. Omit to use the default (registry work.pip_install); only a positive integer overrides, <=0 falls back to the default",
            },
        },
        "required": ["packages"],
    }

    async def execute(self, **kwargs) -> str:
        packages: list[str] = kwargs["packages"]
        if not packages:
            return "Error: no package names provided."
        logger.info("pip_install packages=%s", packages)

        # The `--` separator ends uv's option parsing: a package spec that
        # begins with `-` (e.g. "--index-url https://attacker") would otherwise
        # be consumed as a uv flag and redirect the install to a hostile index.
        t = kwargs.get("timeout")
        timeout = t if isinstance(t, int) and not isinstance(t, bool) and t > 0 else None
        timeout = timeout or _timeouts.timeouts.work.pip_install
        try:
            run = await _run_captured(
                ["uv", "pip", "install", "--python", sys.executable, "--", *packages],
                timeout=timeout,
                codec="utf-8",
            )
        except asyncio.TimeoutError:
            logger.warning("pip_install_timeout packages=%s", packages)
            return f"Error: pip install timed out after {timeout:g}s"
        out = run.stdout.strip()
        err = run.stderr.strip()

        if run.returncode == 0:
            logger.info("pip_install_done packages=%s", packages)
            return out or f"✓ Installed: {', '.join(packages)}"
        else:
            logger.warning("pip_install_failed packages=%s err=%s", packages, err)
            return f"Error installing {', '.join(packages)}:\n{err}" if err else f"Error installing {', '.join(packages)} (exit {run.returncode})"
