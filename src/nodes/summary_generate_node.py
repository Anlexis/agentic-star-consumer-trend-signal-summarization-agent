"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state, config) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-332 — SummaryGenerateNode
# Slot: fourth node in DomainWorkflowGraph (summary generation step)
#
# Execution paths:
#   (a) Early exit: state["out_of_scope"] == True
#       -> return SUCCESS immediately (no work done)
#   (b) Error: ranked_trends missing or empty
#       -> return ERROR with error_log
#   (c) Normal: generate structured trend_summary dict from ranked_trends
#       -> deterministic template-based synthesis (no LLM in v1)
#       -> demand_forecast.DISCLAIMER MUST be present (S-3 validated by OutputValidateNode)
#
# S-4 audit: emit_trace_event("summary_generated", {...}, state) after synthesis.
# Production wires the real LLM here via config["configurable"].

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.request_contract import ContractError, finite_in_range, safe_keyword

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# S-3 DISCLAIMER — exact advisory text required by OutputValidateNode
# ---------------------------------------------------------------------------
_DEMAND_FORECAST_DISCLAIMER: str = (
    "This demand forecast is advisory only and does not"
    " constitute a guarantee of future performance."
    " Retail decisions should be validated against"
    " actual sales data and market conditions."
)

# Recognised SNS platforms, mapped from the lower-cased form a caller may send
# to the spelling this agent renders. The rendered name always comes from this
# table, never from the request.
_JAPAN_SNS_PLATFORMS: Dict[str, str] = {
    "x": "X",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "line": "LINE",
}

# Composite score thresholds for demand outlook classification
_OUTLOOK_ACCELERATING_THRESHOLD = 0.7
_OUTLOOK_STABLE_THRESHOLD = 0.4


def _classify_outlook(top_score: float) -> str:
    """Classify demand outlook from the top trend's composite score."""
    if top_score >= _OUTLOOK_ACCELERATING_THRESHOLD:
        return "accelerating"
    if top_score >= _OUTLOOK_STABLE_THRESHOLD:
        return "stable"
    return "decelerating"


def _build_four_week_signal(outlook: str, top_keyword: str) -> str:
    """Deterministic 1-2 sentence four-week signal string."""
    if outlook == "accelerating":
        return (
            f"Consumer interest in '{top_keyword}' is accelerating." " Expect elevated demand over the next four weeks."
        )
    if outlook == "stable":
        return (
            f"Demand signals for '{top_keyword}' remain stable."
            " No significant surge or decline is anticipated over the next four weeks."
        )
    return (
        f"Consumer interest in '{top_keyword}' shows signs of deceleration."
        " Monitor closely and consider inventory adjustment over the next four weeks."
    )


def _extract_japan_specifics(signals: Any) -> Dict[str, Any]:
    """Summarise the Japan-specific breakdown from the canonical signal buckets.

    Both outputs are built from the agent's OWN vocabulary rather than from
    caller text: platform names are only counted when they match the recognised
    set, and the name written into the breakdown is the recognised spelling, not
    the caller's. An unrecognised platform is counted nowhere and named nowhere.
    """
    japan_specifics: Dict[str, Any] = {
        "sns_platform_breakdown": {},
        "offline_signals": [],
    }

    if not isinstance(signals, dict):
        return japan_specifics

    platform_counts: Dict[str, int] = {}
    for item in signals.get("social") or []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        platform = metadata.get("platform")
        if not isinstance(platform, str):
            continue
        canonical = _JAPAN_SNS_PLATFORMS.get(platform.strip().lower())
        if canonical:
            platform_counts[canonical] = platform_counts.get(canonical, 0) + 1
    if platform_counts:
        japan_specifics["sns_platform_breakdown"] = platform_counts

    offline: List[str] = []
    for item in signals.get("sales") or []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        channel = metadata.get("channel")
        keyword = item.get("keyword")
        if isinstance(channel, str) and channel.strip().lower() == "offline" and isinstance(keyword, str):
            offline.append(keyword)
    japan_specifics["offline_signals"] = offline

    return japan_specifics


