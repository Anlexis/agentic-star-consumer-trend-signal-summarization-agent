"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-332 — OutputValidateNode: the output boundary of the domain pipeline.
#
# The stated output invariant, enforced here for EVERY representation rather than
# for the convenient ones:
#
#   1. A summary exists and is a mapping.
#   2. The advisory disclaimer is present and non-empty. A demand signal that
#      ships without it reads as a guarantee, which is the one thing this agent
#      must never appear to give.
#   3. No competitor identity appears anywhere in the rendered structure — at any
#      nesting depth, in a key or in a value.
#   4. At least one ranked trend is present; an "empty" summary is not a summary.
#   5. No credential-shaped string appears anywhere in the rendered structure.
#
# This template does not render monetary aggregates — there are no amounts, no
# currency markers and no money grid anywhere in its output schema — so the
# precision-grid gate other templates carry is not applicable here. The invariant
# above is this agent's own, and it is what gets enforced completely instead.
#
# CONTAINMENT. A violation returns ERROR **and clears every output-bearing
# field**. Returning ERROR alone is not containment: the framework's own
# get_output() falls back to state["result"] even on an error status, so an
# output that merely failed to be assigned — rather than being actively
# cleared — can still be published inside the error envelope. The clearing is
# the guarantee; the status is only the label on it.
#
# The violation reason is a CLOSED SET of codes authored in this file. An earlier
# form of this gate put the matched competitor name into the reason string, which
# meant the one value the gate exists to withhold was written into error_log and
# the audit trail by the act of blocking it.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Competitor identities that must not appear raw in output.
# Operators may extend via config; these are the built-in defaults.
# This module-level list is treated as IMMUTABLE — execute() builds a local
# per-call copy (list(_DEFAULT_COMPETITOR_NAMES) + extra) and never mutates it,
# so operator-supplied names from one invocation cannot leak into another.
_DEFAULT_COMPETITOR_NAMES: List[str] = [
    "competitor_a",
    "competitor_b",
    "rival_brand",
]

# Credential shapes screened IN ADDITION to the framework detector, never
# instead of it. Taking the union matters in both directions, and both have bitten
# peers: a local set narrower than the framework's lets a value through here that
# the framework then raises on inside the wrapper, which discards this node's
# clearing entirely; and "delegating" to the framework alone drops the assignment
# forms below, because the framework patterns describe credential FORMATS and
# match none of them. Wider is safe; narrower is a containment bypass.
_LOCAL_CREDENTIAL_RES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("assignment", re.compile(r"\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S", re.IGNORECASE)),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("basic_auth_url", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@")),
)

# Closed-set violation codes. Every value is authored here, so nothing derived
# from caller data or from a blocked value can travel out on this channel.
VIOLATION_MISSING_SUMMARY = "missing_summary"
VIOLATION_MISSING_FORECAST = "missing_demand_forecast"
VIOLATION_MISSING_DISCLAIMER = "missing_disclaimer"
VIOLATION_COMPETITOR_IDENTITY = "competitor_identity_present"
VIOLATION_EMPTY_TRENDS = "empty_top_trends"
VIOLATION_CREDENTIAL = "credential_pattern_present"

# Every state field that can carry output. A violation clears all of them.
_OUTPUT_BEARING_FIELDS: Tuple[str, ...] = (
    "validated_output",
    "trend_summary",
    "result",
    "formatted_output",
)

# Response returned when the request was outside the agent's supported scope.
_SCOPE_EXCEEDED_RESPONSE: Dict[str, Any] = {
    "out_of_scope": True,
    "message": ("The requested category or period is outside the supported scope of this agent."),
    "status": "SUCCESS",
}


def _extract_all_strings(obj: Any, depth: int = 0) -> List[str]:
    """Collect every string in a nested structure — dict KEYS included.

    Keys are collected as well as values because a gate that reads only values is
    blind to an identity that arrives as a field name, and the structure being
    screened here is assembled from caller-influenced data.
    """
    collected: List[str] = []
    if depth > 16:
        return collected
    if isinstance(obj, str):
        collected.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                collected.append(k)
            collected.extend(_extract_all_strings(v, depth + 1))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            collected.extend(_extract_all_strings(item, depth + 1))
    return collected


