"""AgentCore Platform v1.0"""

# RET-C2-332 — the caller-data contract.
#
# Everything a caller can put into a trend request passes through this module
# before any node acts on it. It is deliberately a single place, so the bounds
# the agent enforces can be read (and tested) as one contract rather than
# rediscovered node by node.
#
# Four rules, applied to every caller field:
#
#   1. FINITE AND BOUNDED — every number is parsed by _finite_in_range().
#      float("NaN") and float("Infinity") both survive a plain float() call, and
#      every comparison against NaN is False, so an unchecked NaN weight would
#      sort and threshold silently instead of failing. Rejection is the only safe
#      outcome, so the parser fails CLOSED and names the field it rejected.
#
#   2. INERT WHERE IT RENDERS — a keyword reaches the caller-visible summary
#      verbatim, so it is held to an explicit character class. Anything outside
#      that class is rejected rather than escaped: the class is the contract.
#
#   3. SCREENED FOR INSTRUCTION SHAPES — the payload is walked depth-first,
#      KEYS INCLUDED, and each string is checked twice: once as received, and
#      once with markup removed. The first pass catches chat-template control
#      tokens; the second catches a directive that was split by inserted markup
#      and only reassembles after stripping. Screening only one of the two forms
#      is how a stripper turns a detectable attack into undetectable plain text.
#
#   4. NEVER ECHOED — a rejection names the field, never the value. Rejected
#      input is caller-controlled text and must not be reflected into an error
#      message, an audit event or a log line.
#
# The character class is wider than a bare identifier pattern would be, and that
# is a deliberate, documented decision: consumer trend keywords ARE the domain
# payload, they are frequently Japanese, and an ASCII-identifier lock would make
# the agent unable to do the job it exists for. The class therefore admits word
# characters in any script plus a short list of separators, and excludes every
# character used to structure markup, templates or delimiters.

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from framework.security.credential_detector import detect_credentials_in_value

# ---------------------------------------------------------------------------
# Structural bounds
# ---------------------------------------------------------------------------

#: Maximum signal items accepted per source bucket.
MAX_ITEMS_PER_SOURCE = 200
#: Maximum signal items accepted across all buckets combined.
MAX_ITEMS_TOTAL = 500
#: Maximum metadata keys retained per signal item.
MAX_METADATA_KEYS = 10
#: Maximum length of any caller string anywhere in the payload.
MAX_STRING_LEN = 512
#: Maximum nesting depth walked when screening the payload.
MAX_PAYLOAD_DEPTH = 8
#: Maximum length of a keyword that renders into the summary.
MAX_KEYWORD_LEN = 64
#: Inclusive magnitude bound for every caller-supplied weight.
MAX_WEIGHT = 1_000_000.0

#: Source buckets the caller may populate, in canonical order.
SIGNAL_SOURCE_KEYS: Tuple[str, ...] = (
    "social_signals",
    "search_trends",
    "sales_velocity",
    "competitor_activity",
)

# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------

# Keywords render verbatim into the caller-visible summary. `\w` is Unicode-aware
# in Python, so this admits Latin, kana, kanji and digits; the explicit tail adds
# the separators real product names use. Everything else — angle brackets, pipes,
# square/curly brackets, backslashes, quotes, semicolons, equals — is excluded,
# which is what makes `<|im_start|>`, `[INST]` and `<<SYS>>` structurally
# unrepresentable in a rendered keyword.
_KEYWORD_RE = re.compile(r"^[\w \-.&/()+]{1,%d}$" % MAX_KEYWORD_LEN, re.UNICODE)

# Metadata keys that survive into state. An unrecognised key is dropped, never
# echoed back to the caller under its own name.
_METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# ---------------------------------------------------------------------------
# Instruction-shape screening
# ---------------------------------------------------------------------------