def _build_regulatory_flags(validated_input: Any) -> List[str]:
    """Return the consumer-affairs watch items carried on the validated request.

    The request contract holds these to the same character class as a keyword,
    so what is returned here is already safe to render.
    """
    if not isinstance(validated_input, dict):
        return []

    regulatory = validated_input.get("regulatory")
    if not isinstance(regulatory, dict):
        return []

    watch_items = regulatory.get("consumer_affairs_agency_flags")
    if isinstance(watch_items, list):
        return [item for item in watch_items if isinstance(item, str) and item]
    return []


def _build_recommended_actions(outlook: str, top_trends: List[Dict[str, Any]]) -> List[str]:
    """Generate deterministic recommended actions from outlook and top trends."""
    actions: List[str] = []
    top_keywords = [t.get("keyword", "") for t in top_trends[:3] if t.get("keyword")]

    if outlook == "accelerating":
        actions.append("Increase stock levels for trending categories to meet rising demand.")
        if top_keywords:
            actions.append(f"Prioritise promotional campaigns for: {', '.join(top_keywords)}.")
        actions.append("Monitor social channels for further signal acceleration.")
    elif outlook == "stable":
        actions.append("Maintain current inventory levels; no urgent replenishment needed.")
        if top_keywords:
            actions.append(f"Continue standard campaigns for: {', '.join(top_keywords)}.")
        actions.append("Review trend signals weekly for early signs of change.")
    else:
        actions.append("Consider reducing purchase orders for decelerating trend categories.")
        if top_keywords:
            actions.append(f"Evaluate markdown or clearance options for: {', '.join(top_keywords)}.")
        actions.append("Reassess product mix and watch for emerging replacement trends.")

    return actions


