# Subagent LLM fail-fast + stream timeout — IMPLEMENTED

## What was done

Subagents now fail fast on LLM errors and cap a single stream call, so a
raised provider error / silent stall surfaces as a **pushed-back error
result** to the parent inbox instead of retrying a flaky provider or hanging
forever. The main agent keeps its current behavior (retry transient
transport errors, show raw error, wait for user).

### `slife/agent/loop.py`
- `AgentLoop.__init__`: new `stream_timeout: float | None = None` and
  `stream_max_retries: int | None = None` (defaults to
  `_LLM_STREAM_MAX_RETRIES`).
- `_consume_stream(...)` — new helper extracted from the `async for` body so
  `asyncio.wait_for` can cap the whole iteration. Takes an `emitted: list[bool]`
  one-element holder so "partial output was shown" survives a mid-stream raise
  (retry path needs it).
- `_process_stream(...)`:
  - when `stream_timeout` set, wraps `_consume_stream` in
    `asyncio.wait_for(..., timeout=...)`; a stall becomes
    `TimeoutError("LLM stream timed out after Ns")` (not retryable →
    propagates immediately).
  - `stream_max_retries == 0` disables retry — one attempt, then the error
    wraps to `RuntimeError("LLM stream failed after 1 attempts: …")` and
    propagates (fail-fast).
  - retry branch reads `emitted[0]` (not a local) for `on_stream_retry`.

### `slife/agent/service.py`
- `AgentLoop(...)` construction: when `is_subagent`, passes
  `stream_timeout=(config.subagent_config or {}).get("task_timeout", 120)`
  and `stream_max_retries=0`; main agent keeps defaults (`None`).

### `tests/test_loop.py`
- `test_stream_timeout_turns_stall_into_error` — a never-yields stream +
  `stream_timeout=0.05` → `TimeoutError`.
- `test_stream_max_retries_zero_fails_fast` — a retryable transport error +
  `stream_max_retries=0` → one call, wrapped `RuntimeError`.
- Existing retry tests use default `AgentLoop(...)` (main-agent defaults) and
  pass unchanged.

## Verification

- `tests/test_loop.py`: 50 passed (48 original + 2 new).
- Full suite: **2261 passed** — no regressions.

## Design notes

- Main agent unchanged (defaults preserved).
- A `TimeoutError`/`RuntimeError` is not a `BadRequestError`, so the inbox
  keeps history intact and routes it via `on_reply("Error: …")` → the
  subagent `_reply` writes it back + `worker/complete` → parent stores
  `failed` and auto-pushes the error text to the parent inbox.
