# Recall: one axis, one priority, one report

Design write-up for the revision to `DESIGN.md` §2.3.

**Landed**: the axis rule and the `anchor` key — `DESIGN.md` §2.3 is authoritative for those, and
the prose proposed below is superseded by what is written there. **Still open**: the truncation
report, the `merge_hybrid` tie-break, the vec0 time column — kept here because the reasoning for
*not* building them yet is the part worth not re-deriving.

## The problem

Two defects in the per-turn recall's selection, one of which the design currently *asserts* wrongly:

1. **The time branch is hard-wired to the newest end.** `server.__memory_turn_recall`'s empty-query
   branch takes `search_time(limit=policy.limit)` → `turn_list` → `ORDER BY rowid DESC LIMIT 40`.
   So "recall the earliest turns" is **inexpressible**: the natural encoding —
   `{"since": "<memory start>"}` — returns the newest turns of the whole history, i.e. the opposite
   end, silently. The only encoding that works is a narrow window just past the origin, which is a
   trick, not an interface.
2. **Every cut below the caps is invisible.** `total ≥ considered ≥ selected` is only a chain for
   time conditions. For a query it is `retrieval-truncated ≥ gated ≥ budgeted`, where the first term
   is invisible: both legs truncate at `limit × overfetch = 120` before fusion, so nothing downstream
   can be exact — neither a count nor an anchor over the true matched set.

Neither is fixable by patching the *report*, because the two branches do not truncate at the same
layer. Consistency has to be established at **"what is the condition's candidate set, and what is
its order"** — which is what the rule below does.

## The rule

> **A condition provides an axis, and the axis decides the priority.**
> With no query the axis is **time** — the candidates are the window's turns, in time order, and
> `anchor` names the end the caps spend from. With a query the axis is **relevance** — the
> candidates are the fused RRF order, and time enters only as a *bound* on the candidate set, never
> as a priority.
> Caps spend from the axis's head. **Render order is chronological either way.**

Decisions this settles (all taken in review, 2026-09-26):

- **Relevance wins over time.** An `anchor` sent beside a query is never consulted. "When did we
  first talk about X" is therefore a *read* (`turn_search` / `turn_read`), not a context selection.
  Precedence reads as "the loser is dropped", so: dropped **and logged**, not a refused reply. (If
  refusal is preferred — the `_parse_recall_args` precedent for a decision-level contradiction — it
  is a one-line change of stance; recorded here as the open variant.)
- **`anchor` is scoped to time conditions**, where time is the axis and the end is genuinely
  ambiguous. The query branch's budget rule is unchanged: `fit_budget` skips an unaffordable turn and
  keeps scanning by relevance. The time-contiguous rule never applies where a query is present.
- **No pagination in the recall surface.** A recall answer is a one-shot prefix/sample; `search_time`
  drops `turn_list`'s envelope and paging. Continuation is out-of-band, through the model's own
  `turn_list` — which does take `offset` and does return `total`.

## Where `anchor` lives — recall is **not** an LLM tool

The distinction the whole surface hangs on, so it is stated once here:

```
discriminator's reply (a model call, not a tool call)   {context, recall}
  → RECALL_REPLY         the reply surface, rendered into rebuild_messages.j2
  → _parse_recall_args   the allow-list gate (_RECALL_KEYS)
  → AgentLoop.recall_turns(...)               the harness
  → __memory_turn_recall the internal MCP tool — the LLM never sees it
```

