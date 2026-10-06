# RET-C2-332 — Proof-of-Boundary: 3 mandatory security boundary scenarios
#
# PoB-1  S-3 output gate : OutputValidateNode must REJECT a trend_summary that is
#                          missing demand_forecast.DISCLAIMER. The domain gate
#                          (_run_s3_domain_gate) raises SecurityViolationError;
#                          execute() catches it and returns status=ERROR. Either
#                          surfacing is acceptable — this test asserts the
#                          end-to-end execute() ERROR (the framework-catch path
#                          the merged code implements) and, independently, that
#                          the gate method itself raises.
#
# PoB-2  Advisory DISCLAIMER present : a happy-path run through
#                          SummaryGenerateNode -> OutputValidateNode always
#                          produces validated_output carrying the advisory
#                          DISCLAIMER, with status=SUCCESS.
#
# PoB-3  Out-of-scope -> SUCCESS : an unrecognised category at the input gate
#                          returns out_of_scope=True with status=SUCCESS — NOT an
#                          error boundary.
#
# SecurityViolationError is imported from the SAME path the node uses
# (framework.errors). Audit free functions are muted at each node module via an
# autouse fixture (NEVER stub shared.* in sys.modules — the CI wheel ships a real
# shared package).

import pytest

from src.nodes.input_validate_node import InputValidateNode
from src.nodes.summary_generate_node import SummaryGenerateNode
from src.nodes.output_validate_node import OutputValidateNode
from framework.schemas.agent_status import AgentStatus
from framework.errors import SecurityViolationError


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free functions at every node module under test."""
    monkeypatch.setattr("src.nodes.input_validate_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.summary_generate_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.output_validate_node.emit_trace_event", lambda *a, **k: None)


def _ranked(n=5):
    """A ranked_trends list of n items, scores descending."""
    return [
        {
            "rank": i + 1,
            "keyword": f"trend_{i}",
            "composite_score": round(0.9 - i * 0.1, 6),
            "sources": ["social", "search"],
            "metadata": {},
        }
        for i in range(n)
    ]


class TestPoB1S3GateRejectsMissingDisclaimer:
    """PoB-1: the S-3 output gate cannot be bypassed — missing DISCLAIMER fails."""

    def setup_method(self):
        self.node = OutputValidateNode()

    def _summary_without_disclaimer(self):
        return {
            "category": "electronics",
            "period": "4 weeks",
            "headline": "Top trend in electronics.",
            "top_trends": [{"rank": 1, "keyword": "earbuds", "score": 5.5, "sources": ["sales"]}],
            "competitor_summary": "Withheld per S-1 privacy guidelines.",
            # demand_forecast present but DISCLAIMER deliberately absent.
            "demand_forecast": {
                "outlook": "accelerating",
                "four_week_signal": "Consumer interest is accelerating.",
            },
            "recommended_actions": ["Increase stock levels."],
        }

    def test_execute_rejects_missing_disclaimer_as_error(self):
        """OutputValidateNode.execute() returns status=ERROR for a missing DISCLAIMER."""
        result = self.node.execute({"trend_summary": self._summary_without_disclaimer()})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]

    def test_gate_method_raises_on_missing_disclaimer(self):
        """The S-3 domain gate raises SecurityViolationError directly."""
        with pytest.raises(SecurityViolationError):
            self.node._run_s3_domain_gate(self._summary_without_disclaimer())


class TestPoB2DisclaimerPresentInOutput:
    """PoB-2: the advisory DISCLAIMER propagates to the final validated_output."""

    def test_disclaimer_present_in_validated_output(self):
        """SummaryGenerate -> OutputValidate yields validated_output with DISCLAIMER."""
        summary_node = SummaryGenerateNode()
        output_node = OutputValidateNode()

        gen_state = {
            "ranked_trends": _ranked(5),
            "validated_input": {"category": "electronics", "period": "4 weeks"},
            "signals": {},
        }
        gen_result = summary_node.execute(gen_state)
        assert gen_result["status"] == AgentStatus.SUCCESS

        # Carry the generated trend_summary into the output gate.
        out_result = output_node.execute({"trend_summary": gen_result["trend_summary"]})

        assert out_result["status"] == AgentStatus.SUCCESS
        disclaimer = out_result["validated_output"]["demand_forecast"]["DISCLAIMER"]
        assert isinstance(disclaimer, str)
        assert disclaimer.strip()  # present and non-empty


class TestPoB3OutOfScopeIsSuccess:
    """PoB-3: an out-of-scope request is graceful SUCCESS, not an error boundary."""

    def test_unknown_category_returns_success_and_out_of_scope(self):
        """Unrecognised category at the input gate -> SUCCESS + out_of_scope=True."""
        node = InputValidateNode()
        state = {
            "validated_input": {
                "category": "UNKNOWN_CATEGORY_XYZ",
                "period": "4 weeks",
                "social_signals": [{"keyword": "x", "weight": 1.0}],
            }
        }
        result = node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
