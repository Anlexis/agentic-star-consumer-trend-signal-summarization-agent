"""AgentCore Platform v1.0"""

# RET-C2-332 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (TrendSummaryGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Rules enforced:
#   ✅ ConsumerTrendSummaryAgent inherits AgentBaseGraph
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ TrendSummaryGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No Level 0 platform SDK imports

from typing import Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph import context_bridge
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class TrendSummaryGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of ConsumerTrendSummaryAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    — instantiate and return DomainWorkflowGraph
      extract_input()   — pass structured signals as JSON string into inner graph
      merge_output()    — map sub_result fields into outer state delta (changed keys only)
      error_strategy    — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default — fail fast).
    # "handle": call on_subgraph_error() instead — use for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: HITL interrupts are handled inside the inner graph only.
    # True: surface inner HITL interrupt to the outer caller.
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> BaseGraph:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the Cat 2 pattern.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph()

    def extract_input(self, state: AgentState) -> str:
        """Return the text passed into inner_graph.invoke(), and bridge the payload.

        The framework's subgraph node calls ``subgraph.invoke(user_input, ...)``
        and forwards no structured context, so the validated payload is handed
        across through the ContextVar bridge here and re-seeded by the inner
        graph's ``_extra_initial_state()``. See src/graph/context_bridge.py for
        why a ContextVar rather than the text channel.

        The returned string is the request line, not the payload: encoding a
        structured object into it would put domain data through text-shaped
        processing it was never meant for, and would lose its types on the way.
        """
        context_bridge.stash(state.get("validated_input"))
        user_input = state.get("user_input", "")
        return user_input if isinstance(user_input, str) else ""

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output()   emits: "validated_output", "status", "trace_id",
                                      "correlation_id", "node_history"
          This merge_output() reads: sub_result.get("validated_output"),
                                     sub_result.get("status")

        Containment: a non-success inner result never carries an output field
        upwards. The framework's own get_output() falls back to state["result"]
        even on an error status, so leaving a value there would publish an
        answer the inner output gate had already refused.
        """
        status = sub_result.get("status")
        inner_output = sub_result.get("validated_output")
        if status != AgentStatus.SUCCESS.value or inner_output is None:
            return {"result": None, "formatted_output": None, "status": AgentStatus.ERROR}
        return {
            "result": inner_output,
            "status": AgentStatus.SUCCESS,
        }


class ConsumerTrendSummaryAgent(AgentBaseGraph):
    """Outer graph for RET-C2-332 (Cat 2).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in TrendSummaryGraphNode (main slot), which delegates
    to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode (S-1 input validation)
      - main:        TrendSummaryGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (S-3 output gate)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "ret_c2_332"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = TrendSummaryGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat alias — callers may reference either name.
Graph = ConsumerTrendSummaryAgent
