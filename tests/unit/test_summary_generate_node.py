# RET-C2-332 — Unit Tests: SummaryGenerateNode
#
# Inner DomainWorkflowGraph node 4: build a structured trend_summary dict from
# ranked_trends. Deterministic template synthesis — no LLM in v1. The demand
# forecast section MUST carry the mandatory advisory DISCLAIMER (S-3, enforced
# downstream by OutputValidateNode).
#
# Behaviour asserted by these tests:
#   - ranked_trends with 5 items -> trend_summary dict with all required keys.
#   - trend_summary["demand_forecast"]["DISCLAIMER"] is a non-empty string.
#   - out_of_scope=True -> early exit returns {"status": SUCCESS}; trend_summary
#     absent.
#   - The competitor_summary carries NO raw competitor names (S-1).
#
# S-4 audit muted at the node module via an autouse fixture (NEVER stub shared.*).

import pytest

from src.nodes.summary_generate_node import SummaryGenerateNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.summary_generate_node.emit_trace_event",
        lambda *a, **k: None,
    )


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


class TestSummaryGenerateNode:
    """Unit tests for SummaryGenerateNode (template synthesis + DISCLAIMER)."""

    def setup_method(self):
        self.node = SummaryGenerateNode()

    def test_happy_path_builds_full_summary(self):
        """ranked_trends (5) -> trend_summary dict with all required keys."""
        state = {
            "ranked_trends": _ranked(5),
            "validated_input": {"category": "electronics", "period": "4 weeks"},
            "signals": {},
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        summary = result["trend_summary"]
        assert isinstance(summary, dict)
        for key in (
            "category",
            "period",
            "headline",
            "top_trends",
            "competitor_summary",
            "demand_forecast",
            "recommended_actions",
        ):
            assert key in summary, f"trend_summary missing required key: {key}"
        # top_trends is capped at the leading 5 ranked items.
        assert isinstance(summary["top_trends"], list)
        assert len(summary["top_trends"]) == 5

    def test_disclaimer_present_and_non_empty(self):
        """S-3 precondition: demand_forecast.DISCLAIMER is a non-empty string."""
        state = {
            "ranked_trends": _ranked(5),
            "validated_input": {"category": "electronics", "period": "4 weeks"},
            "signals": {},
        }
        result = self.node.execute(state)

        disclaimer = result["trend_summary"]["demand_forecast"]["DISCLAIMER"]
        assert isinstance(disclaimer, str)
        assert disclaimer.strip()  # non-empty after stripping whitespace

    def test_out_of_scope_early_exit_no_summary(self):
        """out_of_scope=True -> returns SUCCESS only; trend_summary absent."""
        result = self.node.execute({"out_of_scope": True, "ranked_trends": _ranked(5)})

        assert result["status"] == AgentStatus.SUCCESS
        assert "trend_summary" not in result

    def test_missing_ranked_trends_is_error(self):
        """ranked_trends missing or empty -> status=ERROR."""
        result = self.node.execute({"validated_input": {"category": "electronics", "period": "4 weeks"}})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]

    def test_competitor_names_not_in_competitor_summary(self):
        """S-1: no raw competitor identifiers leak into competitor_summary."""
        # Even when competitor signals are present in-state, the summary text is
        # a fixed category-level statement that never names a competitor.
        state = {
            "ranked_trends": _ranked(5),
            "validated_input": {"category": "electronics", "period": "4 weeks"},
            "signals": {
                "competitor": [{"name": "Rival Brand X", "share": 0.4}],
            },
        }
        result = self.node.execute(state)

        competitor_summary = result["trend_summary"]["competitor_summary"]
        assert "Rival Brand X" not in competitor_summary
        # The fixed S-1 statement is present instead.
        assert "withheld" in competitor_summary.lower()
