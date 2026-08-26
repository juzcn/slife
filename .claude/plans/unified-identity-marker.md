# Unified identity marker — `[Kind:{json}]` (restore-time only) — IMPLEMENTED

## What was done

Implemented the unified identity marker, applied **at restore time only** (the live message stream is untouched — it keeps `[Turn: N]` / `[TrimContext: N]`).

### New module — `slife/a2a/markers.py`
- `render_marker(kind, **payload)` → `[Kind:{json}]`; empty payload → `[Kind:{}]`.
- `parse_marker(text)` → `(identity-dict | None, remainder)`; no marker = human (`None`).
- Tolerates legacy `[Heartbeat]` / `[Schedule …]` (no-JSON) prefixes.
- Kind constants: `Human, Wechat, Subagent, Heartbeat, Schedule, Remote, Unknown`.

### `slife/ui/restore.py`
- `marker_for_channel(channel, user_text)` — derives the marker from the persisted `diary.channel` (subagent → `[Subagent:{...}]`, heartbeat → `[Heartbeat:{}]`, schedule → `[Schedule:{"name":...}]`, wechat → `[Wechat:{}]`, remote id → `[Remote:{"peer_id":...}]`; human/empty → no marker).
- `_extract_name(user_text)` — best-effort worker/peer/task name from restored text.
- `_strip_legacy(text)` — drops a legacy `[Heartbeat]`/`[Schedule …]` prefix once folded into the marker.
- Phase-1 rebuild: prepends the marker to restored non-human user messages (before the `[Turn: N]` header), strips the legacy prefix, skips the header for heartbeat/schedule.
- UI-ops loop: `restore_prefix(raw, agent_name)` parses the marker → `Subagent`→`⚙️ subagent> `, `Wechat`→`You(Wechat)> `, `Remote`→`{peer_id}(a2a)`, `None`→`You> `. `is_synthetic`/`is_schedule` now come from `parse_marker` (kind Heartbeat/Schedule).
- Removed `_channel_by_row` and the `is_autonomous_trigger`/`is_schedule_trigger` text-sniffing imports.

### Tests
- New `tests/test_a2a_markers.py` — render/parse round-trip, human=None, empty `{}`, nested braces, legacy forms, kind constants.
- Updated `tests/test_ui_restore.py` — schedule trigger now renders `[Schedule:{"name":"daily_diary"}]` (legacy prefix folded).

## Out of scope (confirmed)

- **Live path untouched**: `heartbeat.py`, `schedules.py`, `service.py` A2A/subagent injection, `headless.py` unchanged. The live markers remain `[Turn: N]` / `[TrimContext: N]` only.
- **No `channel` column change**, no migration script, no schema change.
- Routing-in-marker (`reply_to` persistence) deferred — requires live A2A path changes.
- `is_autonomous_trigger`/`is_schedule_trigger` still used by `service.py:2001` (live persistence decision) — left in place.

## Verification

- `tests/test_a2a_markers.py`, `tests/test_ui_restore.py` pass.
- Full suite (`pytest tests/ -q`) passes — no regressions.
