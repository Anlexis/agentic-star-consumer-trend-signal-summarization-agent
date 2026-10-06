"""AgentCore Platform v1.0"""

# RET-C2-332 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full consumer trend signal summarization domain workflow:
#
#   START → input_validate → signal_synthesize → trend_rank
#         → summary_generate → output_validate → END
#
# Called by TrendSummaryGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() designed together with TrendSummaryGraphNode.merge_output()
#   ❌ No Level 0 platform SDK imports
#   ❌ Not placed under src/subagents/

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph import context_bridge
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_validate_node import OutputValidateNode
from src.nodes.signal_synthesize_node import SignalSynthesizeNode
from src.nodes.summary_generate_node import SummaryGenerateNode
from src.nodes.trend_rank_node import TrendRankNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for RET-C2-332.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by TrendSummaryGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate     (InputValidateNode)
          → signal_synthesize  (SignalSynthesizeNode)
          → trend_rank         (TrendRankNode)
          → summary_generate   (SummaryGenerateNode)
          → output_validate    (OutputValidateNode)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "ret_c2_332_domain_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        No mandatory config keys for v1 — domain nodes handle their own
        fallbacks via config["configurable"].
        """
        pass

    # ── Caller payload ────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated caller payload handed over by the outer graph.

        The framework's subgraph node does not forward ``input_context``, so the
        outer node stashes the already-validated payload and this hook picks it
        up as the inner graph builds its initial state. ``consume()`` clears the
        slot, so an inner graph invoked without a preceding stash starts with no
        payload rather than inheriting a previous request's.
        """
        payload = context_bridge.consume()
        return {"validated_input": payload} if payload is not None else {}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["signal_synthesize"] = SignalSynthesizeNode()
        self._nodes["trend_rank"] = TrendRankNode()
        self._nodes["summary_generate"] = SummaryGenerateNode()
        self._nodes["output_validate"] = OutputValidateNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear consumer trend summarization domain topology.

        Each step passes its partial-dict output into the shared State.
        The topology is intentionally linear — no conditional branching
        between domain nodes. route() is implemented as required by the ABC
        but add_conditional_edges() is not used.

        Flow:
          input_validate → signal_synthesize → trend_rank
                        → summary_generate → output_validate
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "signal_synthesize")
        self._sg.add_edge("signal_synthesize", "trend_rank")
        self._sg.add_edge("trend_rank", "summary_generate")
        self._sg.add_edge("summary_generate", "output_validate")
        self._sg.add_edge("output_validate", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime. It is implemented to satisfy the ABC
        contract. Returns END on error so an unexpected call does not re-enter
        a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_validate"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by TrendSummaryGraphNode.merge_output()
        in graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()   emits: "validated_output", "status",
                                        "trace_id", "correlation_id",
                                        "node_history"
            Outer merge_output() reads: sub_result.get("validated_output"),
                                        sub_result.get("status")

        Additional fields (trace_id, correlation_id, node_history) are
        surfaced for observability / downstream extension.
        """
        return {
            "validated_output": state.get("validated_output"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

    def get_state_class(self) -> type:
        """Return the State TypedDict used by both inner and outer graphs."""
        return State
