"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state, config=None) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-332 — TrendRankNode
# Inner domain graph node: score and rank synthesized trend signals to produce
# an ordered ranked_trends list. Deterministic composite scoring — no LLM in v1.
#
# Input:  state["synthesized_signals"] — dict from SignalSynthesizeNode;
#             key "merged_items": list of {"source", "keyword", "weight", "metadata"}
#         state["out_of_scope"] — bool early-exit guard
# Output: state["ranked_trends"] — list of top-N items sorted by composite score desc
#             each item: {"rank", "keyword", "composite_score", "sources", "metadata"}

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.request_contract import ContractError, finite_in_range, safe_keyword

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source multipliers (deterministic v1 ranking)
# ---------------------------------------------------------------------------

_SOURCE_MULTIPLIERS: Dict[str, float] = {
    "social": 1.0,
    "search": 0.9,
    "sales": 1.1,  # sales velocity weighted highest
    "competitor": 0.7,
}
_DEFAULT_MULTIPLIER = 1.0

# Corroboration boost awarded when a keyword appears in multiple sources.
_CORROBORATION_BOOST = 0.2

# Return top-N items; hard cap at 20.
_TOP_N = 10
_MAX_CAP = 20


def _source_multiplier(source: str) -> float:
    """Return the multiplier for the given source label (case-insensitive)."""
    return _SOURCE_MULTIPLIERS.get(source.lower(), _DEFAULT_MULTIPLIER)


