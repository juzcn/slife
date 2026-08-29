# Slife — Glossary of Terms

*Second edition, 2026-08-24*

---

## Preface: Scope and Purpose

> **This glossary is the authoritative terminology reference for the Slife
> project.** README.md and DESIGN.md defer to it: where a term is defined
> here, the other documents use the definition without restating it. When a
> term's meaning changes, it changes here first, and the other documents
> follow.

This glossary defines the vocabulary of the Slife project, in two layers.

**Part I** and **Part II** describe the terms that a language model encounters
while operating as a Slife agent: the system prompt, the schemas of the tools
it may call, and the machine-generated annotations embedded in its messages.
Each entry states what the term *means in operation* — the object it denotes,
the role it plays, or the rule that governs it — and, where useful, points to
related terms. This layer is deliberately independent of any particular
implementation: it describes concepts, not code.

**Part III** records the developer-facing terms used in the design and the
user documentation — the architecture, the extension contract, and the
notation that governs the codebase. These are the terms a contributor or
operator encounters when reading, configuring, or extending Slife.

Where the two layers share a word (for example, *tool* or *memory*), the
model-facing meaning is defined in Part II and the developer-facing sense in
Part III; the cross-references keep the reader oriented.

---

## Part I — Conceptual Foundation

The agent's world is organised around four objects, three of which the agent
manages directly and one of which is the persistent record of its life:

1. **Message** — the atomic unit exchanged with the model. A message has one
   of four roles — system, user, assistant, or tool — and a tool call always
   travels with its matching tool result.
2. **Turn** — the unit of interaction: one user message together with the
   assistant's complete response to it, tool calls and results included. The
   system manages interaction in turns, and every completed turn is retained
   permanently.
3. **LLM Context** — everything submitted to the model on a single call: the
   tool schemas, the system prompt, the historical turns, and the current
   turn. The context is the agent's working set; it is finite, and it is
   maintained dynamically within a fixed window.
4. **Memory** — the persistent layer that outlives the working set. Memory
   has two stores: the **Turns DB**, which holds the full turn history, and
   the **File Cabinet**, which holds notes, diaries, and saved files.

The distinction between *context* and *memory* is central: the context may be
trimmed or cleared without loss, because memory retains everything; memory is
never destroyed by the management of context.

---

## Part II — Glossary

### A

**Active model**
The model currently selected to produce responses. It determines the
available input modalities (for example, whether images may be attached).
Switching the active model takes effect on the following turn. *See also*
Model ref; Provider.

**Agent**
The named entity whose identity, environment, and capabilities the model
inhabits. The agent is framed as an autonomous being — "silicon-based life" —
with a memory that begins at a recorded point in time.

**Agent card**
A short description of an agent on the mesh — its name and its status —
used for discovery and for checking whether a peer is reachable. *See also*
A2A mesh; Peer.

**Agent worker**
*See* Subagent.

**Annotation**
Machine-generated metadata embedded in a message, distinguishable from the
message's own content. Annotations are not the words of the user or the
assistant; they are read as information about the message. *See also* Turn
footnote; Trim note; Heartbeat marker.

**Approval prompt**
A user confirmation requested before a tool executes. When a tool call is
marked for approval, the human is shown the call and must consent before it
runs. *See also* Tool meta-parameters.

**A2A mesh**
The agent-to-agent network over which agents exchange tasks. The mesh has a
broker (a message bus), peers, and a set of operations for sending tasks,
listening for results, cancelling, broadcasting, and discovering agents.
The transport is internal to the mesh; the tool surface is transport-agnostic.
*See also* Peer; Presence; Task (mesh).

**Async task**
A tool call executed in the background rather than in the main flow. An
async task returns a task identifier immediately; its result is later
retrieved or, in some families, delivered automatically. *See also* Task;
Tool meta-parameters.

### B

**Background task**
*See* Async task.

**Broker**
The central hub of the mesh through which agents exchange messages. When the
broker is unreachable, mesh tools are unavailable. *See also* A2A mesh.

### C

**Credential**
A named secret (for example, an API key). Credentials are referenced rather
than written out, and their values are never exposed in tool output.

**Credential resolution chain**
The ordered set of sources consulted to obtain a value for a referenced
credential: the process environment first, then the credential store, then a
literal default supplied with the reference.

**Credential store**
The persistent, secure repository in which secrets are kept (abbreviated
*credstore*). It is the canonical source for secrets; the environment is a
temporary mirror. *See also* Credential resolution chain.

**Context**
*See* LLM context.

**Context ceiling / Context floor**
The upper and lower bounds, expressed as percentages of the context window,
within which the system keeps the live context. On overflow above the
ceiling, the oldest turns are trimmed down toward the floor. *See also*
Trim.