class OutputValidateNode(FunctionNode):
    """Output gate — validate trend_summary before it can reach the caller.

    Input state keys:
        trend_summary: dict  — output from SummaryGenerateNode
        out_of_scope:  bool  — True if the request is outside agent scope
        scope_reasons: list  — closed-set reason codes from the input gate
        error_log:     list  — accumulated error messages (extended on failure)

    Output state keys (partial dict):
        validated_output: dict — final gated output (trend_summary on success;
                                  scope-exceeded response when out_of_scope=True)
        status:           AgentStatus — SUCCESS or ERROR

    Configuration (via config["configurable"]["node_config"]):
        competitor_names: list[str] — additional competitor identities to block
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def _run_s3_domain_gate(self, trend_summary: Any, competitor_names: Optional[List[str]] = None) -> None:
        """Enforce the output invariant, raising SecurityViolationError on breach.

        This is a domain method called inside execute(); it is not the framework
        base hook (which is marked final and must not be overridden).

        The exception message is a closed-set code. Callers that need to know
        which check failed read the code; nobody, including the audit trail, is
        told which value matched.
        """
        if competitor_names is None:
            competitor_names = list(_DEFAULT_COMPETITOR_NAMES)

        if not trend_summary or not isinstance(trend_summary, dict):
            raise SecurityViolationError(VIOLATION_MISSING_SUMMARY)

        demand_forecast = trend_summary.get("demand_forecast")
        if not demand_forecast or not isinstance(demand_forecast, dict):
            raise SecurityViolationError(VIOLATION_MISSING_FORECAST)
        disclaimer = demand_forecast.get("DISCLAIMER")
        if not disclaimer or not str(disclaimer).strip():
            raise SecurityViolationError(VIOLATION_MISSING_DISCLAIMER)

        all_strings = _extract_all_strings(trend_summary)
        combined_text = " ".join(all_strings)
        lowered = combined_text.lower()
        for name in competitor_names:
            if isinstance(name, str) and name and name.lower() in lowered:
                raise SecurityViolationError(VIOLATION_COMPETITOR_IDENTITY)

        if detect_credentials(combined_text):
            raise SecurityViolationError(VIOLATION_CREDENTIAL)
        for _label, pattern in _LOCAL_CREDENTIAL_RES:
            if pattern.search(combined_text):
                raise SecurityViolationError(VIOLATION_CREDENTIAL)

        top_trends = trend_summary.get("top_trends")
        if not top_trends or not isinstance(top_trends, list):
            raise SecurityViolationError(VIOLATION_EMPTY_TRENDS)

    @staticmethod
    def _contain(reason: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """Return an ERROR delta with every output-bearing field cleared.

        Nothing derived from the blocked summary is re-emitted here — not the
        text, not a truncation of it, not the reason it was blocked beyond the
        closed-set code.
        """
        contained: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
        contained["status"] = AgentStatus.ERROR
        contained["error_log"] = (state.get("error_log") or []) + [f"OutputValidateNode: output withheld ({reason})"]
        return contained

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        node_config: Dict[str, Any] = (config or {}).get("configurable", {}).get("node_config", {})

        # Allow operators to extend the competitor block-list via config.
        # Build a LOCAL per-call copy — never mutate the module-level default,
        # which would otherwise contaminate subsequent invocations.
        extra_competitors: List[str] = node_config.get("competitor_names", []) or []
        competitor_names: List[str] = list(_DEFAULT_COMPETITOR_NAMES) + [
            c for c in extra_competitors if c not in _DEFAULT_COMPETITOR_NAMES
        ]

        # --- Scope passthrough: request is outside the agent's supported scope ---
        if state.get("out_of_scope"):
            logger.info("OutputValidateNode: out_of_scope=True — returning scope response")
            scope_reasons = [r for r in (state.get("scope_reasons") or []) if isinstance(r, str)]
            emit_trace_event(
                "output_validated",
                {"out_of_scope": True, "reasons": scope_reasons, "top_trend_count": 0},
                state,
            )
            response = dict(_SCOPE_EXCEEDED_RESPONSE)
            response["reasons"] = scope_reasons
            return {
                "validated_output": response,
                "status": AgentStatus.SUCCESS,
            }

        # --- Normal path: run the output gate ---
        trend_summary = state.get("trend_summary")

        try:
            self._run_s3_domain_gate(trend_summary, competitor_names)
        except SecurityViolationError as exc:
            reason = str(exc)
            logger.error("OutputValidateNode: output withheld — %s", reason)
            emit_trace_event("output_security_violation", {"reason": reason}, state)
            return self._contain(reason, state)

        category = trend_summary.get("category", "unknown") if isinstance(trend_summary, dict) else "unknown"
        top_trend_count = len((trend_summary or {}).get("top_trends", []))

        logger.info(
            "OutputValidateNode: gate PASS — category=%s, top_trends=%d",
            category,
            top_trend_count,
        )

        emit_trace_event(
            "output_validated",
            {
                "out_of_scope": False,
                "category": category,
                "top_trend_count": top_trend_count,
                "disclaimer_verified": True,
            },
            state,
        )

        return {
            "validated_output": trend_summary,
            "status": AgentStatus.SUCCESS,
        }
