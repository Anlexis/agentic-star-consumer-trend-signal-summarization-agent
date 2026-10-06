# Template Design Specification — RET-C2-332

Consumer Trend & Social Signal Summarization Agent. A deterministic pipeline that
turns caller-supplied demand signals into a ranked trend briefing for one retail
category. No model is invoked anywhere; the same signals always produce the same
briefing, which is what makes the output reviewable.

## Position in the framework

| Attribute | Value |
|---|---|
| Agent class | `ConsumerTrendSummaryAgent` (`src/graph/graph.py`; `Graph` is an alias) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 — domain-specific multi-step pipeline |
| Composition pattern | Two-layer nested: a subgraph node in the `main` slot delegates to an inner `BaseGraph` |
| Error propagation | `propagate` — an inner failure is re-raised at the boundary, never smoothed over |
| Generation mode | `deterministic` — no model call, no provider extra, no secret |

Three-layer separation holds: state is a flat TypedDict, every node is a
`FunctionNode` overriding `execute()` only, and the graph is assembled by
composition in `register_nodes()`.

## Architecture overview

### Outer backbone (`src/graph/graph.py`)

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (retry, max_retry from config/config.yaml)
                                  pre_process
```

`add_edges()` is deliberately NOT overridden — the backbone wiring belongs to the
framework.

| Node | Responsibility | Reads | Writes |
|---|---|---|---|
| initialize | framework default | — | session/trace identity, `input_context` |
| pre_process | `PreProcessNode` — the caller-data boundary | `user_input`, `input_context` | `validated_input`, `payload_channel` |
| main | `TrendSummaryGraphNode` — delegates to the inner graph | `validated_input`, `user_input` | `result`, `formatted_output` (cleared on failure) |
| post_process | `PostProcessNode` — response envelope | `result` | `formatted_output` |
| finalize | framework default | `node_history`, `error_log` | `response_metadata` |

### Inner domain pipeline (`src/graph/domain_workflow_graph.py`)

```
START → input_validate → signal_synthesize → trend_rank → summary_generate → output_validate → END
```

Linear by design — there is no conditional routing between domain nodes, so there
is no path callable whose annotation could project state fields away.

| Node | Responsibility | Reads | Writes |
|---|---|---|---|
| input_validate | is the accepted request ANSWERABLE? | `validated_input` | `signals`, `out_of_scope`, `scope_reasons` |
| signal_synthesize | merge buckets, deduplicate, accumulate sources | `signals` | `synthesized_signals` |
| trend_rank | composite scoring and ranking | `synthesized_signals` | `ranked_trends` |
| summary_generate | deterministic briefing synthesis | `ranked_trends`, `signals`, `validated_input` | `trend_summary` |
| output_validate | the output boundary | `trend_summary`, `out_of_scope` | `validated_output` (or clears everything) |

### Crossing the layer boundary

The framework's subgraph node calls `subgraph.invoke(user_input, ...)` and forwards
no structured context, so a caller payload that arrives on the outer graph is
invisible to every inner node. `src/graph/context_bridge.py` carries it across: the
outer node stashes the validated payload in a `ContextVar`, and the inner graph's
`_extra_initial_state()` consumes it — clearing the slot on read, so a payload can
never survive into an unrelated invocation. A `ContextVar` rather than a module
global is what makes that safe under concurrency.

## Caller-data contract

`src/schemas/request_contract.py` is the single place every bound lives.

| Field | Rule |
|---|---|
| `category` | string; recognised retail category, else the request is out of scope |
| `period` | string; week/day/month count or a date range, else out of scope |
| `social_signals` / `search_trends` / `sales_velocity` | ≤200 entries each, ≤500 total |
| `competitor_activity` | reduced to category-level intent at validation; identity never carried forward |
| `<item>.keyword` | ≤64 chars, held to an explicit character class (word characters in any script plus ` - . & / ( ) +`) |
| `<item>.weight` / `.score` / `.count` | finite, within ±1,000,000; bools and non-finite values refused |
| `<item>.metadata` | ≤10 keys, unrecognised keys DROPPED (never echoed) |
| any string | ≤512 chars; nesting ≤8 deep |

Four rules apply to every field:

1. **Finite and bounded.** `float("NaN")` survives a plain `float()` call and then
   compares False against every threshold, so an unchecked non-finite weight would
   sort and score silently. Every number goes through `finite_in_range`, which fails
   closed.
2. **Inert where it renders.** A keyword reaches the caller-visible summary
   verbatim, so it is held to an explicit character class and anything outside it is
   refused rather than escaped. The class is wider than a bare ASCII identifier
   pattern, deliberately: trend keywords are the domain payload and are frequently
   Japanese, so an identifier lock would make the agent unable to do its job. The
   class excludes every character used to structure markup, templates or
   delimiters, which is what makes chat-template control tokens structurally
   unrepresentable in a rendered keyword.
3. **Screened for instruction shapes.** The parsed payload is walked depth-first,
   KEYS INCLUDED, and each string is checked twice: as received, and with markup
   removed. The first pass catches control tokens (`<|…|>`, `[INST]`, `<<SYS>>`);
   the second catches a directive split by inserted markup that only reassembles
   after stripping. Scanning the parsed object rather than the raw body is what
   makes `\u`-escaped payloads unable to evade it.
4. **Never echoed.** A rejection names the field and the reason; the rejected value
   appears in no message, no log line and no audit event.

### Entry point

`POST /invoke` accepts `input` (the request line), `session_id`, and
`input_context`. The structured request travels on `input_context.trend_request`;
the same object sent as a JSON string in `input` is also accepted, tolerantly — a
plain request line is a legitimate call, not an error.

`input_context` is size-capped at the adapter, and screened for credential shapes
using the framework's own detector before `invoke()`. That screen exists because the
framework's initialize step returns `input_context` verbatim in its result and the
output gate scans every value of every result, so a credential-shaped value there
ends the run with an opaque error before any template code executes. The request
cannot succeed either way; refusing it at the adapter turns that into a 400 that
names the field.

Caller trust is resolved from a bearer credential at the adapter. Without that
resolution every standalone caller stays anonymous and the domain nodes — which
require an internal caller — deny the request at the trust gate.

## State definition

| Field | Type | Purpose |
|---|---|---|
| `validated_input` | `dict \| None` | normalised request payload |
| `payload_channel` | `str` | which channel the payload arrived on (audit only) |
| `signals` | `dict` | canonical source buckets |
| `synthesized_signals` | `dict` | merged, deduplicated items with their source lists |
| `ranked_trends` | `list[dict]` | scored and ranked trend items |
| `trend_summary` | `dict` | the generated briefing, before the output boundary |
| `validated_output` | `dict \| None` | what the output boundary released |
| `out_of_scope` | `bool` | request is well-formed but not answerable |
| `scope_reasons` | `list[str]` | closed-set reason codes |

State constraints (mandatory, and all satisfied here): flat TypedDict only, no
credentials in state, invocation context via `config["configurable"]` only, no
Pydantic models or arbitrary Python objects.

## Framework utilization

- [x] `InvocationContext` — correlation and session identity, caller trust level
- [x] `SecurityViolationError` — raised by the domain output gate
- [x] `detect_credentials` / `detect_credentials_in_value` — the framework detector, used as the FLOOR at both the adapter screen and the output gate
- [x] `emit_trace_event()` — at least one domain event on a reachable path in every `execute()`
- [ ] `_extra_security_gate_input()` — not used; the caller boundary owns input refusal directly, so it can be proved by calling `execute()` with no framework wrapper in front
- [ ] `_extra_security_gate_output()` — not used; the output boundary is a domain node in the pipeline, which keeps it testable in isolation

## The output invariant

The stated invariant, enforced for every representation:

1. a summary exists and is a mapping;
2. the advisory disclaimer is present and non-empty;
3. no competitor identity appears anywhere in the rendered structure, at any depth,
   in a key or a value;
4. at least one ranked trend is present;
5. no credential-shaped string appears anywhere in the rendered structure.

**This template renders no monetary aggregates** — no amounts, no currency markers,
no money grid anywhere in the output schema — so the rounding-grid gate that
money-rendering templates carry is not applicable here. The invariant above is this
agent's own, and it is what gets enforced completely instead.

Credential screening takes the UNION of the framework detector and a local pattern
set. Both directions matter: a local set narrower than the framework's lets a value
through that the framework then raises on inside the wrapper, which discards this
node's clearing; and delegating to the framework alone drops the assignment forms
(`password=…`), because the framework patterns describe credential formats and match
none of them. Wider is safe; narrower is a bypass.

**Containment.** A violation returns an error AND clears every output-bearing field.
Returning an error alone is not containment: the framework's `get_output()` falls
back to the result field even on an error status, so an output that merely failed to
be assigned can still be published inside the error envelope. The violation reason
travels as a closed-set code — an earlier form of this gate put the matched
competitor identity into the reason string, which meant blocking a value was the act
that published it.

The response envelope is closed-set labels and the released report only. Node error
text is never a caller-visible channel.

## Import isolation

- [x] The template imports no platform SDK (Level 0)
- [x] Import targets are `framework/` and `shared/` only

## Design decision record

| Decision | Chosen | Rationale |
|---|---|---|
| L1 base type | `AgentBaseGraph` | fixed backbone; the domain pipeline is encapsulated in the `main` slot |
| Composition pattern | nested subgraph | keeps a five-step domain topology out of the fixed backbone |
| Caller payload channel | `input_context` | escapes text-shaped processing that would mangle structured domain data; the credential screen at the adapter is the cost of that choice, and it is paid explicitly |
| Rendered-keyword class | wider than an ASCII identifier | trend keywords are the domain payload and are frequently Japanese; the class still excludes every markup and template character |
| Absent payload | out of scope, not an error | a caller must be able to tell "nothing to work from" apart from "something broke" |
| Entry trust level | internal | every domain node enforces it; the manifest now declares what the code enforces rather than a weaker level that was denied at the first node |
