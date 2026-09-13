# A2A-over-MQTT — the adopted standard

## Design principle

> 有最新的、官方的、标准的、流行的 package,一定要使用,不要重复造轮子。 — *DESIGNER_NOTES.md:80*

We previously built our own A2A-over-MQTT binding (`slife/a2a/{wire,mqtt,transport,client}.py`): a custom
topic tree (`Slife/{agent}/tasks/inbox|result`, `slife/+/presence`), a custom `_slife` routing payload, a
message-vs-task bifurcation, and a non-standard LLM tool surface (`a2a_send_task` / `a2a_send_message` +
async/poll variants with sync-wait semantics). EMQX maintains the official **`a2a-over-mqtt`** SDK (PyPI,
Apache-2.0) implementing the A2A-over-MQTT transport profile. **We adopted it wholesale.**

- **Wire + protocol = 100 % the official SDK.** slife keeps only harness glue (plugin, drain, channel,
  task bookkeeping, presence display, the LLM tool surface).
- **Interop** with the EMQX ecosystem becomes possible (same topics, same wire, same presence).
- Standard A2A is **async-native**: `SendMessage` returns a task_id immediately; the result is pushed
  back later. slife's harness already had that push machinery (`[A2A-RESULT:…]` cut-in), so the model no
  longer needs sync-wait tools or a message/task distinction.

## The standard profile (SDK `a2a-over-mqtt` v0.1.0)

### Topics

`$a2a/v1/{category}/{org}/{unit}/{agent_id}` — `TopicSpace(org, unit)` builds them:

| Category | Topic | Role |
|---|---|---|
| discovery | `$a2a/v1/discovery/{org}/{unit}/{agent_id}` | Retained Agent Card; presence via MQTT v5 `a2a-status` user property + LWT |
| request | `$a2a/v1/request/{org}/{unit}/{agent_id}` | Inbound `SendMessage` / `CancelTask` |
| reply | `$a2a/v1/reply/{org}/{unit}/{requester}/{session}` | Requester's per-session response topic |
| event | `$a2a/v1/event/{org}/{unit}/{agent_id}` | Fire-and-forget broadcast (this binding: `…/broadcast`) |

### Wire

- **JSON-RPC 2.0** envelopes; method names `SendMessage` / `CancelTask`; task states map to
  `TASK_STATE_*` (`submitted` / `working` / `completed` / `failed` / `canceled` / `input-required` /
  `auth-required` / `rejected`); bind-specific error codes (−32700 parse, −32602 invalid params,
  −32003 request expired, −32004 responder unavailable, −32005 transport protocol error).
- **MQTT v5 `ResponseTopic` + `CorrelationData`** properties carry the requester's response topic and
  the correlation; a request lacking them is rejected (`validate_a2a_request`).
- **Lifecycle**: ack `submitted` → optional `working` streams → `artifactUpdate` (the result) →
  terminal `completed` / `canceled` / `failed`. Per-task dedup: in-flight registry + 300 s completed TTL,
  `context_id` mismatch rejected.
- **Peer identity**: the standard field `params.metadata.sender` (`A2ARequest.sender`) — slife always
  stamps it; an absent sender degrades to `"unknown"`.

### QoS & retry (the profile's values, as delivered by the SDK)

| Path | QoS |
|---|---|
| discovery registration | 1 (supports PUBACK reason codes) |
| request | 1 (mandatory) |
| reply | 1 (mandatory) |
| event (broadcast) | 0 (loss-tolerant, optional) |

Requester retry profile (`reply_first_timeout_ms=15000`, `stream_idle_timeout_ms=30000`,
`max_attempts=3`, exponential backoff 1000/2000/4000 ms ± 20 % jitter; retries reuse the **same
Task.id + context_id**) with a **fresh Correlation Data** per attempt. slife mirrors it in its push-model
outbound driver (see below) — the SDK's `Requester` itself is connection-per-call and would break our
persistent + async-push + late-result model, so it is not used.

### Interaction types (canonical A2A ↔ slife)

| Canonical interaction | Method | In slife |
|---|---|---|
| MESSAGE | `message/send` returning a bare `Message` | `a2a_send_message(message_type="message")` — a conversation, no task; the reply arrives as another message |
| TASK_REQUEST | `message/send` starting a task (→ `Task`) | `a2a_send_message(message_type="task_request")` — the only type that creates a task; result auto-pushes |
| TASK_RESPONSE | task status / artifact updates | `a2a_send_message(message_type="task_response")` completes an inbound task; produced by the responder as artifact + terminal on the response topic |
| TASK_GET | `tasks/get` | Not implemented (SDK gap) — the local task store covers it |
| TASK_CANCEL | `tasks/cancel` | `a2a_cancel_task` (`CancelTask`) |
| TASK_RESUBSCRIBE | `tasks/resubscribe` | Not applicable — MQTT reply sessions auto-deliver, nothing to resubscribe |