**Context coverage**
The time range the currently loaded context spans, reported to the model so
it knows how far back its working set reaches.

**Context injection**
The introduction of information into the context on the system's own
initiative, rather than in response to the user. It takes three forms: a
marker-carrying user message injected as a turn (heartbeat, scheduled-task
trigger); a harness tool-pair contributed by an auto-invoked harness tool;
and marker-carrying info appended to an existing message (turn footnote,
trim note). *See also* Heartbeat; Scheduled task; Harness tool; Marker
(Part III); Channel (Part III); Annotation.

**Context status**
A per-turn report of the current operating conditions: the time, the context
usage (tokens and percentage of the window), and any recent changes to the
model, working directory, or shell. Peer presence changes may also be
included. *See also* LLM context; Presence.

### D

**Diary**
A per-day record written in the File Cabinet, keyed by date. A diary entry
is appended to the day's file and indexed for search. A diary is a cabinet
document, written by action (the `diary_write` tool); it is not the turn
history, which lives in the Turns DB. *See also* File Cabinet; Note; Turns
DB.

### E

**Environment**
The agent's operating conditions as reported to the model: platform type,
operating system, interpreter, package manager, and the locations of its data
root, configuration, logs, turns database, skills, and file cabinet.

**External server tool**
A tool exposed by an external MCP server and reached through the MCP gateway.
It is namespaced *server__tool* and, unlike a native or built-in tool, is not
exposed to the model wholesale: it is discovered in the tool catalog and made
callable by an explicit loading step. A tool whose server is disabled is
cataloged but not usable. *See also* Tool naming; Native tool; Tool catalog;
Tool search.

### F

**File Cabinet**
One of the two memory stores. The cabinet holds three kinds of private
content — notes, diaries, and saved files — mirrored as readable files and
indexed for search. Content is saved to the cabinet by explicit action and is
recalled by search. *See also* Memory; Turns DB.

**File category**
The classification under which a saved file is filed, determined by its type
and overridable explicitly: images, documents, archives, code, audio, video,
data, or other.

**File sharing**
The operation of publishing a local file as a public URL so that a
multimodal model can fetch it directly. Publishing is always an explicit
choice; nothing is published automatically. *See also* File Cabinet.

**Full-text search modes**
The search strategies available over the turns database: exact substring
(*grep*), keyword ranking (*fts5*), a hybrid of keyword and semantic search
(*hybrid*, the default), and browsing by date range (*time*). *See also*
Semantic index.

### H

**Harness tool**
A tool that is visible in the schema but reserved for the system: it is
invoked automatically to maintain the context, and the model must not call
it. Harness tools are identified by a leading underscore. *See also*
Internal tool.

**Health check**
A structured report on the state of a component — its component name, a
level (ok, warning, error, or informational), a key, a value, and a
human-readable hint with remediation. Health checks cover the turns
database, the file cabinet, messaging, sharing, the local embedding service,
the mesh, external servers, and the process watchdogs. *See also* Watchdog.

**Heartbeat**
A periodic autonomous trigger delivered to an idle agent. A heartbeat is a
synthetic stimulus, not a user query; it offers the agent an opportunity to
act on its own. When the agent has nothing worth surfacing, it replies with
silence. *See also* Heartbeat marker; Silence output.

**Heartbeat marker**
The annotation that marks a heartbeat stimulus. *See also* Heartbeat.

**Hybrid search**
*See* Full-text search modes; Semantic index.

### I

**In-flight turn**
The turn currently being processed — not yet complete and therefore not yet
persisted. Certain annotations (for example, a summary) may be applied to
the in-flight turn and take effect when it completes. *See also* Turn.

**Internal tool**
A tool that is not exposed to the model at all, used only by the system.
Internal tools are identified by a double underscore prefix. *See also*
Harness tool.

**ISO time bound**
A date-time used to constrain searches and listings, given in ISO format;
relative expressions (such as "yesterday") are also accepted. *See also*
Since / Until.

### L

**LLM context**
Everything submitted to the model on one call: the tool schemas, the system
prompt, the historical turns, and the current turn. The context is managed
dynamically to stay within a window; it is rebuilt from memory at startup and
may be trimmed or cleared without loss. *See also* Context ceiling / Context
floor; Memory; Restore; Trim.

### M

**Memory**
The persistent layer holding everything the agent has stored. Memory has two
stores — the Turns DB (the turn history) and the File Cabinet (notes,
diaries, and files) — and is never destroyed by context management.
*See also* LLM context; File Cabinet; Turns DB.