class SummaryGenerateNode(FunctionNode):
    """Generate a structured trend summary report dict from ranked_trends.

    Slot: fourth node of DomainWorkflowGraph (RET-C2-332).

    Deterministic template-based synthesis — no LLM in v1.
    Production wires the real LLM here via config["configurable"].

    Execution paths:

    (a) Early exit (``state["out_of_scope"] == True``):
        Returns ``{"status": AgentStatus.SUCCESS}`` immediately.

    (b) Error (``ranked_trends`` missing or empty):
        Returns ``{"status": AgentStatus.ERROR, "error_log": [...]}}``.

    (c) Normal:
        Builds ``trend_summary`` dict from ``ranked_trends``, ``signals``,
        and ``validated_input``.  Demand forecast section includes the S-3
        DISCLAIMER string (validated by OutputValidateNode).

    Input state keys:
        ranked_trends:   list[dict] from TrendRankNode
                         — keys: rank, keyword, composite_score, sources, metadata
        signals:         dict — original signals (SNS platforms, 消費者庁 items)
        validated_input: dict — category and period metadata
        out_of_scope:    bool — early-exit guard

    Output state keys (partial dict):
        trend_summary:   structured dict (see schema in issue spec)
        status:          AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:       list[str] — populated only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # ------------------------------------------------------------------
        # Path (a): Early exit — out-of-scope guard
        # ------------------------------------------------------------------
        if state.get("out_of_scope"):
            logger.info("SummaryGenerateNode: out_of_scope=True — early exit")
            emit_trace_event("summary_generated", {"skipped": True, "reason": "out_of_scope"}, state)
            return {"status": AgentStatus.SUCCESS}

        # ------------------------------------------------------------------
        # Path (b): Error — ranked_trends missing or empty
        # ------------------------------------------------------------------
        ranked_trends: Any = state.get("ranked_trends")
        if not ranked_trends or not isinstance(ranked_trends, list):
            err = "SummaryGenerateNode: ranked_trends is missing or empty" " — cannot generate trend summary"
            logger.error(err)
            emit_trace_event("summary_rejected", {"field": "ranked_trends", "reason": "missing_or_empty"}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": [err],
            }

        # ------------------------------------------------------------------
        # Path (c): Normal — deterministic template synthesis
        # Production wires the real LLM here via config["configurable"].
        # ------------------------------------------------------------------
        # --- Extract metadata from validated_input ---
        validated_input: Any = state.get("validated_input", {})
        if not isinstance(validated_input, dict):
            validated_input = {}

        category: str = str(validated_input.get("category", "general"))
        period: str = str(validated_input.get("period", ""))
        generated_at: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # --- Top 5 trends ---
        top_trends: List[Dict[str, Any]] = []
        for trend in ranked_trends[:5]:
            if not isinstance(trend, dict):
                continue
            top_trends.append(
                {
                    "rank": trend.get("rank"),
                    "keyword": trend.get("keyword", ""),
                    "score": trend.get("composite_score"),
                    "sources": trend.get("sources", []),
                }
            )

        # --- Headline from top trend ---
        # Both values are re-parsed through the request contract rather than
        # trusted from state: the outlook classification below is a threshold
        # comparison, and a non-finite score compares False against every
        # threshold, which would quietly report "decelerating" for any input.
        top_trend = ranked_trends[0] if isinstance(ranked_trends[0], dict) else {}
        try:
            raw_keyword = top_trend.get("keyword")
            top_keyword: str = safe_keyword(raw_keyword, "ranked_trends[0].keyword") if raw_keyword else ""
            top_score: float = finite_in_range(
                top_trend.get("composite_score", 0.0), "ranked_trends[0].composite_score"
            )
        except ContractError as exc:
            emit_trace_event("summary_rejected", {"field": exc.field, "reason": exc.reason}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": [f"SummaryGenerateNode: {exc.field} {exc.reason}"],
            }

        if top_keyword:
            headline: str = f"Top trend in {category}: '{top_keyword}' is leading consumer signals."
        else:
            headline = f"Trend analysis complete for {category}."

        # --- Japan specifics ---
        signals: Any = state.get("signals", {})
        japan_specifics: Dict[str, Any] = _extract_japan_specifics(signals)

        # --- Competitor summary (category-level only — S-1: no raw names) ---
        competitor_summary: str = (
            f"Competitor activity in the {category} category shows alignment"
            " with observed consumer trend signals."
            " Individual competitor data is withheld per S-1 privacy guidelines."
        )

        # --- Demand forecast ---
        outlook: str = _classify_outlook(top_score)
        four_week_signal: str = _build_four_week_signal(outlook, top_keyword)

        demand_forecast: Dict[str, str] = {
            "outlook": outlook,
            "four_week_signal": four_week_signal,
            "DISCLAIMER": _DEMAND_FORECAST_DISCLAIMER,
        }

        # --- Regulatory flags ---
        regulatory_flags: List[str] = _build_regulatory_flags(validated_input)

        # --- Recommended actions ---
        recommended_actions: List[str] = _build_recommended_actions(outlook, top_trends)

        # --- Assemble trend_summary ---
        trend_summary: Dict[str, Any] = {
            "category": category,
            "period": period,
            "generated_at": generated_at,
            "headline": headline,
            "top_trends": top_trends,
            "japan_specifics": japan_specifics,
            "competitor_summary": competitor_summary,
            "demand_forecast": demand_forecast,
            "regulatory_flags": regulatory_flags,
            "recommended_actions": recommended_actions,
        }

        logger.info(
            "SummaryGenerateNode: generated trend_summary — category=%s outlook=%s trends=%d",
            category,
            outlook,
            len(top_trends),
        )

        # Audit trace. `category` and `outlook` are closed-set values authored by
        # this agent; the keyword and the period are caller-derived and stay out.
        emit_trace_event(
            "summary_generated",
            {
                "category": category,
                "outlook": outlook,
                "trend_count": len(top_trends),
                "disclaimer_present": "DISCLAIMER" in demand_forecast,
            },
            state,
        )

        return {
            "trend_summary": trend_summary,
            "status": AgentStatus.SUCCESS,
        }
