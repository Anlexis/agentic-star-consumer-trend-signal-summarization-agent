"""AgentCore Platform v1.0"""

# RET-C2-332 — PostProcessNode (outer post_process slot).
#
# Shapes the caller-facing response envelope from the gated summary the domain
# pipeline produced. It deliberately does NOT re-run the output gate: a second
# copy of that check here would contain a leak on its own and thereby make the
# clearing in OutputValidateNode impossible to falsify — more defence buying less
# assurance. The two nodes answer different questions. That one asks "may this be
# released?"; this one asks "is there anything to release?".

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

#: Note carried in every response, describing what the report does and does not show.
OUTPUT_SCHEMA_NOTE = (
    "Ranked aggregate trend signals only. Individual source records are not "
    "rendered, and competitor activity is reported at category level."
)


class PostProcessNode(FunctionNode):
    """Build the caller-facing response envelope."""

    # Formatting an already-gated result carries no privilege of its own.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result: Any = state.get("result")

        if not isinstance(result, dict) or not result:
            # Nothing was released by the output gate. Publishing an empty or
            # partial envelope here would turn "withheld" into "answered".
            emit_trace_event(
                "output_formatted",
                {"node": self.__class__.__name__, "published": False},
                state,
            )
            return {
                "formatted_output": None,
                "result": None,
                "status": AgentStatus.ERROR,
                "error_log": (state.get("error_log") or []) + ["PostProcessNode: no released output to format"],
            }

        out_of_scope = bool(result.get("out_of_scope"))
        envelope: Dict[str, Any] = {
            "template_id": "RET-C2-332",
            "out_of_scope": out_of_scope,
            "schema_note": OUTPUT_SCHEMA_NOTE,
            "report": result,
        }

        emit_trace_event(
            "output_formatted",
            {
                "node": self.__class__.__name__,
                "published": True,
                "out_of_scope": out_of_scope,
            },
            state,
        )

        return {
            "formatted_output": envelope,
            "status": AgentStatus.SUCCESS,
        }
