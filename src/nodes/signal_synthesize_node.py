"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state, config=None) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-332 — SignalSynthesizeNode
# Second node of the inner domain workflow: merge and deduplicate the source
# buckets (social, search, sales, competitor) into one synthesized_signals dict.
# Purely deterministic — no model is invoked anywhere in this pipeline.
#
# Items arriving here have already passed the request contract, so their keyword
# is inside the permitted render class and their weight is a finite, bounded
# float. This node re-asserts both rather than assuming them: it is the only way
# a direct unit call — or a future caller of this node from somewhere else — gets
# the same guarantee the pipeline gives.
#
# The source label is stamped from the BUCKET, never read from the item. A label
# taken from caller data would let a request relabel a competitor row as a sales
# one and collect the higher source multiplier downstream.

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.request_contract import ContractError, finite_in_range, safe_keyword

logger = logging.getLogger(__name__)

# Canonical bucket keys, in the order they are merged.
_SOURCE_ORDER = ("social", "search", "sales", "competitor")


def _canonical_item(raw: Any, source: str) -> Optional[Dict[str, Any]]:
    """Re-assert the item contract and stamp the source, or return None.

    None means "not usable as a signal" — a gap in the caller's data. A value
    that violates the contract raises instead, because a violation is a
    rejection the caller must be told about, not a row to skip quietly.
    """
    if not isinstance(raw, dict):
        return None
    keyword_value = raw.get("keyword")
    if keyword_value is None:
        return None
    keyword = safe_keyword(keyword_value, f"signals.{source}[].keyword")
    weight = finite_in_range(raw.get("weight", 1.0), f"signals.{source}[].weight")
    metadata = raw.get("metadata")
    return {
        "source": source,
        "keyword": keyword,
        "weight": weight,
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


class SignalSynthesizeNode(FunctionNode):
    """Merge and deduplicate multi-source signals into ``synthesized_signals``.

    Reads ``state["signals"]`` (canonical buckets produced by InputValidateNode
    with keys ``social``, ``search``, ``sales``, ``competitor``) and writes a
    ``synthesized_signals`` dict structured as::

        {
            "sources":      list[str],   # source types that contributed items
            "merged_items": list[dict],  # deduplicated, weight-descending items
            "dedup_count":  int,         # number of duplicate keywords removed
        }

    Each entry in ``merged_items`` follows the canonical schema::

        {"source": str, "sources": list[str], "keyword": str,
         "weight": float, "metadata": dict}

    ``source`` is the bucket the surviving (heaviest) occurrence came from;
    ``sources`` lists every bucket the keyword appeared in, which is what makes
    cross-source corroboration computable downstream.

    Competitor identity never reaches this node: the request contract has already
    reduced a competitor row to its category-level intent, so ``merged_items``
    and the audit trail carry intent signals only.

    Input state keys:
        signals:      dict — canonical source buckets (required)
        out_of_scope: bool — early-exit guard (optional, default False)

    Output state keys (partial dict):
        synthesized_signals: dict
        status:              AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:           list[str] (on ERROR only)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # Early exit: out-of-scope guard (no processing, no state mutation).
        if state.get("out_of_scope"):
            emit_trace_event(
                "signals_synthesized",
                {"skipped": True, "reason": "out_of_scope"},
                state,
            )
            return {"status": AgentStatus.SUCCESS}

        signals = state.get("signals")

        if not isinstance(signals, dict):
            emit_trace_event("signals_rejected", {"field": "signals", "reason": "not_a_mapping"}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": [
                    "SignalSynthesizeNode: 'signals' is missing or not a mapping; "
                    "the input validation step must run first"
                ],
            }

        # ------------------------------------------------------------------
        # Step 1: collect items from each bucket, stamping the source label
        # ------------------------------------------------------------------
        all_items: List[Dict[str, Any]] = []
        contributing_sources: List[str] = []

        for source in _SOURCE_ORDER:
            bucket = signals.get(source)
            if not bucket:
                continue
            if not isinstance(bucket, list):
                bucket = [bucket]
            try:
                valid = [item for item in (_canonical_item(raw, source) for raw in bucket) if item is not None]
            except ContractError as exc:
                # Names the field, never the value.
                emit_trace_event("signals_rejected", {"field": exc.field, "reason": exc.reason}, state)
                return {
                    "status": AgentStatus.ERROR,
                    "error_log": [f"SignalSynthesizeNode: {exc.field} {exc.reason}"],
                }
            if valid:
                all_items.extend(valid)
                contributing_sources.append(source)

        # ------------------------------------------------------------------
        # Step 2: deduplicate by keyword (case-insensitive); keep highest weight
        # ------------------------------------------------------------------
        # Deduplication keeps one row per keyword but ACCUMULATES the buckets that
        # contributed it. Dropping the duplicate rows outright also destroyed the
        # only evidence that a keyword was corroborated across sources, which is
        # the signal the ranking step's corroboration boost is computed from — so
        # that boost could never fire, on any input. Merging the source list
        # instead keeps the deduplication and makes corroboration observable.
        seen: Dict[str, Dict[str, Any]] = {}
        dedup_count = 0

        for item in all_items:
            key = item["keyword"].lower()
            existing = seen.get(key)
            if existing is None:
                item["sources"] = [item["source"]]
                seen[key] = item
                continue
            dedup_count += 1
            if item["source"] not in existing["sources"]:
                existing["sources"].append(item["source"])
            if item["weight"] > existing["weight"]:
                # The heaviest occurrence supplies the weight, the primary source
                # label and the metadata; the accumulated source list survives.
                merged_sources = existing["sources"]
                item["sources"] = merged_sources
                seen[key] = item

        # ------------------------------------------------------------------
        # Step 3: sort merged items descending by weight
        # ------------------------------------------------------------------
        merged_items: List[Dict[str, Any]] = sorted(seen.values(), key=lambda x: x["weight"], reverse=True)

        synthesized_signals = {
            "sources": contributing_sources,
            "merged_items": merged_items,
            "dedup_count": dedup_count,
        }

        logger.info(
            "SignalSynthesizeNode: sources=%s merged=%d dedup_count=%d",
            contributing_sources,
            len(merged_items),
            dedup_count,
        )

        # Audit trace — counts and source labels only. Keyword text is derived
        # from caller data and is deliberately excluded from the audit channel.
        emit_trace_event(
            "signals_synthesized",
            {
                "source_count": len(contributing_sources),
                "merged_item_count": len(merged_items),
                "dedup_count": dedup_count,
                "sources": contributing_sources,
            },
            state,
        )

        return {
            "synthesized_signals": synthesized_signals,
            "status": AgentStatus.SUCCESS,
        }
