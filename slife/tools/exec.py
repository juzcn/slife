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
import os
import sys

from slife.platform import _resolve_skill_script, kill_process_tree
from slife.logfmt import sanitize_secrets
from slife.tools.base import Tool

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


def _merge_bounded(head: bytes, tail: bytes, dropped: int) -> str:
    """Decode a bounded head+tail pair into one output string, marking the
    dropped middle explicitly inside the tool result (the tool-result policy
    requires truncation to be visible, not silent)."""
    text = (head + tail).decode(_shell_output_codec(), errors="replace")
    if dropped > 0:
        text += f"\n… (truncated: {dropped} bytes of streamed output not retained)"
    return text


def _merge_utf8(head: bytes, tail: bytes, dropped: int) -> str:
    """Decode a bounded head+tail pair as UTF-8 with the dropped-middle marker."""
    text = (head + tail).decode("utf-8", errors="replace")
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
            script = "$ProgressPreference = 'SilentlyContinue'; " + command
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

    On Windows, Windows PowerShell 5.1 writes the console/OEM code page to a
    pipe (GBK/cp936 on a zh-CN locale) — decoding as UTF-8 produces mojibake.
    ``locale.getpreferredencoding(False)`` returns the right codec.  POSIX
    shells emit UTF-8.
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
    description = "Run a shell command. Returns stdout + stderr. Default timeout 30s."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."},
        },
        "required": ["command"],
    }

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg, config, ctx=None):
        tool = cls(timeout=cfg.get("timeout", 30))
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool

    async def execute(self, **kwargs) -> str:
        command: str = kwargs["command"]
        timeout: int = kwargs.get("timeout", self.timeout)
        logger.debug("shell_exec cmd=%.200s timeout=%d", sanitize_secrets(command), timeout)

        # Run the detected shell (not COMSPEC=cmd.exe on Windows) so the
        # command executes in the same shell the system prompt reports.
        argv = _shell_argv(command)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group on POSIX so a timeout can kill the whole
            # tree (sh + children like yt-dlp/ffmpeg) — see _kill_process_tree.
            start_new_session=True,
        )
        try:
            # Stream the pipes with a read-side memory bound instead of
            # communicate()'s buffer-everything — a large dump must not grow
            # the process unboundedly (F7).  Reading both streams concurrently
            # (gather) so neither pipe fills while we wait on the other.
            stdout_head, stdout_tail, stdout_dropped, \
                stderr_head, stderr_tail, stderr_dropped = await asyncio.wait_for(
                    _read_stdout_stderr(process), timeout=timeout,
                )
            output = _merge_bounded(stdout_head, stdout_tail, stdout_dropped)
            err_output = _merge_bounded(stderr_head, stderr_tail, stderr_dropped)
        except asyncio.TimeoutError:
            # Kill the whole tree — a bare process.kill() only kills the
            # shell and orphans yt-dlp/ffmpeg, which keep writing to the
            # console and garble the TUI.
            await kill_process_tree(process)
            logger.warning("shell_timeout timeout=%ds cmd=%.200s", timeout, sanitize_secrets(command))
            return f"Error: Command timed out after {timeout}s"

        result = output
        if err_output:
            result += f"\n[stderr]\n{err_output}"
        if not result.strip():
            result = f"Command completed with exit code {process.returncode} (no output)"
        elif process.returncode:
            # Non-zero exit WITH output — surface the code so the LLM can tell
            # the difference (F7); the plain-output shape silently hid it.
            result += f"\n[exit {process.returncode}]"

        logger.debug("shell_done exit=%d out_len=%d err_len=%d",
                     process.returncode or 0, len(output), len(err_output))
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

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group on POSIX so cancel/timeout can kill the
            # whole tree — see _kill_process_tree.
            start_new_session=True,
        )
        try:
            out_h, out_t, out_d, err_h, err_t, err_d = await _read_stdout_stderr(proc)
        except asyncio.CancelledError:
            # The loop's tool-timeout cancels the read — kill the child tree
            # so the running script (e.g. a yt-dlp download) doesn't survive
            # as an orphan writing to the console.
            await kill_process_tree(proc)
            raise
        # UTF-8 decode for the script stream (the script codec, not the shell
        # OEM codec) — head+tail with the dropped middle reported.
        out = _merge_utf8(out_h, out_t, out_d).strip()
        err = _merge_utf8(err_h, err_t, err_d).strip()

        if proc.returncode != 0:
            if out:
                return out
            return f"Error (exit {proc.returncode}): {err}" if err else f"Error (exit {proc.returncode})"
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
        proc = await asyncio.create_subprocess_exec(
            "uv", "pip", "install", "--python", sys.executable, "--", *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group so timeout/cancel can kill the whole tree —
            # otherwise a mid-install uv process survives as an orphan.
            start_new_session=True,
        )
        try:
            out_h, out_t, out_d, err_h, err_t, err_d = await asyncio.wait_for(
                _read_stdout_stderr(proc), timeout=120,
            )
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            logger.warning("pip_install_timeout packages=%s", packages)
            return f"Error: pip install timed out after 120s"
        except asyncio.CancelledError:
            await kill_process_tree(proc)
            raise
        out = _merge_utf8(out_h, out_t, out_d).strip()
        err = _merge_utf8(err_h, err_t, err_d).strip()

        if proc.returncode == 0:
            logger.info("pip_install_done packages=%s", packages)
            return out or f"✓ Installed: {', '.join(packages)}"
        else:
            logger.warning("pip_install_failed packages=%s err=%s", packages, err)
            return f"Error installing {', '.join(packages)}:\n{err}" if err else f"Error installing {', '.join(packages)} (exit {proc.returncode})"
