# RET-C2-332 — Unit Tests: InputValidateNode
#
# Inner DomainWorkflowGraph node 1: validate the structured multi-source signal
# payload arriving from validated_input (set by the outer pre_process slot),
# detect out-of-scope requests, and emit an S-4 audit trace with competitor
# data masked (S-1).
#
# Behaviour asserted by these tests:
#   - A valid payload (known category + parseable period + >=1 signal source)
#     -> signals dict populated, out_of_scope=False, status=SUCCESS.
#   - An unrecognised category -> out_of_scope=True, status=SUCCESS (NOT ERROR).
#   - No structured payload at all -> out_of_scope=True with a closed-set reason
#     code, still SUCCESS: absent optional caller data is a documented baseline,
#     never a fault.
#   - Every scope reason is a code authored by the node, so a rejected value can
#     never travel back out through that channel.
#   - competitor_activity is MASKED in the S-4 trace (raw payload never emitted).
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

from unittest.mock import MagicMock

import pytest

from src.nodes.input_validate_node import InputValidateNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.input_validate_node.emit_trace_event",
        lambda *a, **k: None,
    )


def _payload(**overrides):
    """A complete, in-scope multi-source signal payload."""
    base = {
        "category": "electronics",
        "period": "4 weeks",
        "social_signals": [{"keyword": "wireless earbuds", "weight": 3.0}],
        "search_trends": [{"keyword": "noise cancelling", "weight": 2.0}],
        "sales_velocity": [{"keyword": "earbuds", "weight": 5.0}],
    }
    base.update(overrides)
    return base


class TestInputValidateNode:
    """Unit tests for InputValidateNode (validation + out-of-scope + S-1 mask)."""

    def setup_method(self):
        self.node = InputValidateNode()

    def test_happy_path_populates_signals(self):
        """Valid multi-source payload -> signals dict populated, status=SUCCESS."""
        result = self.node.execute({"validated_input": _payload()})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is False
        signals = result["signals"]
        assert isinstance(signals, dict)
        # Canonical signal buckets are produced from the raw source keys.
        assert signals["social"] == [{"keyword": "wireless earbuds", "weight": 3.0}]
        assert signals["search"] == [{"keyword": "noise cancelling", "weight": 2.0}]
        assert signals["sales"] == [{"keyword": "earbuds", "weight": 5.0}]
        # validated_input is normalised (category lower-cased, period stripped).
        assert result["validated_input"]["category"] == "electronics"
        assert result["validated_input"]["period"] == "4 weeks"

    def test_unknown_category_is_out_of_scope_success(self):
        """Unrecognised category -> out_of_scope=True, status=SUCCESS (not ERROR)."""
        result = self.node.execute({"validated_input": _payload(category="UNKNOWN_CATEGORY_XYZ")})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True

    def test_missing_category_is_out_of_scope_success(self):
        """A payload missing 'category' (but with signals/period) is out-of-scope."""
        payload = _payload()
        del payload["category"]
        result = self.node.execute({"validated_input": payload})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True

    def test_no_payload_is_out_of_scope_not_an_error(self):
        """No structured payload -> the documented baseline, not a fault.

        Absent optional caller data must never be reported as a failure, or a
        caller cannot tell "I have nothing to work from" apart from "something
        broke". The reason is a closed-set code and no output is produced.
        """
        result = self.node.execute({})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        assert result["scope_reasons"] == ["no_structured_payload"]
        assert result["signals"] == {}

    def test_no_required_keys_and_no_signals_is_out_of_scope(self):
        """A payload with neither category/period nor any signal is out of scope."""
        result = self.node.execute({"validated_input": {"unrelated": "x"}})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        assert set(result["scope_reasons"]) == {
            "category_missing",
            "period_missing",
            "no_signal_sources",
        }

    def test_scope_reasons_never_carry_caller_text(self):
        """A rejected value is never reflected back through the scope channel."""
        marker = "UNKNOWN_CATEGORY_XYZ"
        result = self.node.execute({"validated_input": _payload(category=marker)})

        assert result["out_of_scope"] is True
        assert result["scope_reasons"] == ["category_not_recognised"]
        assert marker not in repr(result["scope_reasons"])

    def test_competitor_activity_masked_in_audit_trace(self):
        """S-1: raw competitor_activity is NEVER emitted in the audit trace."""
        spy = MagicMock()
        # Re-patch the module-level emit_trace_event with a spy for this test.
        import src.nodes.input_validate_node as mod

        original = mod.emit_trace_event
        mod.emit_trace_event = spy
        try:
            secret = [{"name": "Rival Brand X", "share": 0.42}]
            payload = _payload(competitor_activity=secret)
            result = self.node.execute({"validated_input": payload})
        finally:
            mod.emit_trace_event = original

        assert result["status"] == AgentStatus.SUCCESS
        assert spy.called
        # Serialise EVERY trace call's args/kwargs and assert the raw competitor
        # identifier text never appears in any emitted payload.
        # S-1: the raw competitor identifier must never appear in any emitted
        # PAYLOAD (2nd positional arg = the audit event data the logger persists).
        # The 3rd arg is the working-state context, which legitimately still
        # carries competitor_activity for downstream nodes (see
        # test_competitor_still_carried_in_signals_dict) and is not serialised
        # into the audit record.
        emitted_payloads = repr([c.args[1] for c in spy.call_args_list if len(c.args) > 1])
        assert "Rival Brand X" not in emitted_payloads

    def test_competitor_still_carried_in_signals_dict(self):
        """The competitor bucket is carried in-state (masking happens downstream)."""
        secret = [{"category": "electronics", "intent": "price_drop"}]
        result = self.node.execute({"validated_input": _payload(competitor_activity=secret)})

        assert result["status"] == AgentStatus.SUCCESS
        # InputValidateNode maps competitor_activity -> signals["competitor"].
        assert result["signals"]["competitor"] == secret
