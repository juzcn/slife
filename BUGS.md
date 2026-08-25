# BUGS

Backlogged defects — confirmed but not yet fixed. Each entry has enough
context to be picked up and fixed in one session. Prefer fixing, but if a
bug is being dropped for now, it lives here with a Status annotation.

| ID | Component | Status |
|----|-----------|--------|
| BUG-001 | `slife/agent/plugins.py` | backlogged 2026-08-25 |

---

## BUG-001 — `plugin.kill()` wait doesn't accept `timeout` on asyncio Process

**Status:** backlogged · not a correctness blocker · fixed opportunistically
if touched anyway.

### Symptom

Every shutdown logs a `TypeError` (caught and demoted to debug):

```
Traceback (most recent call last):
  File "D:\Dev\Workspace\slife\slife\agent\plugins.py", line 447, in kill
    p.wait(timeout=3.0)
TypeError: Process.wait() got an unexpected keyword argument 'timeout'
```

### Root cause

`PluginLifecycle.kill()` reaches into `self.process._process`, which is an
`asyncio.subprocess.Process`, and calls `p.wait(timeout=3.0)`. The asyncio
Process `wait()` takes **no arguments** — that signature belongs to
`subprocess.Popen.wait(timeout=…)`, which the call was evidently copied from.
The argument blows up on the call itself, so the intended 3-second bounded
wait never happens.

The same pattern appears twice more in
`slife/agent/service.py:kill_child_processes()` — `p.wait(timeout=3.0)` at
line 1476 (auto-discovered plugin sweep) and `proc._process.wait(timeout=2.0)`
at line 1494 (subagent sweep), both with `# type: ignore[call-arg]` marks that
flagged the mismatch but didn't fix the runtime behavior. Fix all three sites
together.

### Impact

Cosmetic. This is the sync crash-path safety net that runs from `main()`'s
`finally` after the event loop is gone. On Windows `terminate()` is
`TerminateProcess` (immediate hard kill), so nothing is actually orphaned and
no wait is really needed; the only effects are the noisy traceback in every
log and a dead code branch (the escalation to force-kill that the wait was
supposed to gate).

### Constraints for a fix

- Synchronous context — no running event loop, so `asyncio.wait_for` is out.
- `os.WNOHANG` does not exist on win32 (verified 2026-08-25), so a
  `waitpid(pid, WNOHANG)` poll is POSIX-only.
- `psutil` is not a dependency; don't add one for a crash path.
- On Windows a bounded wait is pointless — skip it (terminate == kill).

### Fix direction

Deterministic `terminate()` always, then:
- POSIX: poll `os.waitpid(pid, os.WNOHANG)` up to ~3s, then `kill()`.
- Windows: no wait, proceed straight past.

Or, per the unified-mechanism principle: a small loop-free sync helper in
`slife/platform.py` next to `terminate_process()`, used by all three call
sites.

### First seen

`logs/20260825_114028_slife.log` at shutdown (2026-08-25 11:48:31).