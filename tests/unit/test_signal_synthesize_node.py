# RET-C2-332 — Unit Tests: SignalSynthesizeNode
#
# Inner DomainWorkflowGraph node 2: merge and deduplicate the multi-source
# signals dict (social / search / sales / competitor) into a unified
# synthesized_signals dict. Purely deterministic — no LLM in v1.
#
# Behaviour asserted by these tests:
#   - signals with 3 sources -> synthesized_signals with merged_items list,
#     a sources list, and dedup_count >= 0.
#   - The same keyword across two sources is deduplicated to one item with the
#     highest weight, dedup_count >= 1 — and the surviving row records EVERY
#     source it appeared in, which is what makes corroboration computable later.
#   - A non-finite weight fails closed rather than sorting silently.
#   - The source label is stamped from the bucket, never read from the item.
#   - out_of_scope=True -> early exit returns {"status": SUCCESS} and the
#     synthesized_signals key is ABSENT.
#   - signals missing / not a dict -> status=ERROR.
#
# S-4 audit muted at the node module via an autouse fixture (NEVER stub shared.*).

import pytest

from src.nodes.signal_synthesize_node import SignalSynthesizeNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.signal_synthesize_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestSignalSynthesizeNode:
    """Unit tests for SignalSynthesizeNode (merge + dedup)."""

    def setup_method(self):
        self.node = SignalSynthesizeNode()

    def test_happy_path_merges_three_sources(self):
        """signals with 3 sources -> merged_items populated, dedup_count present."""
        signals = {
            "social": [{"keyword": "wireless earbuds", "weight": 3.0}],
            "search": [{"keyword": "noise cancelling", "weight": 2.0}],
            "sales": [{"keyword": "earbuds case", "weight": 5.0}],
            "competitor": None,
        }
        result = self.node.execute({"signals": signals})

        assert result["status"] == AgentStatus.SUCCESS
        synth = result["synthesized_signals"]
        assert isinstance(synth, dict)
        assert isinstance(synth["merged_items"], list)
        # Three distinct keywords across three contributing sources.
        assert len(synth["merged_items"]) == 3
        assert set(synth["sources"]) == {"social", "search", "sales"}
        assert synth["dedup_count"] >= 0
        # merged_items are sorted descending by weight (sales 5.0 first).
        assert synth["merged_items"][0]["weight"] == 5.0

    def test_dedup_same_keyword_keeps_highest_weight(self):
        """Same keyword in 2 sources -> merged to 1 item with the highest weight."""
        signals = {
            "social": [{"keyword": "smartwatch", "weight": 2.0}],
            "search": [{"keyword": "smartwatch", "weight": 7.0}],
            "sales": None,
            "competitor": None,
        }
        result = self.node.execute({"signals": signals})

        assert result["status"] == AgentStatus.SUCCESS
        synth = result["synthesized_signals"]
        # The duplicate keyword collapses to a single merged item.
        smartwatch_items = [i for i in synth["merged_items"] if i["keyword"].lower() == "smartwatch"]
        assert len(smartwatch_items) == 1
        # The surviving item carries the highest of the two weights.
        assert smartwatch_items[0]["weight"] == 7.0
        assert synth["dedup_count"] >= 1

    def test_dedup_accumulates_the_contributing_sources(self):
        """Deduplication must not destroy the evidence of corroboration.

        Collapsing duplicates by keyword also removed the only record that a
        keyword had appeared in more than one bucket — which is the input the
        ranking step's corroboration boost is computed from, so that boost could
        never fire on any request. The surviving row now carries every source.
        """
        result = self.node.execute(
            {
                "signals": {
                    "social": [{"keyword": "smartwatch", "weight": 2.0}],
                    "search": [{"keyword": "smartwatch", "weight": 7.0}],
                    "sales": [{"keyword": "smartwatch", "weight": 1.0}],
                }
            }
        )

        merged = result["synthesized_signals"]["merged_items"]
        assert len(merged) == 1
        assert merged[0]["weight"] == 7.0
        assert set(merged[0]["sources"]) == {"social", "search", "sales"}

    def test_non_finite_weight_is_refused(self):
        """A NaN weight compares False against every threshold — fail closed."""
        result = self.node.execute({"signals": {"social": [{"keyword": "smartwatch", "weight": float("nan")}]}})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]
        assert "synthesized_signals" not in result

    def test_source_label_comes_from_the_bucket_not_the_item(self):
        """A caller cannot relabel a row into a bucket with a higher multiplier."""
        result = self.node.execute({"signals": {"competitor": [{"keyword": "price_drop", "source": "sales"}]}})

        merged = result["synthesized_signals"]["merged_items"]
        assert merged[0]["source"] == "competitor"

    def test_out_of_scope_early_exit_no_synthesis(self):
        """out_of_scope=True -> returns SUCCESS only; synthesized_signals absent."""
        result = self.node.execute({"out_of_scope": True, "signals": {"social": []}})

        assert result["status"] == AgentStatus.SUCCESS
        assert "synthesized_signals" not in result

    def test_missing_signals_is_error(self):
        """signals missing / not a dict -> status=ERROR with error_log."""
        result = self.node.execute({})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]

    def test_signals_wrong_type_is_error(self):
        """signals present but not a dict -> status=ERROR."""
        result = self.node.execute({"signals": "not-a-dict"})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]
