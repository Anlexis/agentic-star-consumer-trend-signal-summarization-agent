# RET-C2-332 — Unit Tests: OutputValidateNode
#
# Outer post-process / inner output_validate slot: the S-3 mandatory output
# security gate for consumer trend summaries. Validates trend_summary for
# presence, the mandatory advisory DISCLAIMER, no raw competitor names, and a
# non-empty top_trends list. On any violation the domain gate raises
# SecurityViolationError, which execute() catches and converts to ERROR.
#
# Behaviour asserted by these tests:
#   - A valid trend_summary with DISCLAIMER -> validated_output set, SUCCESS.
#   - DISCLAIMER missing -> execute() returns status=ERROR (the framework catches
#     SecurityViolationError); _run_s3_domain_gate() raises directly.
#   - out_of_scope=True -> validated_output is the scope-exceeded stub, SUCCESS.
#   - Empty / None trend_summary -> status=ERROR.
#   - A violation CLEARS every output-bearing field, not merely labels the
#     status: the framework falls back to the result field even on an error, so
#     withholding has to be done rather than declared.
#   - The blocked identity is never named in the refusal, and a credential shape
#     is refused whether the framework detector or the local floor catches it.
#
# The S-3 gate method under test is _run_s3_domain_gate (the current domain-gate
# method name on OutputValidateNode). SecurityViolationError is imported from the
# SAME path the node uses (framework.errors).
#
# S-4 audit muted at the node module via an autouse fixture (NEVER stub shared.*).

import pytest

from src.nodes.output_validate_node import OutputValidateNode
from framework.schemas.agent_status import AgentStatus
from framework.errors import SecurityViolationError


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.output_validate_node.emit_trace_event",
        lambda *a, **k: None,
    )


def _valid_summary():
    """A complete, gate-passing trend_summary (DISCLAIMER + non-empty top_trends)."""
    return {
        "category": "electronics",
        "period": "4 weeks",
        "headline": "Top trend in electronics: 'earbuds' is leading consumer signals.",
        "top_trends": [
            {"rank": 1, "keyword": "earbuds", "score": 5.5, "sources": ["sales"]},
        ],
        "competitor_summary": (
            "Competitor activity in the electronics category shows alignment with "
            "observed consumer trend signals. Individual competitor data is "
            "withheld per S-1 privacy guidelines."
        ),
        "demand_forecast": {
            "outlook": "accelerating",
            "four_week_signal": "Consumer interest in 'earbuds' is accelerating.",
            "DISCLAIMER": (
                "This demand forecast is advisory only and does not constitute a " "guarantee of future performance."
            ),
        },
        "recommended_actions": ["Increase stock levels for trending categories."],
    }


class TestOutputValidateNode:
    """Unit tests for OutputValidateNode (S-3 output gate)."""

    def setup_method(self):
        self.node = OutputValidateNode()

    def test_happy_path_sets_validated_output(self):
        """Valid trend_summary with DISCLAIMER -> validated_output set, SUCCESS."""
        result = self.node.execute({"trend_summary": _valid_summary()})

        assert result["status"] == AgentStatus.SUCCESS
        # On the happy path validated_output IS the gated trend_summary.
        assert result["validated_output"]["demand_forecast"]["DISCLAIMER"]
        assert result["validated_output"]["top_trends"]

    def test_disclaimer_missing_execute_returns_error(self):
        """DISCLAIMER missing -> execute() catches SecurityViolationError -> ERROR."""
        summary = _valid_summary()
        del summary["demand_forecast"]["DISCLAIMER"]
        result = self.node.execute({"trend_summary": summary})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]

    def test_disclaimer_missing_gate_method_raises(self):
        """The S-3 domain gate method raises directly on a missing DISCLAIMER."""
        summary = _valid_summary()
        del summary["demand_forecast"]["DISCLAIMER"]

        with pytest.raises(SecurityViolationError):
            self.node._run_s3_domain_gate(summary)

    def test_clean_summary_passes_gate_method(self):
        """A valid trend_summary passes _run_s3_domain_gate without raising."""
        assert self.node._run_s3_domain_gate(_valid_summary()) is None

    def test_out_of_scope_passthrough_success(self):
        """out_of_scope=True -> validated_output is the scope stub, SUCCESS."""
        result = self.node.execute({"out_of_scope": True})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["validated_output"]["out_of_scope"] is True

    def test_empty_trend_summary_is_error(self):
        """Empty / None trend_summary -> status=ERROR (gate rejects)."""
        result = self.node.execute({"trend_summary": {}})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]

    def test_violation_clears_every_output_bearing_field(self):
        """A refusal must WITHHOLD, not merely label.

        Asserted here at the node rather than only end-to-end, because the outer
        layers of this template happen to contain an inner error on their own —
        so an end-to-end assertion alone would pass whether or not this node
        cleared anything, and would tell us nothing about this node.
        """
        summary = _valid_summary()
        del summary["demand_forecast"]["DISCLAIMER"]
        result = self.node.execute({"trend_summary": summary, "result": summary})

        assert result["status"] == AgentStatus.ERROR
        for field in ("validated_output", "trend_summary", "result", "formatted_output"):
            assert field in result, f"{field} must be cleared explicitly, not merely omitted"
            assert result[field] is None

    def test_a_blocked_competitor_identity_is_never_named_in_the_refusal(self):
        """Blocking a value must not be the act that publishes it.

        The reason travels as a closed-set code, so the identity the gate exists
        to withhold stays out of error_log and out of the audit event.
        """
        summary = _valid_summary()
        summary["headline"] = "competitor_a is leading consumer signals."
        result = self.node.execute({"trend_summary": summary})

        assert result["status"] == AgentStatus.ERROR
        serialised = repr(result)
        assert "competitor_a" not in serialised
        assert "competitor_identity_present" in serialised

    def test_credential_shape_in_the_summary_is_refused(self):
        """The local pattern set is a floor on top of the framework detector.

        Both directions are covered: a framework-recognised shape and an
        assignment form the framework's format-based patterns do not match.
        """
        for leaked in (
            "Bearer abc123def456ghi789jkl012",
            "password=hunter2shouldnotship",
        ):
            summary = _valid_summary()
            summary["headline"] = f"Trend note {leaked}"
            result = self.node.execute({"trend_summary": summary})

            assert result["status"] == AgentStatus.ERROR, leaked
            assert result["validated_output"] is None
            assert leaked not in repr(result)