def _compute_ranked_trends(merged_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Score each merged_item and return them sorted by composite score descending.

    Algorithm per issue spec:
        composite_score = weight * source_multiplier + corroboration_boost
    where corroboration_boost = 0.2 if the keyword appears in > 1 source.

    Multiple rows with the same keyword are collapsed: the first occurrence's
    weight and metadata are used; all sources are merged into the sources list
    and corroboration is computed from that merged list.
    """
    # Collapse items by keyword so we can detect cross-source corroboration.
    keyword_map: Dict[str, Dict[str, Any]] = {}

    for index, item in enumerate(merged_items):
        if not isinstance(item, dict):
            raise ContractError(f"merged_items[{index}]", "must be an object")
        keyword_value = item.get("keyword")
        if keyword_value is None:
            continue
        keyword = safe_keyword(keyword_value, f"merged_items[{index}].keyword")
        # The synthesis step records every bucket a keyword appeared in; the
        # single `source` label is the fallback for a row that has not been
        # through that step (a direct call to this node, for instance).
        item_sources = item.get("sources")
        if not isinstance(item_sources, list) or not item_sources:
            item_sources = [str(item.get("source", "unknown"))]
        # Every arithmetic input is re-parsed as finite and bounded here. A NaN
        # weight would pass float() and then compare False against every
        # threshold and every sort comparison, which silently produces a ranking
        # that looks ordinary and is not — the one failure mode this node cannot
        # be allowed to have.
        weight = finite_in_range(item.get("weight", 0.0), f"merged_items[{index}].weight")
        metadata = item.get("metadata") or {}

        normalised_sources = [str(s) for s in item_sources]
        if keyword not in keyword_map:
            keyword_map[keyword] = {
                "keyword": keyword,
                "weight": weight,
                "sources": list(dict.fromkeys(normalised_sources)),
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        else:
            # Additional row: accumulate sources; keep first row's weight/metadata.
            for extra in normalised_sources:
                if extra not in keyword_map[keyword]["sources"]:
                    keyword_map[keyword]["sources"].append(extra)

    scored: List[Dict[str, Any]] = []
    for entry in keyword_map.values():
        keyword = entry["keyword"]
        weight = entry["weight"]
        sources = entry["sources"]
        metadata = entry["metadata"]

        # Use the multiplier of the first (primary) source.
        multiplier = _source_multiplier(sources[0])

        # Corroboration boost when the keyword appears in more than one source.
        corroboration = _CORROBORATION_BOOST if len(sources) > 1 else 0.0

        composite_score = weight * multiplier + corroboration

        scored.append(
            {
                "keyword": keyword,
                "composite_score": round(composite_score, 6),
                "sources": sources,
                "metadata": metadata,
            }
        )

    # Sort descending by composite score.
    scored.sort(key=lambda x: x["composite_score"], reverse=True)

    # Cap at _MAX_CAP then take top _TOP_N.
    capped = scored[:_MAX_CAP]
    top = capped[:_TOP_N]

    # Assign 1-indexed rank.
    ranked = []
    for i, item in enumerate(top, start=1):
        ranked.append(
            {
                "rank": i,
                "keyword": item["keyword"],
                "composite_score": item["composite_score"],
                "sources": item["sources"],
                "metadata": item["metadata"],
            }
        )

    return ranked


class TrendRankNode(FunctionNode):
    """Score and rank synthesized trend signals for RET-C2-332.

    Deterministic composite-score ranking (no LLM in v1).
    Reads synthesized_signals["merged_items"] produced by SignalSynthesizeNode
    and writes ranked_trends — a top-10 ordered list of trend items.

    Input state keys:
        synthesized_signals: dict with key "merged_items" — list of
            {"source", "keyword", "weight", "metadata"} dicts.
        out_of_scope: bool — if True, node returns SUCCESS immediately
            without mutating ranked_trends.

    Output state keys (partial dict):
        ranked_trends: list of dicts with keys:
            - rank            (int):   1-indexed position
            - keyword         (str):   trend keyword
            - composite_score (float): computed score
            - sources         (list):  sources this keyword appeared in
            - metadata        (dict):  merged metadata from input
        status: AgentStatus.SUCCESS on success, AgentStatus.ERROR on failure
        error_log: list of error messages (only present on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # Out-of-scope guard: nothing to rank, and nothing to change.
        if state.get("out_of_scope"):
            emit_trace_event("trends_ranked", {"skipped": True, "reason": "out_of_scope"}, state)
            return {"status": AgentStatus.SUCCESS}

        # Validate synthesized_signals is present and is a dict.
        synthesized_signals = state.get("synthesized_signals")
        if synthesized_signals is None:
            return {
                "status": AgentStatus.ERROR,
                "error_log": ["TrendRankNode: 'synthesized_signals' is missing from state"],
            }
        if not isinstance(synthesized_signals, dict):
            return {
                "status": AgentStatus.ERROR,
                "error_log": ["TrendRankNode: 'synthesized_signals' must be a mapping"],
            }

        # Extract merged_items; tolerate missing key (treat as empty list).
        merged_items = synthesized_signals.get("merged_items")
        if merged_items is None:
            logger.info("TrendRankNode: 'merged_items' not in synthesized_signals — " "returning empty ranked_trends")
            merged_items = []

        if not isinstance(merged_items, list):
            return {
                "status": AgentStatus.ERROR,
                "error_log": ["TrendRankNode: synthesized_signals['merged_items'] must be a list"],
            }

        # Compute composite-scored ranked list. A contract violation names the
        # field and nothing else: the offending value is caller data, so it must
        # not travel out through the error channel.
        try:
            ranked_trends = _compute_ranked_trends(merged_items)
        except ContractError as exc:
            emit_trace_event("ranking_rejected", {"field": exc.field, "reason": exc.reason}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": [f"TrendRankNode: {exc.field} {exc.reason}"],
            }

        logger.info(
            "TrendRankNode: ranked %d trends from %d merged_items",
            len(ranked_trends),
            len(merged_items),
        )

        # Audit trace, emitted after the ranking is complete. Scores and counts
        # only: a keyword is caller-supplied text, and the audit log is not a
        # channel caller text should travel on.
        emit_trace_event(
            "trends_ranked",
            {
                "total_ranked": len(ranked_trends),
                "top3_scores": [item["composite_score"] for item in ranked_trends[:3]],
            },
            state,
        )

        return {
            "ranked_trends": ranked_trends,
            "status": AgentStatus.SUCCESS,
        }
