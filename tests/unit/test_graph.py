# RET-C2-332 — Unit Tests: outer graph (ConsumerTrendSummaryAgent) +
#                          inner graph (DomainWorkflowGraph) wiring
#
# Behaviour asserted by these tests:
#   - ConsumerTrendSummaryAgent (outer AgentBaseGraph) registers
#     TrendSummaryGraphNode in the "main" slot and does NOT override add_edges().
#   - DomainWorkflowGraph (inner BaseGraph) registers all 5 domain nodes in
#     pipeline order: input_validate -> signal_synthesize -> trend_rank ->
#     summary_generate -> output_validate.
#   - TrendSummaryGraphNode.merge_output() maps sub_result["validated_output"]
#     -> "result" on success (changed keys only), and withholds every output
#     field when the inner result is not a success.
#   - extract_input() bridges the validated payload to the inner graph, which is
#     the only path structured caller data has across the layer boundary.
#
# Framework-dependent instantiation is wrapped in ImportError skips (the SDK
# wheel may be absent in a bare local checkout). The merge_output / extract_input
# checks are pure dict ops and run unconditionally once the class imports.

import pytest


class TestInnerDomainWorkflowGraph:
    """Inner BaseGraph: identity, node registration, output shaping."""

    def test_inner_graph_instantiates(self):
        """DomainWorkflowGraph() must not raise NotImplementedError (ABCs filled)."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph

            graph = DomainWorkflowGraph()
            assert graph is not None
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")
        except NotImplementedError as exc:  # pragma: no cover
            pytest.fail(f"DomainWorkflowGraph has unimplemented ABC methods: {exc}")

    def test_inner_graph_identity(self):
        """Inner graph name + state_schema are the shared workflow id and State."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
            from src.schemas.state import State
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        graph = DomainWorkflowGraph()
        assert graph.name == "ret_c2_332_domain_workflow"
        assert graph.state_schema is State

    def test_register_nodes_all_five_in_pipeline_order(self):
        """All 5 domain nodes register in the linear pipeline order."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
            from src.nodes.input_validate_node import InputValidateNode
            from src.nodes.signal_synthesize_node import SignalSynthesizeNode
            from src.nodes.trend_rank_node import TrendRankNode
            from src.nodes.summary_generate_node import SummaryGenerateNode
            from src.nodes.output_validate_node import OutputValidateNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        graph = DomainWorkflowGraph()
        # _nodes is populated by register_nodes(); call it directly to avoid
        # depending on a full compile().
        graph._nodes = {}
        graph.register_nodes()

        keys = list(graph._nodes.keys())
        assert keys == [
            "input_validate",
            "signal_synthesize",
            "trend_rank",
            "summary_generate",
            "output_validate",
        ], f"Domain nodes must register in pipeline order, got {keys}"
        assert isinstance(graph._nodes["input_validate"], InputValidateNode)
        assert isinstance(graph._nodes["signal_synthesize"], SignalSynthesizeNode)
        assert isinstance(graph._nodes["trend_rank"], TrendRankNode)
        assert isinstance(graph._nodes["summary_generate"], SummaryGenerateNode)
        assert isinstance(graph._nodes["output_validate"], OutputValidateNode)

    def test_get_output_surfaces_validated_output(self):
        """get_output() emits validated_output + status + trace_id + node_history."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        graph = DomainWorkflowGraph()
        state = {
            "validated_output": {"headline": "H"},
            "status": "SUCCESS",
            "trace_id": "T-1",
            "correlation_id": "C-1",
            "node_history": ["input_validate"],
        }
        out = graph.get_output(state)
        assert out["validated_output"] == {"headline": "H"}
        assert "status" in out
        assert out["trace_id"] == "T-1"
        assert out["node_history"] == ["input_validate"]


