# Slife — Invariants

The rules that must not be broken, and what each prevents. They are collected here, apart from
[DESIGN.md](DESIGN.md), because they have a different half-life: the design changes when the
architecture does, an invariant changes when an incident does. Each was learned the hard way and each
is silently violated by a plausible-looking change — so each is stated as an **assertion**, not as a
fact about the current code. If the code does not satisfy one, the code is wrong.

The numbers are stable and may be cited. DESIGN.md §9.1 and §3.3 point here for the canonical
statement of the interaction rules they summarize.

**Memory and context**

1. **Usage is measured or it is zero.** The context-size accessor never returns an estimate — a guess
   presented as occupancy is worse than an honest zero. Estimates appear in exactly one place: sizing
   what a recall may add to a context that has not been rebuilt yet.
2. **Every save path is a hard stop, not a skip.** A turn that cannot be persisted is not worth
   running. The flip side is deliberate too: subordinate dependencies never gate readiness, because
   they are uncontrollable and self-healing.
3. **Nothing a model says can empty the context by accident.** A decision keeps what it names and
   *adds* what it recalls, so a recall that answers nothing adds nothing; clearing is the explicit
   clear. A *failed* decision keeps the context too — defaulting it to a recency list would change
   the context on the strength of no decision at all.
4. **A store or tokenizer failure is fatal; no ids is the answer for everything else.** No ids is
   safe by construction (see 3), so fatalness is not what protects the context — it is kept because a
   broken database and a missing vocabulary are real environment failures, and a plausible-looking
   empty list hides them behind a turn that ran without the history it asked for.
5. **One turn→messages builder, shared by restore and recall**, and the rebuilt list is chronological
   even though membership is by relevance. A rebuilt turn must render byte-identically to the same
   turn restored, or every rebuild costs a prompt-cache miss — which is also why a decision that asks
   for exactly what is in hand rebuilds nothing at all.
6. **The rebuild happens before the user message is added**, because it replaces the message list
   wholesale. A decision that changes nothing never gets there: the context stands as it is.

**Caching and the wire**

7. **Identity and world change only on a model switch or a user-preference write, and always from the
   role's own template; the per-turn status is a message-stream tool pair, never a second system
   message.** Both rules exist so the static prefix stays byte-identical between those events and the
   prompt-cache breakpoint lands on it — and so that a worker never renders the main agent's identity.
8. **A harness tool must be schema-declared**, because the Messages and Responses backends reject a
   tool call in history whose name is not in the declared tool list.
9. **The tool list is computed once per request, outside the retry loop**, so every attempt sends
   byte-identical tools; and a mid-turn load **appends**, leaving the request's prefix untouched.
10. **Never emit an async notification from inside a request handler's cancel scope.** Interleaving a
    burst into that scope desyncs the SDK's cancel-scope stack and every later call dies.

**The tool system**

11. **Load state governs what a turn injects, never what a call may do.** The only refused call is one
    with no execution instance behind it, and a refusal names the state and nothing more — a refusal
    that guesses at a remedy tells the caller to do what it has already done.
12. **Load state has exactly four writers**: the autoload override, the load tool, the unload tool,
    and eviction. No connectivity verdict is ever written into it.
13. **Removal is a row delete, never a status mark**, and the row's embedding chunks go with it
    explicitly rather than through a cascade that may be off.
14. **Off is not down.** Disabled and error are different facts: the config arm may write disabled
    over either, but the runtime arm may never resurrect a tool the config switched off.
15. **The injected schema is the catalog's stored schema column** — the stored definition and the wire
    definition are one and the same.
16. **A schema is enforced, not advisory**: closed by default at class definition, validated at the
    single dispatch point. Exceptions are stated, not assumed — a schema declaring its own openness
    keeps it, and a remote server's schema is never **closed**. The adapter's one edit to a remote
    schema is the opposite direction and keeps every other key: a non-object input schema becomes
    `type: object`, so a definition reference cannot dangle.
17. **The agent's timeout overrides all defaults; a tool's own timeout is a generous backstop.** A
    native tool with an internal run-timeout must expose it as a parameter, or a hidden inner timer
    silently clamps the injection. Zero or negative never means "no timeout".
18. **Background calls escape the tool budget** — with no injected timeout, a background call is
    scheduled bare, because the chain default must not govern work that exists to escape it.