# Chat-template control tokens, screened as a CLASS rather than as literals.
# The token form is the one a phrase-based screen misses: the payload is not a
# recognisable English directive, it is the framing a model uses to separate
# turns, so it reads as ordinary text to any check that looks for verbs.
_CONTROL_TOKEN_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"<\|[^|>]{0,64}\|>"),  # <|im_start|>, <|system|>, ...
    re.compile(r"\[/?INST\]", re.IGNORECASE),  # [INST] / [/INST]
    re.compile(r"<</?SYS>>", re.IGNORECASE),  # <<SYS>> / <</SYS>>
    re.compile(r"<\|?endoftext\|?>", re.IGNORECASE),
    re.compile(r"^\s*(?:system|assistant|user)\s*:", re.IGNORECASE | re.MULTILINE),
)

# Directive phrases, anchored to an instruction shape rather than to bare verbs.
# Anchoring matters in both directions: an unanchored "act as a" matched the
# legitimate phrase "transact as a settlement agent" in a peer template, and a
# screen that refuses real domain text is a fail-CLOSED defect of its own.
_DIRECTIVE_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\b", re.IGNORECASE),
    re.compile(r"\bnew\s+(?:system\s+)?instructions?\s*:", re.IGNORECASE),
    re.compile(r"\boverride\s+(?:all\s+)?(?:system\s+|prior\s+)?(?:prompts?|instructions?|rules?)\b", re.IGNORECASE),
    re.compile(r"\breveal\s+(?:your\s+)?(?:system\s+)?prompt\b", re.IGNORECASE),
)

# Markup removed before the SECOND screening pass. Stripping is not sanitising
# here — the stripped text is screened and then discarded, never forwarded.
_MARKUP_RE = re.compile(r"<[^<>]{0,64}>")