**Message**
The atomic unit exchanged with the model, carrying one of four roles —
system, user, assistant, or tool. A tool call is a message and its result is
a paired message. A turn consists of messages. *See also* Turn.

**Mesh**
*See* A2A mesh.

**Model ref**
The stable handle identifying a model, of the form *provider/model*. The
active model is named by its ref. *See also* Active model; Provider.

**Multimodal**
The capability of a model to accept non-text input, most commonly images.
*See also* Vision.

### N

**Native tool**
A tool that is part of the agent's own toolset, exposed under its bare
name. Built-in plugin tools are native in this sense. *See also* Tool
naming; External server tool.

**Note**
A private note keyed by subject, written in the File Cabinet and indexed for
search. *See also* File Cabinet; Diary.

### P

**Peer**
Another agent reachable on the mesh. *See also* A2A mesh; Agent card;
Presence.

**Platform type**
The kind of host the agent runs on — a native operating system, a WSL
environment, or a headless process. *See also* Environment.

**Presence**
The online/offline state of peers on the mesh, reported to the model as a
series of events (coming online, going offline, timing out). *See also*
A2A mesh; Peer.

**Provider**
A configuration of one model API endpoint: its identifier, protocol, base
URL, and authentication. Models are grouped under providers. *See also*
Active model; Model ref.

### R

**Report**
A document a scheduled task produces when it runs. A report is saved to the
file cabinet, belongs to its task, and records the period it covers; it can
be listed, read, and searched like any cabinet document. *See also*
Scheduled task; File Cabinet.

**Restore**
The reconstruction of the exit-time context at startup, so that the agent
resumes as if it had never been interrupted. Restore reads from memory and
does not affect it. *See also* LLM context; Memory.

**Result delivery mode**
How the result of an asynchronous task is returned: *auto* (delivered to the
agent automatically when complete) or *poll* (retrieved on request by its
task identifier). *See also* Async task; Task.

### S

**Scheduled task**
A recurring task the agent runs on a schedule: a named definition carrying a
description, a cron schedule, and a timezone. Its name is also the name of
the subagent worker that executes it, so it must be a safe identifier. When a
task fires, the agent is prompted (via a ``[Schedule <name>]`` message) to
call ``run_schedule_now``, which records a run and dispatches the work to
that worker; the agent does not perform the work inline. The worker saves the
result as a report and notifies the user. Scheduled tasks fire only while the
agent is running. *See also* Scheduled run; Report; Schedule marker;
Subagent.

**Scheduled run**
One occasion of a scheduled task firing, recorded optimistically as
``pending`` and confirmed only when the worker's report arrives. A run
records when it was due and its outcome — ``ran`` (a report landed),
``failed`` (no report: interrupted, errored, or a previous process lifetime
left it undone), ``missed`` (due while the agent was not running), or
``skipped`` (the user declined a backfill) — and, once the task finishes,
links to the report that was produced. A one-shot startup sweep settles
what a previous process lifetime left behind — unconfirmed runs swept to
``failed``, fires that were due while the agent was down marked ``missed``
— silently, with nothing announced; either can still be backfilled with
``run_schedule_now`` or closed with ``scheduled_run_skip`` at the user's
choice. *See also* Scheduled task; Report; Startup sweep.

**Schedule marker**
The annotation that marks a scheduled-task trigger: ``[Schedule <name>]``
when a task fires — prompting the agent to dispatch it via
``run_schedule_now``. *See also* Scheduled task; Scheduled run; Schedule
loop.

**Semantic index**
The auxiliary structure that enables meaning-based (vector) search over the
turns database, the file cabinet, and the MCP tool catalog, complementing
keyword search. It has its own readiness state and can be enabled, disabled,
or reconfigured independently; keyword search remains available while it is
unavailable. *See also* Full-text search modes; Tool catalog.

**Silence output**
The minimal reply (a single period) by which the agent expresses that it has
nothing worth surfacing. Silence is not rendered as content. *See also*
Heartbeat.

**Since / Until**
The lower and upper bounds of a time range over which turns, tokens, or
listings are filtered. *See also* ISO time bound.

**Skill**
An installable set of instructions and scripts, presented progressively: the
agent first sees a list of skills with brief descriptions, then loads a
chosen skill's full documentation on demand. Skills may be installed,
removed, enabled, and disabled.

**Source provenance**
The recorded origin of an external item (a command, an API, a skill, or a
server): its source type, URL, and version, so that it can be updated later.

**Subagent**
A local worker process spawned by the agent to carry out delegated work in
parallel. A subagent has the same capabilities as the agent that spawned it
but no independent identity: externally it acts as its parent, its own turns
are not saved, and all replies and management return to the parent. A
scheduled task's worker is named after the task itself, so the two are
identified by the same name. *See also* Task (worker); Scheduled task.

