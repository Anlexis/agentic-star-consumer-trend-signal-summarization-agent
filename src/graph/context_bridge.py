"""AgentCore Platform v1.0"""

# RET-C2-332 — caller-context bridge between the outer and inner graph.
#
# Why this module exists
# ----------------------
# The nested two-layer architecture runs the domain pipeline inside a separate
# graph instance. The framework's subgraph node calls
# ``subgraph.invoke(user_input, session_id=..., ctx=...)`` and does NOT forward
# ``input_context``, so a caller-supplied payload that arrives on the outer graph
# is invisible to every inner node unless it is carried across explicitly.
#
# A ContextVar is used rather than smuggling the payload through ``user_input``
# because the payload is structured data, not text: encoding it into the inner
# ``user_input`` string would expose it to text-shaped processing it was never
# meant for, and would lose type information on the way in.
#
# The bridge is deliberately narrow:
#   * ``stash()``   — called by the outer subgraph node just before delegation.
#   * ``consume()`` — called once by the inner graph while it builds its initial
#                     state; it CLEARS the slot so a value can never survive into
#                     an unrelated invocation on the same worker thread.
#
# ContextVar (not a module global) is what makes that safe: each asyncio task and
# each thread sees its own value, so two concurrent requests cannot read each
# other's payload.

from contextvars import ContextVar
from typing import Any, Dict, Optional

# Payload handed from the outer graph to the inner graph for one invocation.
_CALLER_PAYLOAD: ContextVar[Optional[Dict[str, Any]]] = ContextVar("ret_c2_332_caller_payload", default=None)


def stash(payload: Optional[Dict[str, Any]]) -> None:
    """Record the validated caller payload for the inner graph of this invocation."""
    _CALLER_PAYLOAD.set(dict(payload) if isinstance(payload, dict) else None)


def consume() -> Optional[Dict[str, Any]]:
    """Return the stashed payload and clear the slot.

    Clearing on read is the invariant that keeps one request's payload out of the
    next one: a graph that is invoked without a preceding ``stash()`` reads None.
    """
    payload = _CALLER_PAYLOAD.get()
    _CALLER_PAYLOAD.set(None)
    return payload


def peek() -> Optional[Dict[str, Any]]:
    """Return the stashed payload without clearing it (tests and assertions only)."""
    return _CALLER_PAYLOAD.get()


def clear() -> None:
    """Drop any stashed payload — used by test fixtures to guarantee isolation."""
    _CALLER_PAYLOAD.set(None)