class ContractError(ValueError):
    """Raised when caller data violates the request contract.

    ``field`` names the offending location; the offending VALUE is never carried.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


# ---------------------------------------------------------------------------
# Primitive validators
# ---------------------------------------------------------------------------


def finite_in_range(value: Any, field: str, low: float = -MAX_WEIGHT, high: float = MAX_WEIGHT) -> float:
    """Parse *value* as a finite float within [low, high], or raise ContractError.

    Rejects booleans explicitly: ``isinstance(True, int)`` is True in Python, so
    ``weight: true`` would otherwise arrive as 1.0 and be treated as a real
    measurement.
    """
    if isinstance(value, bool):
        raise ContractError(field, "must be a number, not a boolean")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except (TypeError, ValueError):
            raise ContractError(field, "is not a number") from None
    else:
        raise ContractError(field, "is not a number")
    if not math.isfinite(parsed):
        raise ContractError(field, "must be a finite number")
    if parsed < low or parsed > high:
        raise ContractError(field, f"must be within [{low}, {high}]")
    return parsed


def safe_keyword(value: Any, field: str) -> str:
    """Return *value* as a keyword that is safe to render, or raise ContractError."""
    if not isinstance(value, str):
        raise ContractError(field, "must be a string")
    candidate = value.strip()
    if not candidate:
        raise ContractError(field, "must not be empty")
    if len(candidate) > MAX_KEYWORD_LEN:
        raise ContractError(field, f"must be at most {MAX_KEYWORD_LEN} characters")
    if not _KEYWORD_RE.match(candidate):
        raise ContractError(
            field,
            "contains characters outside the permitted keyword set " "(word characters, space, - . & / ( ) +)",
        )
    return candidate


# ---------------------------------------------------------------------------
# Instruction-shape and credential screens
# ---------------------------------------------------------------------------


def _string_is_hostile(text: str) -> bool:
    """True when *text* carries a control token or an anchored directive.

    Checked twice by design: as received, and with markup removed. A control
    token only exists in the raw form; a directive split by inserted markup
    (``ig<b>nore all previous``) only exists after the strip.
    """
    for candidate in (text, _MARKUP_RE.sub("", text)):
        for pattern in _CONTROL_TOKEN_RES:
            if pattern.search(candidate):
                return True
        for pattern in _DIRECTIVE_RES:
            if pattern.search(candidate):
                return True
    return False


def screen_payload(obj: Any, field: str = "payload", depth: int = 0) -> None:
    """Walk *obj* depth-first — KEYS INCLUDED — and raise on a hostile string.

    Scanning the PARSED structure rather than the raw request body is what makes
    ``\\u``-escaped payloads unable to evade this: by the time the object exists,
    every escape has already been resolved.
    """
    if depth > MAX_PAYLOAD_DEPTH:
        raise ContractError(field, "is nested more deeply than the contract allows")
    if isinstance(obj, str):
        if len(obj) > MAX_STRING_LEN:
            raise ContractError(field, f"exceeds the {MAX_STRING_LEN}-character limit")
        if _string_is_hostile(obj):
            raise ContractError(field, "contains an instruction-shaped sequence")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                if _string_is_hostile(key):
                    # Deliberately positional: a hostile key name is caller data too.
                    raise ContractError(field, "contains an instruction-shaped field name")
                child = f"{field}.{key}" if _METADATA_KEY_RE.match(key) else f"{field}.<field>"
            else:
                child = f"{field}.<field>"
            screen_payload(value, child, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            screen_payload(item, f"{field}[{index}]", depth + 1)
        return
    if isinstance(obj, (bool, int, float)) or obj is None:
        return
    raise ContractError(field, f"has an unsupported type ({type(obj).__name__})")


def credential_fields(context: Any) -> List[str]:
    """Return the names of top-level *context* fields carrying a credential shape.

    Iterating the top level and scanning each field separately is exactly
    equivalent to scanning the whole mapping — ``detect_credentials_in_value`` on
    a dict is defined as the union over its values — so naming the field neither
    widens nor narrows what is blocked. That equivalence is what lets the refusal
    message be actionable without diverging from the framework's own block set.

    The framework detector scans VALUES only, never keys; this function keeps that
    behaviour rather than diverging from the gate it is protecting the caller from.
    """
    if not isinstance(context, dict):
        return ["input_context"] if detect_credentials_in_value(context) else []
    hits: List[str] = []
    for index, (key, value) in enumerate(context.items()):
        if not detect_credentials_in_value(value):
            continue
        # A field NAME is caller data as well: echo it only when it is a plain
        # identifier that trips no credential pattern itself.
        safe_name = (
            isinstance(key, str) and _METADATA_KEY_RE.match(key) is not None and not detect_credentials_in_value(key)
        )
        hits.append(f"input_context.{key}" if safe_name else f"input_context field #{index + 1}")
    return hits


# ---------------------------------------------------------------------------
# The trend-request contract
# ---------------------------------------------------------------------------


def _clean_metadata(raw: Any, field: str) -> Dict[str, Any]:
    """Return a bounded metadata dict; unrecognised keys are DROPPED, not echoed."""
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key, value in raw.items():
        if len(cleaned) >= MAX_METADATA_KEYS:
            break
        if not isinstance(key, str) or not _METADATA_KEY_RE.match(key):
            continue
        if isinstance(value, str):
            if len(value) > MAX_STRING_LEN:
                raise ContractError(f"{field}.{key}", f"exceeds the {MAX_STRING_LEN}-character limit")
            cleaned[key] = value
        elif isinstance(value, bool):
            cleaned[key] = value
        elif isinstance(value, (int, float)):
            cleaned[key] = finite_in_range(value, f"{field}.{key}")
        elif value is None:
            cleaned[key] = None
    return cleaned


def normalise_signal_item(raw: Any, field: str, *, competitor: bool) -> Optional[Dict[str, Any]]:
    """Validate one caller signal item into the canonical shape, or raise.

    Returns None for an item that carries no usable keyword — a gap in the data,
    not an attack, so it is skipped rather than failing the whole request.
    """
    if not isinstance(raw, dict):
        raise ContractError(field, "must be an object")

    if competitor:
        # A competitor row is reduced to its category-level intent. The raw
        # competitor identity is never carried forward, so it cannot reach the
        # summary, the audit trail or the caller.
        candidate = raw.get("category") or raw.get("intent") or raw.get("segment")
        if candidate is None:
            candidate = "competitor_signal"
    else:
        candidate = raw.get("keyword") or raw.get("term") or raw.get("name")
        if candidate is None:
            return None

    keyword = safe_keyword(candidate, f"{field}.keyword")

    raw_weight = raw.get("weight")
    weight_field = f"{field}.weight"
    if raw_weight is None:
        raw_weight = raw.get("score")
        weight_field = f"{field}.score"
    if raw_weight is None:
        raw_weight = raw.get("count")
        weight_field = f"{field}.count"
    weight = 1.0 if raw_weight is None else finite_in_range(raw_weight, weight_field)

    if competitor:
        allowed = {"category", "intent", "segment", "region", "period"}
        metadata_source = {k: v for k, v in raw.items() if k in allowed}
    else:
        metadata_source = {
            k: v for k, v in raw.items() if k not in {"keyword", "term", "name", "weight", "score", "count"}
        }

    # `source` is stamped by the synthesis step from the bucket the item came
    # from — never taken from the item, so a caller cannot relabel a competitor
    # row as a social one and give it the higher source multiplier.
    return {
        "keyword": keyword,
        "weight": weight,
        "metadata": _clean_metadata(metadata_source, f"{field}.metadata"),
    }


def validate_trend_request(raw: Any) -> Dict[str, Any]:
    """Validate a caller trend request and return the normalised payload.

    Raises ContractError — naming the field, never the value — on any violation.
    """
    if not isinstance(raw, dict):
        raise ContractError("trend_request", "must be an object")

    screen_payload(raw, "trend_request")

    payload: Dict[str, Any] = {}

    category = raw.get("category")
    if category is not None:
        if not isinstance(category, str):
            raise ContractError("trend_request.category", "must be a string")
        payload["category"] = category.strip()

    period = raw.get("period")
    if period is not None:
        if not isinstance(period, str):
            raise ContractError("trend_request.period", "must be a string")
        payload["period"] = period.strip()

    total_items = 0
    for source_key in SIGNAL_SOURCE_KEYS:
        bucket = raw.get(source_key)
        if bucket is None:
            continue
        if not isinstance(bucket, list):
            bucket = [bucket]
        if len(bucket) > MAX_ITEMS_PER_SOURCE:
            raise ContractError(
                f"trend_request.{source_key}",
                f"holds more than {MAX_ITEMS_PER_SOURCE} entries",
            )
        total_items += len(bucket)
        if total_items > MAX_ITEMS_TOTAL:
            raise ContractError(
                "trend_request",
                f"holds more than {MAX_ITEMS_TOTAL} signal entries in total",
            )
        cleaned_bucket: List[Dict[str, Any]] = []
        competitor = source_key == "competitor_activity"
        for index, item in enumerate(bucket):
            normalised = normalise_signal_item(item, f"trend_request.{source_key}[{index}]", competitor=competitor)
            if normalised is not None:
                cleaned_bucket.append(normalised)
        payload[source_key] = cleaned_bucket

    # A regulatory watch-list is operator-supplied context rather than a signal
    # bucket, so it is carried through the same string bounds and nothing else.
    regulatory = raw.get("regulatory")
    if isinstance(regulatory, dict):
        flags = regulatory.get("consumer_affairs_agency_flags")
        if isinstance(flags, list):
            payload["regulatory"] = {
                "consumer_affairs_agency_flags": [
                    safe_keyword(flag, "trend_request.regulatory.consumer_affairs_agency_flags[]")
                    for flag in flags[:MAX_METADATA_KEYS]
                ]
            }

    return payload