**Summary / Tags**
Optional annotations written for a turn — a short summary and keyword tags —
that make the turn findable by search. They may be attached to an existing
turn or captured for the turn currently in progress. *See also* Annotation;
Turn.

**System prompt**
The fixed part of the context that defines the agent's identity,
environment, capabilities, and coordination rules. *See also* LLM context.

### T

**Task**
Work delegated for execution outside the immediate flow, existing in three
families:
- *Async task* — a tool call run in the background by the agent itself.
- *Worker task* — work sent to a local subagent.
- *Mesh task* — work sent to a remote peer over the mesh.
Each family has its own way of being polled and cancelled, and its own
result-delivery mode. *See also* Async task; Result delivery mode; Subagent;
A2A mesh.

**Tool meta-parameters**
Universal optional parameters accepted by every tool, chosen by the model
per call: a timeout override, a request to run in the background, and a
request for an approval prompt. They are stripped before the tool runs.
*See also* Approval prompt; Async task.

**Tool naming**
The rule by which tools are named: the agent's own tools (native and built-in
plugin tools) carry bare, self-describing names; tools exposed by an external
server are namespaced with the server name and a double underscore
(*server__tool*) and are loaded on demand rather than exposed wholesale.
*See also* Native tool; External server tool; Tool search.

**Tool catalog**
The persistent index of external MCP tools, maintained by the MCP gateway and
queried by the model through *tool search*: each entry carries the server
name, the tool's bare name, its description, and whether it is enabled. The
catalog survives restarts, so a search works before any server reconnects.
*See also* External server tool; Tool search; Semantic index.

**Tool search**
The tool the model uses to find external MCP tools. It accepts a query and
returns matching *server__tool* entries from the tool catalog, in three modes:
*grep* (exact substring), *fts5* (keyword), and *hybrid* (semantic + keyword,
the default), which degrades to keyword-only when no embedding endpoint is
configured. *See also* Tool catalog; External server tool; Semantic index;
Full-text search modes.

**Trim**
The removal of the oldest complete turns from the live context, performed
when the context exceeds its ceiling, to bring it back toward its floor.
Trim affects only the context, never memory, and is announced by a trim
note. *See also* Context ceiling / Context floor; Trim note.

**Trim note**
The annotation that reports how many oldest complete turns were trimmed from
the context, written as `[INFO: N oldest turns have been removed from
context]`. It is a runtime notice and is not part of the persisted record.
*See also* Trim.

**Turn**
The unit of interaction: one user message together with the assistant's
complete response to it, including tool calls and results. Turns are the unit
the agent manages; every completed turn is persisted to memory. *See also*
Message; Turn footnote; In-flight turn.

**Turn footnote**
The annotation that identifies a turn by its turn id and the time it
occurred, written as `[INFO: {"turn_id": N, "begin": …, "end": …}]`. It
appears on restored turns and on a just-completed turn once saved, but never
on the turn currently in progress. *See also* Turn; Turn id.

**Turn id**
The identifier by which a turn is addressed. It appears in turn footnotes
and in the results of turn listings and searches, and is used to read,
summarize, or page through turns. *See also* Turn; Turn footnote.

**Turns DB**
One of the two memory stores. The database holds the complete turn history;
every turn persists there permanently and can be recalled into the context by
search. It is implemented by the built-in *memdb* plugin, whose backing table
is named `diary` in the codebase — a developer-facing alias, unrelated to the
File Cabinet's Diary. *See also* Memory; File Cabinet; Diary; Built-in plugin
(Part III).

### U

**Upsert**
A registration operation that adds an item if absent and updates it if
present, in a single call — used for commands, APIs, skills, models, and
servers.

### V

**Vision**
The capability of a model to process images. When the active model supports
vision, images may be attached to the current turn; otherwise attachment is
refused. *See also* Multimodal.

### W

**Watchdog**
A supervisor that monitors a background service process and restarts it
automatically if it exits unexpectedly, with bounded retries. *See also*
Health check.

---

## Appendix A — Annotation Notation

Four annotations may appear inside messages. They are machine-generated
metadata, not content:

| Notation | Meaning |
|---|---|
| `[INFO: {"turn_id": N, "begin": "…", "end": "…"}]` | The turn footnote: turn id `N` and when it happened. |
| `[INFO: N oldest turns have been removed from context]` | The trim note: the `N` oldest complete turns were trimmed from context. |
| `[Heartbeat]` | The heartbeat marker: a synthetic autonomous trigger. |
| `[Schedule <name>]` | The schedule marker: the scheduled task `<name>` is due. |