The model never calls anything: it fills a JSON object in a separate prompt, the harness gates it and
calls the internal tool itself (`__memory_turn_recall` is an `is_internal_tool` — the `__` prefix
keeps it out of the model's registry).

Two things follow, and both constrain this change:

- **There is no schema to quote**, so `anchor`'s meaning can be stated in exactly two places —
  `RECALL_REPLY`'s field text and the instruction template (`rebuild_messages.j2`). §2.3 already
  makes this point for the selector as a whole ("it cannot be read off a tool … no LLM-facing schema
  to quote"), and the tool-schema writing standard does not govern this surface.
- **The report cannot be a tool result.** `selected` / `matched` reach the model only as a note in
  the rebuilt context — which is what makes "note or log only" a real decision rather than a
  formatting one.

Also note the gate: an `anchor` absent from `_RECALL_KEYS` is **dropped silently** (`_parse_recall_args`
drops unknown keys *inside* `recall`, by design — there they are parameters), so the allow-list entry
is part of the change, not a formality.

## The new key: `anchor`

On the `recall` object, beside `query` / `since` / `until`. Values `"newest"` (default) | `"oldest"`.
Named for the **selection anchor**, never for the output order — the output is always chronological,
and a key called `order` would be a lie about the one thing the decision can see.

- `newest`: the newest turn in the window is taken **whatever it costs**, then a contiguous run
  backwards until the budget ends.
- `oldest`: the mirror — the oldest taken whatever it costs, then contiguous forwards.

The anchored turn is exempt from the budget (one turn's overshoot is what the ceiling absorbs, and
the trim removes oldest-first), and the run stops at the first turn that does not fit rather than
skipping it, because a time window is adjacency — a hole is a piece of the conversation missing with
nothing in the result to say so.

**`anchor` alone is a criterion.** `{"recall": {"anchor": "oldest"}}` with no bounds means the oldest
turns of the whole history — the cleanest encoding of "look at our earliest records", and the one the
narrow-window trick existed to avoid. This amends the "empty call recalls nothing" rule deliberately:
an anchor names an end of the whole history, so it *is* a condition.

## The caps

| cap | rule |
|---|---|
| count | `recall_limit` (40), applied in SQL from the anchored end — `ORDER BY rowid ASC\|DESC LIMIT 40` |
| token | the headroom below the ceiling (`min(floor, ceiling − reserved)`), spent contiguously from the anchored end |

`turn_list` already builds the window, its axis and the ordering in one place, so the axis stays
single-sourced; the time branch only flips the direction.

## The report

`selected`, plus `matched` **where it is knowable** — one shape for every condition, and no invented
number anywhere:

| condition | `matched` | continuation |
|---|---|---|
| range / anchor alone | exact — `COUNT(*)` over the window, already computed in `turn_list`'s pass and currently dropped | `offset = selected` (newest) / `matched − selected` (oldest), in `turn_list`'s DESC order |
| query | `null` — both legs truncate before fusion, so any number would be fabricated | none: rank order is not addressable; the remedy is a narrower query |

Reasoning for the query's `null`: `annotate_scores` refuses to invent a similarity for a keyword-only
hit ("inventing a number would be a lie about the match"); a fabricated candidate total is the same
lie one level up.

**Open decision**: does the report reach the **model** (a runtime-only note, trim-note style —
attached at rebuild, discarded on restore, so the byte-identical rebuild/restore contract survives)
or only the **log**? The time branch is where it is most useful, since it is the one with a resumable
offset: the note turns "you were cut" into "you were cut, and here is where the rest starts".

## The three asymmetries

Stated in the doc rather than engineered away, because each is a real property of the storage layer:

1. **A query reaches the old end only by a guessed window.** A time condition's `anchor` names an end
   of the whole history with no date knowledge; a query has no such key (relevance wins), so reaching
   back means naming a window bounded past `memory_start_time` and hoping the material is inside it.
   The anchor removed that guess for one branch and left it for the other.
2. **And that reach rests on the weakest mechanism in the system.** `search_semantic` cannot constrain
   time inside the KNN — vec0 forbids auxiliary-column constraints, including a `JOIN ON`
   (`store.py:1131-1134`) — so a windowed query fetches a **global** pool (`limit × 8`) and filters
   the window in Python. When the window's turns are not in that pool, the semantic half comes back
   empty and reads as "nothing matched in that period". The pool boundary is **observable**: ask for
   `k + 1` and a full return means it was capped. So the honest treatment is to report the leg
   degraded (the `semantic_available` / `recall_degraded` concept already exists) rather than absent —
   cheap, exact, no schema change.
3. **"The earliest match" is answerable for one leg only.** The full-text index can answer it exactly
   and cheaply — `MATCH` ordered by `rowid ASC` — while the semantic leg cannot at all without a time
   partition/metadata column in vec0. So "the earliest turn about X" gets an exact answer when the
   keyword leg matches and nothing when only the meaning does.

## Proposed §2.3 text (drop-in)

Replaces the paragraph beginning "Recall's own three shapes…" and the later order paragraph; the
three caps paragraph stays.

> **A condition provides an axis, and the axis decides the priority.** A time-only range is the
> window's turns in time order; a query is the fused relevance order over the whole diary; a query
> plus a range is the same search bounded. Each shape carries the order its selection is made in, and
> nothing downstream overrides it: with no query time is the axis — the newest turns come first and
> `anchor` names the end the caps spend from — and with a query relevance is the axis, so time enters
> only as a *bound* on the candidate set. An `anchor` sent beside a query is not consulted: relevance
> wins over time, and the reach-back it describes is a read (`turn_search` / `turn_read`), not a
> context selection. Render order is chronological either way, because that is the restore contract
> and not a choice. An empty-query branch must run **before** the hybrid legs — they cannot express
> "no query": an empty query reaches the full-text index as a syntax error and embeds to noise.
>
> **A time-only recall reaches the end you name.** `anchor` is `newest` (the default, today's
> behaviour) or `oldest`, and it names the end the caps spend from — not the order of the answer,
> which is chronological in both. The anchored turn is taken whatever it costs: a turn larger than the
> whole budget is recalled *alone*, which is the answer and not a failure, and one turn's overshoot is
> what the ceiling absorbs — the same bound the trim enforces anyway. The run behind it is
> **contiguous** and stops at the first turn that does not fit rather than skipping it, because a time
> window is adjacency: skipping reaches past what it cannot afford and leaves a hole, and under time
> order the turn it drops first is the newest — the one turn the answer is most often about. Nothing
> older than the anchor's end is ever reached, so the budget can only ever take fewer than the count
> cap allows. An anchor with no range is a condition in its own right: `{"anchor": "oldest"}` is the
> oldest turns of the whole history, which is how "look at our earliest records" is asked without
> naming a window.
>
> **The report says what the caps did.** The selection carries what it took, and how much the
> condition matched **where that is knowable**: a time condition counts its window in the same pass it
> reads it, so its answer is exact and resumable — `offset = selected` from the newest end,
> `offset = matched - selected` from the oldest, both in `turn_list`'s newest-first order. A query's
> answer carries no total, because both legs truncate before fusion and a number there would be
> invented; its remedy is a narrower query, not a page. The same rule that refuses to invent a
> keyword hit's similarity refuses to invent a candidate count.
>
> **The two expressions of "the newest / the oldest" are not equivalent, and the difference is
> written down rather than engineered away.** Under time order the end you name *is* the priority:
> `{"anchor": "oldest"}` reaches the beginning with no date knowledge at all. Under relevance the end
> you name is only a *bound* — a query's caps spend from the relevance head, so
> `{"query": …, "until": early}` returns the **most relevant** turns inside that early window, not its
> oldest. Reaching back through a query therefore means guessing a window, and that reach is the one
> operation the retrieval layer is weakest at: the semantic leg cannot see time inside the KNN, so a
> windowed query filters a pool of the globally nearest turns and comes back empty when the window's
> turns are not in it — reported as a degraded leg, since the pool's own boundary is observable. The
> full-text index can answer "the earliest turn about X" exactly and cheaply (`MATCH` ordered by
> `rowid`); the semantic leg cannot answer it at all without a time column in the vector index.

## Not in scope

- **Tie-break in `merge_hybrid`** — independent of the above, and worth its own item. Exact ties are
  not rare: `1/(k+rank)` is *bit-identical* for a keyword hit at rank `r` and a semantic hit at rank
  `r`, and the stable sort then breaks them by dict insertion order, i.e. **the keyword leg wins every
  cross-leg tie**, which is an undeclared rule with nothing to do with relevance or time. Proposed:
  break by the **measured similarity RRF discarded**, then by **recency**. Not by continuous decay:
  that is the cross-axis override this design bans, and it collides with the measured
  `min_similarity` floor (decay moves hits across the gate → re-calibration). Recency is already
  expressible as a condition, so a recency prior inside relevance duplicates a facility that exists.
- **vec0 time partition/metadata column** — the only path to *exact* windowed KNN, and so to a
  `query + range` as exact as the time branch. Storage-layer change; deferred behind the cheap
  "visible" treatment above, which removes the silent wrong answer without it.

## Work order

1. **Time branch + `anchor`** — self-contained, no index or query-branch dependency. In the order the
   path runs: `RECALL_REPLY` + `rebuild_messages.j2` (the decision surface — the only place the key's
   meaning can be taught), `_RECALL_KEYS` in `loop.py` (the gate), the mirrored rule in `recall.py`,
   the direction in `turn_list`/`search_time`, the argument on the internal
   `__memory_turn_recall`, the `matched`/`selected` report, tests, and the §2.3 revision.
2. **Query branch window/pool reporting** — `k + 1` on the semantic pool, a capped pool reported as a
   degraded leg.
3. **Open**: report to the model or log only (above).
4. **Optional, later**: the vec0 time column, if exactness is wanted rather than honesty.

## Verification

- `uv run pytest` green; new units for the mirrored rule (the two branches disagreeing on the same
  input is the test that pins which rule a branch is wired to).
- Manual: `{"anchor": "oldest"}` with no bounds returns the earliest turns; `{"since": <origin>}`
  alone still means what it means today.
- `DESIGN.md` §2.3 reads as built once implemented — no claim in the doc ahead of the code.
