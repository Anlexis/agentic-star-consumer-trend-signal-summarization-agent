# RET-C2-332 — Unit Tests: TrendRankNode
#
# Inner DomainWorkflowGraph node 3: score and rank the synthesized trend signals
# into an ordered ranked_trends list. Deterministic composite scoring
# (composite = weight * source_multiplier + corroboration_boost) — no LLM in v1.
#
# Behaviour asserted by these tests:
#   - synthesized_signals.merged_items with N items -> ranked_trends sorted
#     descending by composite_score, each item 1-indexed by rank.
#   - The same keyword appearing in two sources earns a corroboration boost,
#     ranking it above an otherwise-equal single-source keyword.
#   - out_of_scope=True -> early exit returns {"status": SUCCESS}; no ranked_trends.
#   - merged_items present but not a list -> status=ERROR.
#
# S-4 audit muted at the node module via an autouse fixture (NEVER stub shared.*).

import pytest

from src.nodes.trend_rank_node import TrendRankNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.trend_rank_node.emit_trace_event",
        lambda *a, **k: None,
    )


def _item(keyword, weight, source="social", metadata=None):
    return {
        "source": source,
        "keyword": keyword,
        "weight": float(weight),
        "metadata": metadata or {},
    }


class TestTrendRankNode:
    """Unit tests for TrendRankNode (composite-score ranking)."""

    def setup_method(self):
        self.node = TrendRankNode()

    def test_happy_path_ranks_descending_by_score(self):
        """5 merged_items -> ranked_trends sorted descending by composite_score."""
        merged_items = [
            _item("alpha", 1.0, source="search"),  # 1.0 * 0.9 = 0.90
            _item("bravo", 5.0, source="sales"),  # 5.0 * 1.1 = 5.50
            _item("charlie", 3.0, source="social"),  # 3.0 * 1.0 = 3.00
            _item("delta", 2.0, source="social"),  # 2.0 * 1.0 = 2.00
            _item("echo", 4.0, source="search"),  # 4.0 * 0.9 = 3.60
        ]
        result = self.node.execute({"synthesized_signals": {"merged_items": merged_items}})

        assert result["status"] == AgentStatus.SUCCESS
        ranked = result["ranked_trends"]
        assert isinstance(ranked, list)
        assert len(ranked) == 5
        # Ranks are 1-indexed and contiguous.
        assert [t["rank"] for t in ranked] == [1, 2, 3, 4, 5]
        # Scores are monotonically non-increasing.
        scores = [t["composite_score"] for t in ranked]
        assert scores == sorted(scores, reverse=True)
        # Highest scorer is the sales-weighted 'bravo'.
        assert ranked[0]["keyword"] == "bravo"

    def test_cross_source_corroboration_boosts_score(self):
        """A keyword in 2 sources outranks an equal-weight single-source keyword."""
        merged_items = [
            # Same keyword from two sources -> sources accumulate -> +0.2 boost.
            _item("trendy", 2.0, source="social"),
            _item("trendy", 2.0, source="search"),
            # A single-source keyword with the same base weight, same multiplier.
            _item("solo", 2.0, source="social"),
        ]
        result = self.node.execute({"synthesized_signals": {"merged_items": merged_items}})

        assert result["status"] == AgentStatus.SUCCESS
        ranked = result["ranked_trends"]
        by_kw = {t["keyword"]: t for t in ranked}
        # The corroborated keyword scored strictly higher than the solo one.
        assert by_kw["trendy"]["composite_score"] > by_kw["solo"]["composite_score"]
        # And its sources list reflects both contributing sources.
        assert set(by_kw["trendy"]["sources"]) == {"social", "search"}
        # Corroborated keyword ranks first.
        assert ranked[0]["keyword"] == "trendy"

    def test_out_of_scope_early_exit_no_ranking(self):
        """out_of_scope=True -> returns SUCCESS only; ranked_trends absent."""
        result = self.node.execute({"out_of_scope": True, "synthesized_signals": {"merged_items": []}})

        assert result["status"] == AgentStatus.SUCCESS
        assert "ranked_trends" not in result

    def test_merged_items_wrong_type_is_error(self):
        """merged_items present but not a list -> status=ERROR with error_log."""
        result = self.node.execute({"synthesized_signals": {"merged_items": "not-a-list"}})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]

    def test_missing_synthesized_signals_is_error(self):
        """synthesized_signals absent -> status=ERROR."""
        result = self.node.execute({})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]