class TestOuterGraphWiring:
    """Outer ConsumerTrendSummaryAgent + TrendSummaryGraphNode mapping methods."""

    def test_outer_agent_registers_main_graph_node(self):
        """register_nodes() puts TrendSummaryGraphNode in the 'main' slot."""
        try:
            from src.graph.graph import (
                ConsumerTrendSummaryAgent,
                TrendSummaryGraphNode,
            )
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        agent = ConsumerTrendSummaryAgent()
        agent._nodes = {}
        agent.register_nodes()

        assert "main" in agent._nodes
        assert isinstance(agent._nodes["main"], TrendSummaryGraphNode)

    def test_outer_graph_does_not_override_add_edges(self):
        """The outer backbone wiring belongs to the framework — add_edges not overridden."""
        try:
            from src.graph.graph import ConsumerTrendSummaryAgent
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        # add_edges must NOT be defined directly on the outer subclass.
        assert "add_edges" not in ConsumerTrendSummaryAgent.__dict__

    def test_outer_agent_identity(self):
        """ConsumerTrendSummaryAgent name + state_schema + Graph alias are correct."""
        try:
            from src.graph.graph import ConsumerTrendSummaryAgent, Graph
            from src.schemas.state import State
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        agent = ConsumerTrendSummaryAgent()
        assert agent.name == "ret_c2_332"
        assert agent.state_schema is State
        assert Graph is ConsumerTrendSummaryAgent

    def test_merge_output_maps_validated_output_to_result(self):
        """merge_output maps sub_result['validated_output'] -> 'result' + status."""
        try:
            from framework.schemas.agent_status import AgentStatus
            from src.graph.graph import TrendSummaryGraphNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = TrendSummaryGraphNode()
        sub_result = {
            "validated_output": {"headline": "H"},
            "status": AgentStatus.SUCCESS.value,
        }
        merged = node.merge_output({}, sub_result)
        assert merged["result"] == {"headline": "H"}
        assert merged["status"] == AgentStatus.SUCCESS

    def test_merge_output_returns_only_changed_keys(self):
        """merge_output must NOT echo the outer state back."""
        try:
            from framework.schemas.agent_status import AgentStatus
            from src.graph.graph import TrendSummaryGraphNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = TrendSummaryGraphNode()
        outer_state = {"user_input": "raw", "session_id": "S-1"}
        sub_result = {"validated_output": {"headline": "H"}, "status": AgentStatus.SUCCESS.value}

        merged = node.merge_output(outer_state, sub_result)
        assert "user_input" not in merged
        assert "session_id" not in merged
        assert set(merged.keys()) == {"result", "status"}

    def test_merge_output_withholds_output_on_inner_error(self):
        """A non-success inner result carries NO output field upwards.

        The framework's get_output() falls back to state["result"] even on an
        error status, so leaving a value there would publish an answer the inner
        output gate had already refused.
        """
        try:
            from framework.schemas.agent_status import AgentStatus
            from src.graph.graph import TrendSummaryGraphNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = TrendSummaryGraphNode()
        sub_result = {
            "validated_output": {"headline": "leaked"},
            "status": AgentStatus.ERROR.value,
        }
        merged = node.merge_output({}, sub_result)

        assert merged["status"] == AgentStatus.ERROR
        assert merged["result"] is None
        assert merged["formatted_output"] is None
        assert "leaked" not in repr(merged)

    def test_extract_input_bridges_payload_to_inner_graph(self):
        """extract_input stashes the validated payload for the inner graph.

        The framework's subgraph node forwards no structured context, so this is
        the only path a validated payload has into the domain pipeline.
        """
        try:
            from src.graph import context_bridge
            from src.graph.graph import TrendSummaryGraphNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        context_bridge.clear()
        node = TrendSummaryGraphNode()
        payload = {"category": "electronics", "period": "4 weeks"}

        text = node.extract_input({"user_input": "summarise", "validated_input": payload})

        assert text == "summarise"
        assert context_bridge.peek() == payload
        # consume() clears the slot, so no payload survives into the next request.
        assert context_bridge.consume() == payload
        assert context_bridge.peek() is None
        context_bridge.clear()

    def test_get_subgraph_returns_inner_graph(self):
        """get_subgraph() returns a DomainWorkflowGraph instance."""
        try:
            from src.graph.graph import TrendSummaryGraphNode
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = TrendSummaryGraphNode()
        assert isinstance(node.get_subgraph(), DomainWorkflowGraph)