---

## Appendix B — Tool Families by Name

The tools visible to the model fall into families, recognisable by their
bare names or prefixes:

- **Turns & memory** — listing, reading, searching, summarizing turns; token
  usage; the semantic index.
- **File Cabinet** — notes, diary, saving and listing files; cabinet search.
- **Scheduled tasks** — defining, listing, and removing scheduled tasks; run
  history and confirmation; running a task now; saving and reading reports.
- **File sharing** — publishing a local file as a public URL.
- **Generation** — image, video, speech synthesis, and transcription.
- **Messaging** — login, send, receive, and status for the messaging channel.
- **Mesh** — agent discovery, sending and cancelling tasks, broadcasting.
- **Subagents** — spawning, listing, stopping workers; delegating and
  tracking tasks.
- **Execution** — shell commands, Python scripts, package installation.
- **Skills** — listing, loading, installing, removing, and toggling skills.
- **Models** — listing, adding, removing, and switching models; attaching
  images to a vision model.
- **Embeddings** — listing, probing, setting, switching, removing, and
  enabling the first-class embeddings config (`embeddings_model_*`).
- **Configuration & credentials** — environment variables, native-tool
  toggles, and the credential store.
- **External servers** — registering, listing, searching for, loading, and
  managing third-party servers and their tools (`mcp_*`, `mcp_tool_search`,
  `mcp_tool_load`).
- **Meta & system** — the tool inventory, background-task management,
  context reset, health reports.

External server tools are additionally namespaced `server__tool`, so their
origin is always visible in the name.

---

## Part III — Developer Terms

Terms used in the design document and the user documentation: the
architecture, the extension contract, and the notation that governs the
codebase. These are not model-facing; they describe how Slife is built and
how it is extended.

Two design disciplines organise how the system steers the agent on its own
initiative (neither waits for the user's input nor the model's decision):
**context engineering**, which sets and dynamically changes the content of
the context, and the **Harness** pattern, which autonomously invokes tools
to change the agent's behavior. The entries below define them and the
mechanics they use.

Within context engineering the annotation layer is organised along two
orthogonal axes: **channel**, the origin of a message as it enters the
inbox, and **marker**, the machine-generated notation written into the
context. The two are independent of one another: a channel names the
sender and is always present; a marker conveys information and is optional.

### A

**Agent Loop**
The runtime that drives a single interaction: it sends the context to the
model, executes the tools the model requests, and repeats until the response
is complete. The loop is the unit that binds messages, turns, and tools
together. *See also* Turn (Part II); Tool.

**Auto-discovery**
The mechanism by which tools and plugins are found at startup without a
registry entry: tool modules under the tools package and plugin packages
containing a server are collected automatically. *See also* Tool module.

**auto_load**
A per-server flag in the MCP gateway's configuration that restores wholesale
tool registration: when true, the server's tools are registered into the
agent's toolset whenever the server connects; when absent or false (the
default), the server's tools are loaded on demand. *See also* mcp-plugin; Tool
loading.

### B

**Built-in plugin**
One of the plugins shipped inside the slife wheel, discovered by scanning
`slife.plugins.*` and started like any other plugin: the turns database,
messaging, the file cabinet, file sharing, media generation, and the mesh.
The MCP gateway is no longer a built-in — it is the standalone `mcp-plugin`
package, registered via `plugins.external`. *See also* Plugin (Part II);
mcp-plugin; Plugin contract.

### C

**Channel**
The identity of the source to which a message entering the inbox is
attributed, taken with reference to the main agent that consumes it.
Every message belongs to exactly one channel, and the channel is
recoverable from the message alone; it is a property of the message's
origin, not of its transport, which imposes no constraint upon it. A
channel is orthogonal to a marker: the channel reports *who* addressed the
agent, whereas a marker reports information *about* the context. Channel
identity is persisted with the turn and governs the prefix under which the
turn is presented in the chat; by default it does not enter the LLM
context. The recognised channels are the operator at the keyboard
(*human*), a peer terminal (*WeChat*), a subagent worker (*subagent*), the
periodic heartbeat (*heartbeat*), a remote mesh peer (*A2A*), and Slife
itself (*system*), the last of which is suppressed from the chat view. A
scheduled task is not a channel: its trigger enters under the *system*
channel and its completion under the *subagent* channel, the schedule
being carried as a marker. *See also* Inbox; Marker; Annotation (Part II);
Context injection (Part II).

