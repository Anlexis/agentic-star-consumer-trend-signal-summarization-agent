"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state, config) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-332 — InputValidateNode
# First node of the inner domain workflow. Decides whether the validated request
# is one this agent can answer, and shapes the source buckets the synthesis step
# consumes.
#
# Division of responsibility, deliberately kept sharp:
#   * src/schemas/request_contract.py decides whether the payload is ACCEPTABLE
#     — types, bounds, character classes, instruction shapes. Violations are
#     rejections and never reach this node.
#   * This node decides whether the accepted payload is ANSWERABLE — a known
#     retail category, a parseable period, at least one signal source. A request
#     that is well-formed but outside what the agent covers is not an error: it
#     returns SUCCESS with out_of_scope set, so a caller can tell "I cannot help
#     with this" apart from "something went wrong".
#
# Rejection and scope reasons are CODES, never the rejected value. Caller text
# that failed a check is exactly the text that must not be reflected back into an
# error message, a log line or an audit event.
#
# Competitor rows are already reduced to category-level intent by the request
# contract, so no raw competitor identity exists in state by the time this node
# runs — the masking is structural, not a scrubbing step that could be skipped.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Recognised retail category strings (lower-cased for comparison).
# Requests referencing a category outside this set are out-of-scope.
_KNOWN_RETAIL_CATEGORIES: frozenset[str] = frozenset(
    {
        "apparel",
        "electronics",
        "furniture",
        "grocery",
        "beauty",
        "sports",
        "home",
        "toys",
        "automotive",
        "jewelry",
        "books",
        "health",
        "outdoor",
        "pet",
        "stationery",
    }
)

# Period validation: accept "N weeks", "N days", "N months",
# or a date-range pattern like "2024-01-01:2024-03-31".
_PERIOD_INT_RE = re.compile(r"^\d+\s*(week|weeks|day|days|month|months)$", re.IGNORECASE)
_PERIOD_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[:\s/]\d{4}-\d{2}-\d{2}$")

# Keys that carry signal sources in the validated payload.
_SIGNAL_SOURCE_KEYS: List[str] = [
    "social_signals",
    "search_trends",
    "sales_velocity",
    "competitor_activity",
]

# Mapping from payload keys to the canonical signals dict keys.
_SIGNAL_KEY_MAP: Dict[str, str] = {
    "social_signals": "social",
    "search_trends": "search",
    "sales_velocity": "sales",
    "competitor_activity": "competitor",
}

# Scope reason codes. A closed set: every value here is authored in this file,
# so nothing a caller supplied can travel out through this channel.
_REASON_NO_PAYLOAD = "no_structured_payload"
_REASON_CATEGORY_MISSING = "category_missing"
_REASON_CATEGORY_UNKNOWN = "category_not_recognised"
_REASON_PERIOD_MISSING = "period_missing"
_REASON_PERIOD_UNPARSEABLE = "period_not_parseable"
_REASON_NO_SIGNALS = "no_signal_sources"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _is_valid_period(period: str) -> bool:
    """Return True if *period* is parseable as a date range or integer weeks/days/months."""
    period = period.strip()
    return bool(_PERIOD_INT_RE.match(period) or _PERIOD_RANGE_RE.match(period))


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class InputValidateNode(FunctionNode):
    """Decide whether the validated request is answerable, and shape the signals.

    Input state keys:
        validated_input: dict | None — payload accepted by the request contract,
            or None when the caller supplied no structured payload.

    Output state keys (partial dict — only changed keys):
        validated_input: normalised payload (category / period / source buckets)
        signals:         canonical source buckets {social, search, sales, competitor}
        out_of_scope:    bool
        scope_reasons:   list[str] — closed-set reason codes, never caller text
        status:          AgentStatus.SUCCESS (scope decisions are not errors)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = state.get("validated_input")

        # ------------------------------------------------------------------
        # 1. No structured payload — the documented baseline, not a failure.
        # ------------------------------------------------------------------
        if not isinstance(payload, dict):
            emit_trace_event(
                "input_out_of_scope",
                {"reasons": [_REASON_NO_PAYLOAD]},
                state,
            )
            return {
                "validated_input": {},
                "signals": {},
                "out_of_scope": True,
                "scope_reasons": [_REASON_NO_PAYLOAD],
                "status": AgentStatus.SUCCESS,
            }

        category = payload.get("category")
        period = payload.get("period")

        # ------------------------------------------------------------------
        # 2. Scope decision — closed-set reason codes only.
        # ------------------------------------------------------------------
        reasons: List[str] = []

        if not category:
            reasons.append(_REASON_CATEGORY_MISSING)
        elif str(category).strip().lower() not in _KNOWN_RETAIL_CATEGORIES:
            reasons.append(_REASON_CATEGORY_UNKNOWN)

        if not period:
            reasons.append(_REASON_PERIOD_MISSING)
        elif not _is_valid_period(str(period)):
            reasons.append(_REASON_PERIOD_UNPARSEABLE)

        has_any_signal = any(payload.get(key) for key in _SIGNAL_SOURCE_KEYS)
        if not has_any_signal:
            reasons.append(_REASON_NO_SIGNALS)

        signals: Dict[str, Any] = {
            canonical: payload.get(source_key) or [] for source_key, canonical in _SIGNAL_KEY_MAP.items()
        }

        if reasons:
            logger.info("InputValidateNode: request out of scope — reasons: %s", ",".join(reasons))
            emit_trace_event("input_out_of_scope", {"reasons": reasons}, state)
            return {
                "validated_input": payload,
                "signals": signals,
                "out_of_scope": True,
                "scope_reasons": reasons,
                "status": AgentStatus.SUCCESS,
            }

        # ------------------------------------------------------------------
        # 3. In scope — normalise the identity fields the summary renders.
        # ------------------------------------------------------------------
        cleaned_payload: Dict[str, Any] = {
            "category": str(category).strip().lower(),
            "period": str(period).strip(),
        }
        for source_key in _SIGNAL_SOURCE_KEYS:
            if payload.get(source_key):
                cleaned_payload[source_key] = payload[source_key]
        if payload.get("regulatory"):
            cleaned_payload["regulatory"] = payload["regulatory"]

        source_count = sum(
            1 for source_key in _SIGNAL_SOURCE_KEYS if source_key != "competitor_activity" and payload.get(source_key)
        )

        logger.info(
            "InputValidateNode: in scope — category=%s source_count=%d",
            cleaned_payload["category"],
            source_count,
        )

        # Audit trace. `category` is a closed-set value by this point (it matched
        # the known-category set), so it is the agent's own vocabulary rather than
        # caller text; the period and every signal value are deliberately absent.
        emit_trace_event(
            "input_validated",
            {
                "category": cleaned_payload["category"],
                "source_count": source_count,
                "competitor_present": bool(payload.get("competitor_activity")),
            },
            state,
        )

        return {
            "validated_input": cleaned_payload,
            "signals": signals,
            "out_of_scope": False,
            "scope_reasons": [],
            "status": AgentStatus.SUCCESS,
        }