19. **Timeout values are read at call time**, never import-captured, so they stay patchable — and a
    hardcoded timeout fails CI, which is what kills the fix-one-drift-another loop.
20. **The run's outcome and the harness's bookkeeping are separate facts**, so the last-used update
    sits outside the execution's error handling. A tool that has already run — a config written, a
    message sent, a file deleted — must never be reported as failed because a shared-database write
    failed, since the model's answer to that is to retry a non-idempotent action.

**Plugins and processes**

21. **The spec table is the only place a plugin is declared.** Adding a plugin is one row plus a
    server package; nothing else may hard-code a plugin's name.
22. **Readiness is the completed protocol negotiation.** There is no readiness probe, and a dependency
    not required to serve never gates readiness. Never signal the port early: the signal means "ready
    to serve MCP on this port".
23. **A capability must report "not yet" as "not yet", never as "no".** An initialization in flight
    must be awaited by every caller, not just the one that started it — a boolean that answers "no"
    while still loading is indistinguishable from a genuinely unavailable one.
24. **A hard-killed parent runs no cleanup**, so the kill-on-close job object is assigned at spawn,
    before the child can spawn anything of its own. On POSIX the process tree is read before anything
    is signalled, and a group kill is only safe when the child leads its own group.

**Subagents**

25. **A worker is the same loop with a declared, zeroed capability set.** A new capability is
    worker-denied by default and must be granted on purpose; a role branch outside the table fails CI.
26. **The harness pushes results; the worker never does**, and a late result is stored, never
    auto-pushed, because the caller was already told it timed out. The exception is the task nobody
    awaits: an async task's failure *is* pushed, because silence is otherwise indistinguishable from
    work in progress.
27. **A stuck task must be preempted in the child**, because a worker processes tasks serially and one
    stuck task would block every later one. A caller's cancel does it too — the same situation as a
    timeout.
28. **Config is handed over by file, never by environment** — the resolved config carries plaintext
    keys and the process environment is readable through the process table.
29. **The config's round trip is a fixed point**, derived from the field list rather than written by
    hand — the hand-written version silently dropped nine fields, which is how a worker came to report
    embeddings disabled while its parent reported enabled.

**Process and platform**

30. **Unbounded blocking calls run on daemon threads, never the default executor.** Both shutdown
    paths join every executor worker, so a blocked worker hangs the whole interpreter.
31. **The stderr relay must never die**, and a discarded over-long line must be consumed through its
    newline.
32. **Blocking regexes must bound their repeats.** An unbounded repeat once froze the parent's event
    loop for minutes on a single relayed line.
33. **No global socket defaults**, which would silently change every third-party socket.

**The TUI**

34. **The app's cancel binding is not priority; the approval prompt's and picker's bindings are.**
    Textual's priority pass resolves the app before the focused widget, so a priority cancel on the
    app would steal the key from the approval prompt and cancel the loop instead of denying, leaving
    the prompt unresolved. The reverse is equally true: non-priority bindings on a prompt would type
    the answer into the input bar instead.
35. **The approval prompt to deny or refocus is the *pending* one** — matched by type and undecided
    state, never by its class alone, because the model picker wears that same class and a decided
    prompt stays in the transcript as its status line. Denying the first match left the real prompt
    mounted and unfocusable behind the picker, with only the turn-cancelling key able to resolve it.
    The mirror direction is the same rule: a dismissed picker must not take focus back from a prompt
    that is already mounted.
36. **A binding action must be sync.** Binding actions run inside the key-event handler, so awaiting
    there blocks the message pump and deadlocks the widget that needs the next key event.
37. **A dismissed widget must resolve its future**, or a re-entrancy flag stays stuck and the shortcut
    is dead. The status-bar scroll happens **after layout**, or it pins the view above the fold.
38. **All user data renders with markup disabled**, and **tool widgets are cleared only at the genuine
    turn-end event** — never where the turn is merely *enqueued*, which wiped an in-flight turn's
    widgets and left its rows stuck.

**Configuration**

39. **A config parse failure raises; it never returns an empty dict**, or a mutating caller writes
    that empty dict over the whole config.
40. **Config writes edit the document and are verified before use.** Losing comments is bad; writing a
    config that says something else is worse.
41. **Credstore is consulted before a `${VAR:-default}` literal**, or the default wins over a key that
    is actually held.