**Config sections**
The named sections of the configuration file that control subsystems:
environment variables (``env``), the agent loop (``agent``), tool overrides
(``tools``), the active model and model registry (``active_model`` +
``models``), media providers (``media``), plugins (``plugins.external`` /
``plugins.required``), the A2A mesh (``a2a``), subagents (``subagent``),
CLI-tool registry (``cli_tools``), and the first-class embeddings config
(``embeddings``). External MCP servers and the gateway's own embeddings config
live in ``mcp-plugin.json5``, not in ``slife.json5``. *See also* Configuration
(Part II).

**Context engineering**
The design discipline of *setting and dynamically changing the content of
the LLM context* — what the model reads on every call. The static half is
the baseline always given to the model: the system prompt (identity and
world spec) and the tool schemas. The dynamic half is the layer that evolves
that content across turns without waiting for user input: the turn footnote
and the trim note, the heartbeat and schedule triggers, context trimming,
and the tool-result cap. Context engineering works directly on the content
the model *sees*; it is the content-side counterpart of the Harness pattern,
which steers the agent through tool invocation instead. *See also* Context
injection (Part II); Harness (Part III); System prompt (Part II); Turn
footnote (Part II); Trim (Part II).

**Cryptfile backup**
The encrypted fallback store maintained by the credential store, used on
platforms without a system keyring. Slife's own credential resolution does
not read it; it exists for the credential store's CLI. *See also* Credential
store (Part II).

### D

**Daemon thread**
A background thread that cannot block shutdown; used for calls that must
never hang the process (for example, a desktop notification or a tunnel
start). *See also* Shutdown.

### E

**Embeddings section**
The first-class top-level ``embeddings`` configuration of ``slife.json5``,
shared by the memory plugins (memdb, memfiles): a set of OpenAI-compatible
endpoints (``base_url`` + ``api_key``) and an ``active_model`` ref that is
configuration-authoritative over the endpoint's own active-model flag. The
local-embed plugin serves a local model behind it. The MCP gateway keeps its
*own* ``embeddings`` section in ``mcp-plugin.json5`` — a single flat config
(base_url + optional model/api_key), edited by hand and refreshed with
``mcp-plugin build``. *See also* local-embed; Semantic index (Part II);
mcp-plugin.

**External plugin**
A plugin package that is not part of the Slife source tree: registered via
``plugins.external`` in ``slife.json5`` and spawned through the same generic
lifecycle as the built-ins. The MCP gateway (mcp-plugin) and local-embed are
external plugins. *See also* Plugin contract; Built-in plugin; mcp-plugin;
local-embed.

### H

**Harness**
The design discipline of *steering the agent by autonomously invoking tools
on its behalf*: the agent loop performs a tool call without waiting for the
user's input or the model's decision, and the agent's behavior changes as a
side effect of a tool execution it appears to have made. The instrument is
the harness tool — a schema-visible, underscore-prefixed tool the model is
forbidden to call, whose call/result pair the loop auto-invokes each turn
(the context-status tool). Harness is the tool-invocation-side counterpart
of context engineering, which works directly on context content instead.
*See also* Harness tool (Part II, Part III); Context engineering; Agent
Loop.

**Harness tool**
The developer sense: a tool that is schema-visible but auto-invoked by the
agent loop on the agent's behalf, reserved against direct calls. In Slife
the single harness tool is the context-status tool, identified by a leading
underscore. The model-facing sense (Part II) is the same. *See also*
Harness; Internal tool; Agent Loop.

### I

**Inbox**
The unified queue through which every input — human keyboard input,
messaging messages, mesh tasks, and subagent results — flows, to be processed
one turn at a time. *See also* Turn (Part II).

**Internal tool**
A plugin tool that serves the main process rather than the model. Internal
tools are identified by a double-underscore prefix and are filtered out of
the schema before registration, so the model never sees them. *See also*
Harness tool; Plugin contract.

### L

**Language policy**
The rule that all model-visible text authored by Slife — the system prompt,
tool names, descriptions, parameter documentation, and result strings — is
written in English, while content from external sources (commands, APIs,
skills, servers) keeps its own language. Scheduled-task messages (the
``[Schedule <name>]`` trigger and the completion notification) are
operator-facing and written in the operator's own language. *See also*
Model-visible (Part II).

**local-embed**
The external plugin that serves an OpenAI-compatible embedding endpoint —
``POST /v1/embeddings``, ``GET /v1/models``, and ``GET /v1/models/{id}`` — from a local GGUF
(llama-cpp) or HF transformer model, loaded once and shared by memdb, memfiles,
and the MCP gateway's tool catalog. Registered via ``plugins.external``; binds
the configured port (default 8000) and reads its model configuration from
``local_embed.json5``. *See also* Embeddings section; mcp-plugin.

### M

