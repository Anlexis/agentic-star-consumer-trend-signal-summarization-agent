# Test Specification — RET-C2-332

## Strategy

Three layers, each answering a question the others cannot.

- **Unit** — the contract and each node in isolation. The caller-data tests call
  `execute()` (and the contract functions) DIRECTLY, with no framework wrapper in
  front. That is deliberate: the template owns these guarantees, and asserting them
  through a framework gate would only prove they hold where that gate is active.
- **Boundary (proof-of-boundary)** — the security-relevant edges: what the entry
  contract accepts, what the output boundary is allowed to release, and the order
  the framework enforces around every node.
- **End-to-end** — the whole stack through the real ASGI `/invoke` with bearer
  auth. Some properties exist only here: whether caller data actually crosses the
  layer boundary, and whether the response a caller receives carries what it
  should.

Assertions are behavioural throughout — a value is accepted or refused, output is
released or withheld — never the wording of a framework message. Wording changes
between framework versions; behaviour is the contract.

## What ships

| File | Tests | Covers |
|---|---:|---|
| `tests/unit/test_request_contract.py` | 78 | the caller-data contract: the non-finite matrix per field, booleans and non-numerics, magnitude bounds, the keyword class in both directions (including Japanese), control tokens, markup-split directives, hostile field names, escaped payloads, depth and size caps, credential screening |
| `tests/unit/test_graph.py` | 12 | outer and inner graph wiring, the payload bridge, and output withholding on an inner failure |
| `tests/unit/test_output_validate_node.py` | 9 | the output invariant, field clearing on violation, closed-set reasons, credential floor |
| `tests/unit/test_input_validate_node.py` | 8 | scope decisions, closed-set reason codes, competitor masking in the audit trail |
| `tests/unit/test_signal_synthesize_node.py` | 8 | merge, deduplication, source accumulation, non-finite refusal, source labelling |
| `tests/unit/test_summary_generate_node.py` | 5 | briefing synthesis and the advisory disclaimer |
| `tests/unit/test_trend_rank_node.py` | 5 | composite scoring, ranking order, caps |
| `tests/unit/test_main_node.py` | 3 | the sample node's contract shape |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | 2 | framework compliance |
| `tests/proof_of_boundary/test_pb_invoke_endpoint.py` | 26 | end-to-end through the real ASGI entry — see below |
| `tests/proof_of_boundary/test_pb_ret_c2_332.py` | 4 | the three mandatory domain boundary scenarios |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | 2 | the framework's enforced call order and trust denial |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | 2 | conditional; skipped, this template declares no human-in-the-loop step |
| `tests/proof_of_boundary/test_import_isolation.py` | 1 | no platform SDK import |
| `tests/proof_of_boundary/test_state_safety.py` | 1 | flat state, no credentials, no unserialisable objects |

**164 passing, 2 skipped.**

## End-to-end groups

| Group | What a failure would mean |
|---|---|
| Real work on the public path | the agent answers from a baseline rather than from the caller's signals |
| The payload bridge | structured caller data never crosses the layer boundary, and the pipeline silently answers out of scope |
| Entry authentication | a caller with no credential, or the wrong one, reaches the pipeline |
| Caller-input refusals | a non-finite weight, an instruction-shaped keyword or a hostile field name reaches the domain logic |
| Context-channel refusals | a credential-shaped context value produces an opaque first-node failure instead of an actionable refusal |
| Output boundary | a withheld summary rides out inside the error envelope |
| Deploy smoke payload | the committed deployment payload and the tests assert different contracts |

The output-boundary group is probed against a control on the identical request
shape, so a pass distinguishes "the answer was withheld" from "no answer was ever
produced".

## Notes on two properties that need stating

**The deploy payload is generated from the boundary suite's own fixture.** The
deployment check posts `deploy/invoke_payload.json` verbatim, and a payload the
entry contract refuses still produces a well-formed HTTP 200 with an error answer
inside it — every surrounding assertion passes. Two tests pin it: the file matches
the fixture, and the file succeeds through `/invoke`.

**Containment is asserted at the node, not only end-to-end.** The nested layers
here contain an inner failure on their own — the subgraph node re-raises before any
output can be merged upward — so an end-to-end assertion alone would pass whether or
not the output node cleared anything. Mutation measurements: removing the node's
clearing fails 2 tests; removing the outer withholding fails 1; restoring the
original pre-migration branch fails 3. The end-to-end containment test fails under
none of them, and is documented as a regression guard rather than as the proof.
