"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Read input_context via state.get("input_context", {}) — read-only [C1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-332 — PreProcessNode (outer pre_process slot)
#
# This node owns the caller-data boundary. Everything the caller can influence is
# validated here, against the contract in src/schemas/request_contract.py, before
# any domain node sees it.
#
# Two caller channels are accepted, in this order:
#   1. input_context["trend_request"] — the structured channel, and the one the
#      documented contract describes.
#   2. user_input parsed as a JSON object — the same request object sent as text,
#      for callers (and the deployment smoke check) that have only the one field.
#      Parsing is TOLERANT: a request line that is not JSON is not an error, it
#      simply carries no structured payload.
#
# A request with no structured payload is not a failure either — it degrades to a
# documented insufficient-data response rather than erroring, so the absence of
# optional caller data can never be mistaken for a fault.
#
# The template owns these guarantees; it does not rely on the framework's input
# gate having run. The framework gate is defence in depth in front of this node,
# not a substitute for it — where it is absent or configured off, this node still
# refuses the same payloads, which is why its tests call execute() directly.

import json
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.request_contract import ContractError, validate_trend_request

#: Field on input_context that carries the structured request.
CALLER_PAYLOAD_KEY = "trend_request"

#: Largest request line accepted on the text channel.
MAX_USER_INPUT_LEN = 8192


class PreProcessNode(FunctionNode):
    """Validate the caller request before the domain pipeline runs.

    Output state keys (partial dict — only changed keys):
        validated_input:  normalised request payload dict, or None when the
                          caller supplied no structured payload
        payload_channel:  which channel the payload arrived on
        status:           AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:        field-naming rejection messages; never the value
    """

    # The caller-data boundary runs for every caller, including anonymous ones —
    # rejecting untrusted input is exactly what this node is for, so gating it
    # behind a higher trust level would remove the check from the callers that
    # most need it. Privileged work happens further down the pipeline.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    @staticmethod
    def _payload_from_text(user_input: Any) -> Optional[Dict[str, Any]]:
        """Return the request object encoded in *user_input*, or None.

        Tolerant by design: a plain request line is a legitimate call, so a text
        body that is not a JSON object yields None rather than a rejection.
        """
        if not isinstance(user_input, str):
            return None
        candidate = user_input.strip()
        if not candidate.startswith("{"):
            return None
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context") or {}  # read-only [C1]

        if not isinstance(user_input, str) or not user_input.strip():
            emit_trace_event(
                "request_rejected",
                {"node": self.__class__.__name__, "field": "user_input", "reason": "empty"},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        if len(user_input) > MAX_USER_INPUT_LEN:
            emit_trace_event(
                "request_rejected",
                {"node": self.__class__.__name__, "field": "user_input", "reason": "too_long"},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": [f"PreProcessNode: user_input exceeds the {MAX_USER_INPUT_LEN}-character limit"],
            }

        raw_payload: Optional[Dict[str, Any]] = None
        channel = "none"
        if isinstance(input_context, dict) and input_context.get(CALLER_PAYLOAD_KEY) is not None:
            raw_payload = input_context.get(CALLER_PAYLOAD_KEY)
            channel = "input_context"
        else:
            raw_payload = self._payload_from_text(user_input)
            if raw_payload is not None:
                channel = "user_input"

        if raw_payload is None:
            # No structured payload: degrade to the documented baseline rather
            # than erroring. The domain pipeline reports insufficient data.
            emit_trace_event(
                "request_accepted",
                {"node": self.__class__.__name__, "channel": "none", "signal_sources": 0},
                state,
            )
            return {
                "validated_input": None,
                "payload_channel": "none",
                "status": AgentStatus.SUCCESS,
            }

        try:
            payload = validate_trend_request(raw_payload)
        except ContractError as exc:
            # exc carries the FIELD and the reason; the rejected value is never
            # part of the message, the audit event or the log.
            emit_trace_event(
                "request_rejected",
                {"node": self.__class__.__name__, "field": exc.field, "reason": exc.reason},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": [f"PreProcessNode: {exc.field} {exc.reason}"],
            }

        emit_trace_event(
            "request_accepted",
            {
                "node": self.__class__.__name__,
                "channel": channel,
                "signal_sources": sum(
                    1 for key in ("social_signals", "search_trends", "sales_velocity") if payload.get(key)
                ),
                "competitor_present": bool(payload.get("competitor_activity")),
            },
            state,
        )

        return {
            "validated_input": payload,
            "payload_channel": channel,
            "status": AgentStatus.SUCCESS,
        }