**Marker**
A brief, machine-generated notation written into the context on the
system's initiative, distinguishable from the words of the user or the
assistant. Markers constitute the annotation layer of context
engineering: the turn footnote, the trim note, the heartbeat marker, and
the schedule marker are all markers. A marker is orthogonal to a channel:
the channel names the source of a message, the marker conveys information
about the context. Whether a marker persists is independent of its
function — the heartbeat and schedule markers ride the stored message text
as turn identity, whereas the ``[INFO: …]`` footnote and trim note are
runtime constructions, reproduced at restore and never stored. Markers are
the unit in which context injection operates. *See also* Channel; Context
injection (Part II); Annotation (Part II); Turn footnote (Part II); Trim
note (Part II).

**mcp-plugin**
The standalone PyPI package that implements the MCP gateway — the plugin that
connects Slife to external MCP servers (stdio / SSE / Streamable HTTP). It
lives in the `mcp-plugin/` workspace member (module `mcp_plugin.server`), is
registered via `plugins.external` in `slife.json5`, and exposes the `mcp_set` /
`mcp_list` / … management tools, the tool-catalog tool (`mcp_tool_search`,
…), and its own `mcp-plugin` CLI
(`set`, `remove`, `build`). It keeps a persistent **tool catalog**
(`mcp-plugin.db`) of every loaded external tool — name, description, and
enabled state — indexed for keyword and semantic search. External tools are
**loaded on demand**: the host registers none of them by default; the model
discovers one with `mcp_tool_search` and loads it with `mcp_tool_load`.
Enable/disable is server-granular (`mcp_set_enabled`), and `mcp-plugin build`
rebuilds the catalog and its index from live connections, marking a disabled
server's tools disabled. It self-hosts an ``embeddings`` section in
``mcp-plugin.json5`` (edited by hand; refreshed by ``mcp-plugin build``) that
feeds the catalog's semantic index. *See also*
Plugin (Part II); Built-in plugin; Plugin contract; Tool catalog; Tool
loading; local-embed.

**Meta-parameter**
The developer sense of tool meta-parameters (Part II): the universal
parameters injected into every tool schema, stripped before dispatch. In the
codebase these are the timeout, background, and approval parameters. *See
also* Tool meta-parameters (Part II); Approval gate.

**Model-visible**
Anything that reaches the model's input: the system prompt, tool schemas,
tool result strings, and message annotations. The language policy governs
this text. *See also* Language policy.

### P

**Plugin contract**
The convention a plugin package must follow: a server module with an entry
point, discovered and spawned as a child process over the Streamable HTTP
transport, exposing tools through the model-context protocol. Tools whose
names begin with a double underscore are treated as internal. A plugin is
ready when its MCP ``initialize`` handshake completes — its lifespan has
established its own serving capacity, so no ``__ready`` probe tool exists.
The lifespan must therefore be **handshake-fast**: it establishes only the
minimum needed to serve; heavyweight startup (e.g. a GIL-holding
embedding-model load) is deferred until after the first ``tools/list`` by
the ``warm_after_handshake`` helper, never run inside the lifespan.
*See also* Built-in plugin; Internal tool; Readiness; Required plugin
(Part III).

**Process watchdog**
The supervisor that monitors a plugin's child process and restarts it on
unexpected exit, with exponential backoff up to a bounded restart count.
*See also* Health check (Part II).

### R

**Registry**
The runtime collection of registered tools, supporting lookup, registration,
and conversion to the function definitions sent to the model. *See also*
Tool (Part II); Auto-discovery.