Method-name note: the SDK's JSON-RPC uses gRPC-style PascalCase (`SendMessage` / `CancelTask`);
the canonical spec's slash forms (`message/send`, `tasks/cancel`) are the same operations.

### SDK surface reused directly

All of these are the SDK's, imported as-is: `TopicSpace`, `MqttConfig` (+`.client_kwargs`),
`A2ARequest`/`A2AResponse`, `make_properties`/`get_correlation_data`/`get_response_topic`,
`validate_a2a_request`, `classify_reply`, `make_status_event`/`make_artifact_event`/`make_a2a_error`,
`build_card`/`parse_card`, error codes, `TERMINAL_KINDS`. `Responder` is subclassed for inbound serving
(only place the SDK class needs a harness bridge). **Gaps we own** (the SDK does not provide them):
agent/presence listing, async result push routing, out-of-band responder completion, cancel initiation,
broadcast. GetTask is unimplemented in the SDK — out of scope.

## slife architecture

```
        a2a plugin (slife.plugins.a2a.server, FastMCP subprocess)
                 │  LLM tools: a2a_send_message / a2a_cancel_task / a2a_list_agents
                 │            a2a_set_task_done / a2a_broadcast
                 │  internal: __a2a_drain_incoming / __check
        A2AMesh (slife/a2a/mesh.py)
           ├─ MeshResponder(Responder)  ── own connection: LWT + retained card + a2a-status,
           │                               request-topic subscribe, dedup, cancel, ack→artifact→terminal
           └─ thin outbound driver        ── own connection (id "-out"): sends, reply listening,
                                            presence discovery, broadcast; NO will, NO presence itself
```

**Two connections, distinct client ids** — the SDK `Responder` owns all presence publishing; the
outbound client only publishes requests and subscribes discovery + own reply sessions + events.
`connect()` does not return until BOTH are live: outbound subscribed **and** the responder has
subscribed its request topic (the SDK publishes our card only after subscribing, so our own online
card sighted on the discovery wildcard is the deterministic "inbound is live" signal). Without the
gate, an early send could be dropped before the responder subscribed.

### Inbound — `MeshResponder` (SDK `Responder` subclass)

`on_request(request, stream)` classifies the message by its `metadata.variables.message_type`
and blocks on a per-task completion bridge **only for `task_request`**. Only a task_request
creates a task. **Completion is the model's explicit action**, whenever the task is truly done
(a task may take many turns): sending `a2a_send_message(message_type="task_response", task_id=…,
message=…)` resolves the bridge → the SDK publishes artifact+terminal. A `message`-typed message
is a conversation (enqueued task-less, no bridge); a `task_response`-typed message is NOT a task
(acknowledged, nothing enqueued). A working keepalive keeps the standard stream alive while the
harness thinks. External (peer) cancellation is detected in a `try/finally` and surfaces a harness
preempt (`cancellations` → `inbox.cancel_correlation`, Esc-equivalent) unless shutting down.

### Outbound — thin driver on SDK primitives

- `a2a_send_message` is typed by `message_type`: `task_request` (only type that creates a task —
  one id serves as JSON-RPC `id`, `task_id`, reply correlation and store key; publish `SendMessage`);
  `message` (a conversation — no store record, replies push as messages); `task_response` completes
  an inbound task via the bridge (nothing is published as a new request).
- **Delivery retry profile** (the standard values above): re-publish the same payload under a fresh
  correlation when no first reply arrives within 15 s, backoff 1000/2000/4000 ms ± 20 %, ≤ 3 attempts.
  Any reply (a deduped `working` replay confirms delivery) stops the retries. A late reply still routes
  after exhaustion — the push model never abandons a result.
- **Reply routing**: non-terminal replies (submitted/working) are ignored; artifact text is held; a
  terminal reply (completed/cancelled/failed) records the store entry and enqueues the auto-push.
- `a2a_cancel_task` → standard `CancelTask`; `a2a_list_agents` → own card + presence cache;
  `a2a_broadcast` → fire-and-forget event, QoS 0.

### Presence

