"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from shared.secrets import factory as secrets_factory

from src.graph.graph import ConsumerTrendSummaryAgent
from src.schemas.request_contract import credential_fields

app = FastAPI(title="Agent")

#: Largest serialised input_context accepted at this boundary.
MAX_INPUT_CONTEXT_BYTES = 256_000

# Same config_dir / "config.yaml" convention the registry uses; an absent file is
# tolerated the same way. Without this the standalone adapter always ran with
# config={}, so max_retry and every other declared runtime value silently never
# reached the graph on this path — a declaration nothing read.
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_config: Dict[str, Any] = load_config(str(_CONFIG_PATH)) if _CONFIG_PATH.exists() else {}

agent = ConsumerTrendSummaryAgent(config=_config)

# Mirror the registry's conditional checkpointer: hitl.enabled or memory_enabled
# needs one, or interrupt()/memory silently no-ops on this path. This pipeline is
# deterministic and declares neither, so no checkpointer is attached.
_hitl_enabled = bool(agent.config.get("hitl", {}).get("enabled", False))
_needs_checkpointer = bool(agent.config.get("memory_enabled")) or _hitl_enabled
if _needs_checkpointer:
    from langgraph.checkpoint.memory import MemorySaver

    agent.compile(checkpointer=MemorySaver())
else:
    agent.compile()

agent.provision_secrets(secrets_factory(namespace="ret", agent_name="ret_c2_332"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    input_context: Optional[Dict[str, Any]] = None


def _bearer_matches(supplied: str, expected: str) -> bool:
    """Constant-time bearer comparison that is safe for non-ASCII header input."""
    return secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode())


def _resolve_standalone_trust(
    current: TrustLevel,
    authorization: str,
    invoke_auth_token: Optional[str],
    internal_runner_token: Optional[str],
) -> TrustLevel:
    """Authenticate standalone callers without allowing external-token elevation.

    The deployment runner credential is a distinct, separately issued value. It is
    considered only for an anonymous caller and maps exactly to INTERNAL; the
    external invoke token remains VERIFIED_EXTERNAL. Trust already established by
    middleware is never changed.

    Without this resolution every standalone caller stays ANONYMOUS, and the
    domain nodes — which require INTERNAL — deny the request at the trust gate.
    That is the failure mode this function exists to prevent.
    """
    if current is not TrustLevel.ANONYMOUS:
        return current
    if internal_runner_token and _bearer_matches(authorization, internal_runner_token):
        return TrustLevel.INTERNAL
    if invoke_auth_token and _bearer_matches(authorization, invoke_auth_token):
        return TrustLevel.VERIFIED_EXTERNAL
    if internal_runner_token or invoke_auth_token:
        raise HTTPException(status_code=401, detail="Token is invalid or expired.")
    return TrustLevel.ANONYMOUS


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    # This adapter is the entry-point auth boundary (the standalone equivalent of
    # the platform auth middleware). Both values are deployment-level caller
    # credentials, not agent secrets: no invocation context exists before this
    # boundary, so ctx.secrets cannot apply.
    trust = _resolve_standalone_trust(
        getattr(request.state, "trust_level", TrustLevel.ANONYMOUS),
        request.headers.get("authorization", ""),
        os.environ.get("INVOKE_AUTH_TOKEN"),
        os.environ.get("STG_INTERNAL_RUNNER_TOKEN"),
    )

    input_context: Dict[str, Any] = req.input_context or {}

    # Size cap at the adapter. The platform's context channel has its own limit,
    # so a request larger than this cannot succeed downstream either; refusing it
    # here keeps the cost of an oversized request bounded at the boundary.
    if len(json.dumps(input_context, default=str)) > MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"input_context exceeds the {MAX_INPUT_CONTEXT_BYTES}-byte limit.",
        )

    # A credential-shaped string anywhere in input_context makes the FIRST node
    # fail: the framework's initialize step returns input_context verbatim in its
    # result, and the output gate scans every value of every result, so the run
    # ends with an opaque error before any of this template's code executes. The
    # request cannot succeed either way, so refuse it here where the failure can
    # name the field and be acted on.
    #
    # The check uses the framework's own detector, so what is refused here is
    # exactly what the gate would block — no local approximation that could drift.
    # Offending fields are NAMED; the offending value is never echoed.
    offending = credential_fields(input_context)
    if offending:
        # 400, not 422: pydantic owns 422 and answers it with a list of error
        # objects, so reusing it would make client handling ambiguous.
        raise HTTPException(
            status_code=400,
            detail=("Request refused: credential-shaped value in " + ", ".join(offending) + ". Remove it and retry."),
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # The framework's invoke() is untyped in the published wheel, so the
        # return is Any; the cast records the shape the caller is promised.
        return cast(Dict[str, Any], agent.invoke(req.input, ctx=ctx, input_context=input_context))


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "ret_c2_332"}