**Readiness**
The state of a plugin under the MCP initialize handshake: a plugin is ready
when the harness's ``initialize`` handshake completes — the server only
answers it after its own FastMCP lifespan established its serving capacity,
so the completed handshake is the ready declaration. The per-plugin
requirement is encoded in the lifespan and covers ONLY the plugin's own
serving capacity — its local store/process (memdb/memfiles require their
store; the other plugins have no local requirement). Subordinate or external
dependencies (mcp's external servers, sharefile's tunnel, wechat's login,
media's providers, a2a's broker, a store's embedding backend) are NOT
readiness conditions: they are uncontrollable, self-heal at runtime, and
are surfaced separately via status tools. A lifespan that fails its
requirement traps it — the plugin is reported FAILED and its watchdog
retries. Because the lifecycle must stay **handshake-fast**, anything that
could stall or freeze the loop while the wrapper connects (a model load, slow
I/O, long connects) is deferred to the post-handshake ``warm_after_handshake``
helper — never the lifespan. Startup holds the service open until every
plugin spawn has converged (Startup convergence). Plugins named in
``plugins.required`` are core: failing to become ready aborts startup
(Required plugin). *See also* Plugin contract (Part II); Required plugin;
Startup convergence.

**Required plugin**
A core component declared in the ``plugins.required`` list of ``slife.json5``
(default: empty — every plugin optional). A required plugin that fails to
become ready — a FAILED spawn, a raised spawn, or the 30 s spawn hang-guard —
**aborts startup**: red message, all plugins stopped, non-zero exit; never a
silent session missing a core component. ``memdb`` and ``memfiles`` are
required in the standard configuration because memory is core. *See also*
Plugin contract; Readiness; Startup convergence.

### S

**Schedule loop**
The main-process background loop that times scheduled tasks. Each poll it
recomputes every enabled task's next fire from the database and injects the
trigger for fires due within the grace window — the poll cadence runs well
inside the window, so while the process is up no fire can slip past
unfired. It fires only: it never sweeps or announces unfinished runs, which
is the startup sweep's job. It never executes a task — the agent
delegates execution to a subagent by calling ``run_schedule_now``. The loop
also reaps idle schedule workers: a worker whose task has no run still in
flight is stopped, to be respawned at the next fire. *See also* Scheduled
task; Scheduled run; Startup sweep.

**Schema authoring**
The convention for writing tool schemas: a tool's description states what it
does; each parameter's description states how to use it — the accepted
format, where the value comes from, what it means, and its default. *See
also* Tool (Part II).

**Secret sanitization**
The masking of secret-shaped strings (API keys, bearer tokens, and similar
patterns) in user input, tool arguments, and tool results before they reach
logs or the model. *See also* Credential (Part II).

**Shutdown**
Orderly process termination, ensuring background tasks and child processes
are stopped and no thread can block exit. *See also* Daemon thread.

**Startup convergence**
The point at which every attempted plugin spawn has reached a terminal
outcome — ready (the MCP initialize handshake completed), skipped, or
failed. Readiness is binary, so there is no start-time "degraded"; runtime
degradation of dependencies lives in the status surfaces. The service opens
for user input only after convergence: the inbox consumer and the TUI
input both wait on the same event, so user input can never race ahead of
core services (this is what visibly fixes the startup-timing problems of
the processing indicator). Event-driven — set by the last spawn's
completion, never polled and never time-bounded; the only timeout is a
hang-guard on a stuck spawn so convergence still fires.
Plugins named in ``plugins.required`` are core and not optional (Required
plugin); for every other plugin a breakdown (e.g. a broken memory backend)
fails loudly where it is used — the inbox freezes with a red banner on the
first turn that cannot be saved. *See also* Readiness; Required plugin;
Startup sweep.

**Startup sweep**
The one-shot startup pass that settles scheduled runs a previous process
lifetime could not finish: runs still `pending` are swept to `failed` —
the process that dispatched
them is gone, so they can never complete — and fires that were due while
the agent was down are marked `missed`. It runs exactly once, outside the
schedule loop, awaiting Startup convergence (event-driven — no polling),
and posts no message; the settled runs just show up in `scheduled_run_list`,
where one can be backfilled with `run_schedule_now` or closed with
`scheduled_run_skip`. *See also* Schedule loop; Scheduled run; Startup
convergence.

**Streamable HTTP**
The transport over which plugins communicate with the main process: a
long-lived HTTP connection carrying model-context-protocol messages. *See
also* Plugin contract.

### T

**Tool module**
A file under the tools package that defines one or more tool classes; tool
modules are collected by auto-discovery. *See also* Auto-discovery; Tool
(Part II).

**Tool catalog**
The MCP gateway's SQLite store (`mcp-plugin.db`) holding one row per loaded
external MCP tool — *server__tool*, name, description, and a per-tool enabled
flag derived from the server's state. Backed by an FTS5 keyword index and a
BLOB-vector semantic index produced against the configured embeddings
endpoint. *See also* mcp-plugin; auto_load; Semantic index (Part II).

**Tool loading (on-demand)**
The mechanism by which external MCP tools reach the agent's toolset: rather
than registering every tool of every connected server, the host registers none
by default, and the model discovers a tool with ``mcp_tool_search`` and loads a
chosen one with ``mcp_tool_load``. A server with ``auto_load`` set keeps the
older wholesale-registration behavior. *See also* mcp-plugin; auto_load;
External server tool (Part II).

**Trim (developer sense)**
The internal operation, performed after a turn is saved, that removes the
oldest complete turns from the live context when it exceeds the ceiling,
marking the cut with a runtime trim note. *See also* Trim (Part II); Context
ceiling / Context floor (Part II).

---

*End of glossary. Part II defines the terms the model encounters; Part III
defines the terms a developer or operator encounters. Where the two layers
share a word, the cross-references keep the reader oriented.*
