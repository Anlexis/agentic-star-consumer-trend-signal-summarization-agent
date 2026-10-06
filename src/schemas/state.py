"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# RET-C2-332 — Consumer Trend & Social Signal Summarization Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Confidentiality note: synthesized_signals and ranked_trends may carry
# competitor activity data.  Downstream nodes (SummaryGenerateNode,
# OutputValidateNode) must mask raw competitor identifiers before surfacing
# any output to the caller.  Do NOT log these fields raw.

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for RET-C2-332.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — InputValidateNode (pre-process slot)
    # ------------------------------------------------------------------

    # Validated multi-source signal payload produced by InputValidateNode (S-1).
    # Raw user_input is not persisted beyond InputValidateNode.
    validated_input: Optional[Dict[str, Any]]

    # Structured source signals dict keyed by source type
    # (e.g. {"social": [...], "sales": [...], "review": [...]}).
    # Written by InputValidateNode; read by SignalSynthesizeNode.
    signals: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — SignalSynthesizeNode
    # ------------------------------------------------------------------

    # Merged and de-duplicated signal dict produced by SignalSynthesizeNode.
    # Keys mirror those of `signals` but with cross-source deduplication applied.
    # Read by TrendRankNode.
    synthesized_signals: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — TrendRankNode
    # ------------------------------------------------------------------

    # Ranked list of trend items produced by TrendRankNode.
    # Each entry: {"rank": int, "topic": str, "score": float, "sources": list}
    # Read by SummaryGenerateNode.
    ranked_trends: Optional[List[Dict[str, Any]]]

    # ------------------------------------------------------------------
    # Inner domain workflow — SummaryGenerateNode (main slot)
    # ------------------------------------------------------------------

    # Structured trend summary produced by SummaryGenerateNode.
    # Keys: "headline" (str), "top_trends" (list), "insights" (str).
    # S-3: this field must pass the output gate before surfacing to caller.
    # The main-slot node also writes state["result"] from this dict.
    trend_summary: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Outer layer — OutputValidateNode (post-process slot)
    # ------------------------------------------------------------------

    # S-3-gated final output written by OutputValidateNode.
    # Competitor activity fields are masked before this field is written.
    # Consumed by outer merge_output().
    validated_output: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Control / routing flags
    # ------------------------------------------------------------------

    # True when the input falls outside the consumer-trend / social-signal
    # domain as determined by InputValidateNode or OutputValidateNode.
    # Out-of-scope is NOT a separate AgentStatus — status stays SUCCESS,
    # and merge_output() checks this flag to shape the caller response.
    out_of_scope: bool

    # Closed-set reason codes explaining an out-of-scope decision. Every value is
    # authored by InputValidateNode, so caller text can never travel on this
    # field — which is what lets it be surfaced in the caller response.
    scope_reasons: List[str]

    # Which caller channel the request payload arrived on: "input_context",
    # "user_input", or "none". Written by PreProcessNode, audit-only.
    payload_channel: Optional[str]

    # ------------------------------------------------------------------
    # Status and audit — populated by all nodes
    # ------------------------------------------------------------------

    # AgentStatus string value ("SUCCESS" or "ERROR") set by each node.
    # Declared explicitly here so outer merge_output() can read it from
    # the inner graph's merged state without an attribute-access miss.
    status: Optional[str]

    # Accumulated error messages appended by any node that catches an
    # exception. OutputValidateNode gates on this list before writing
    # validated_output.
    error_log: List[str]

    # ------------------------------------------------------------------
    # Tracing / audit
    # ------------------------------------------------------------------

    # Correlation ID injected by InitializeNode for audit log correlation.
    # OutputValidateNode includes this in every emit_trace_event() call.
    trace_id: Optional[str]