Peer cards arrive on `discovery_wildcard()` (retained, re-sent on every reconnect). `a2a-status`
maps to `online`/`offline` (`lwt` → offline); transitions fire
`on_agent_change` (own-topic echo filtered). A **cold retained offline card** — a peer already gone
before we subscribed (e.g. a dead card left by a past session) — is cached for the roster but
*not announced*: the feed carries only real transitions, so stale offline cards never fire fake
`✗ offline` lines. Status is **online/offline only** — the old heartbeat, peer-timeout sweep and
`busy` chip are gone (the profile has no heartbeat).

## LLM tool surface — the standard operations

| Tool | Standard op | Behavior |
|---|---|---|
| `a2a_send_message(agent, message, message_type, task_id)` | `message/send` (typed) | `task_request` (default) starts a task, returns its task_id immediately, result auto-pushes later; `task_response` completes the inbound task `task_id`; `message` sends a bare conversation. Only `task_request` creates a task. |
| `a2a_cancel_task(agent, task_id)` | `tasks/cancel` | Returns resulting status (cancelled / already-terminal / not_found). |
| `a2a_list_agents()` | discovery | JSON cards `{agent_name, status}`, own card first, peers by discovery. |
| `a2a_broadcast(event)` | event (QoS 0) | Fire-and-forget to every peer; no reply expected. |

No `timeout` parameter (nothing waits); no async/poll variants. The `_timeout` native-timeout
contract no longer applies to A2A tools.

## Markers

One envelope, `type` distinguishes the message:

| Marker | Meaning |
|---|---|
| `[A2A:{"from": …, "task_id": …, "type": …}]` | Every inbound A2A message. `type` = `task_request` (a task to answer), `task_response` (an auto-delivered result), `message` (a conversation), `broadcast` (an event). |

Machine-facing; `unwrap_info_envelope` strips it for the TUI (channel shows `A2A(<peer>)>`).

## Drain schema (`__a2a_drain_incoming`)

```json
{"tasks":  [{"type":"task","source":"<peer>","content":"<text>","task_id":"<uuid>"}],
 "events": [{"type":"event","source":"<peer>","content":"<text>"}],
 "presence":[{"type":"presence","event":"online|offline",
              "card":{"agent_name":"<peer>","status":"online|offline"}}],
 "cancellations":[{"type":"cancel","corr_id":"<task_id>"}],
 "task_completions":[{"corr_id":"<task_id>","result":"<text>","cancelled":false,"peer":"<peer>"}]}
```

Every inbound exchange is a task and always carries a task_id.

## Config

`A2AConfig`: `enabled` / `agent_name` / `transport` ("mqtt" only) / `broker_host` / `broker_port` /
**`org`** / **`unit`** (default `"default"`). `SLIFE_A2A_CONFIG` carries it to the plugin; the broker
TCP probe (`broker.py`) gates the plugin's start.

## Windows note (selector event loop)

aiomqtt's socket callbacks use `loop.add_reader`/`add_writer`, which the default Windows **Proactor**
loop does not support — its connections fail with `NotImplementedError`. The plugin process is
dedicated to the mesh (it spawns no subprocesses), so its own entry switches to the selector policy
(`server.main()` → `_ensure_windows_selector_loop`, deliberately not at module import — importing the
module in the pytest process must not change the suite-wide policy). The e2e tests run each scenario in
an explicitly-constructed `SelectorEventLoop` thread.

## What was deleted (no compatibility shims)

`slife/a2a/wire.py`, `mqtt.py`, `transport.py`, `client.py`; the `_slife` extension object; the
message-vs-task `a2a_kind` distinction and its `_message_sends`/`_poll_tasks`/`_reply_tos` machinery;
`a2a_send_task`, `a2a_send_task_async`, `a2a_send_message_async`, `a2a_get_task_result`, `a2a_list_tasks`,
`a2a_agent_card`; the `_timeout` contract on A2A tools; the heartbeat/presence-timeout watchdog.

## Tests

- Unit: `slife.a2a.mesh` (aiomqtt patched), plugin tool surface + drain + `__check`, config/task-store
  bookkeeping, harness poll-loop framing, marker builders, health/TUI/system-prompt consumers.
- Optional e2e (`tests/e2e/test_a2a_mesh_e2e.py`, `-m e2e`): two meshes against a live broker — presence,
  a task round-trip with out-of-band completion, a cancel round-trip, and broadcast; skipped when
  mosquitto is unreachable. Retained cards across runs make the ONLINE transition the wait predicate.

## Notes / accepted gaps

- **GetTask** is unimplemented in the SDK — remote task inspection is out of scope (local task-store
  bookkeeping covers the LLM).
- Same-name collision detection (two agents, one org/unit/name) is gone with the old client; our card
  carries `extensions.instance` as insurance, face detection is future work.
- Presence is online/offline only (no busy / timeout), by design of the profile.