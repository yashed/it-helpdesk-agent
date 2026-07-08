"""FastAPI entrypoint for the IT helpdesk agent.

Implements the AM chat-agent contract: ``POST /chat`` on port 8000 accepting
``{session_id, message, context}`` and returning ``{response, session_id}``.
``GET /health`` is provided for local checks (AM does not require it).
"""

from __future__ import annotations

# Load environment variables from .env file before other imports
from dotenv import load_dotenv
load_dotenv()

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from agent import build_agent
from config import Config
from identity import AGENT_IDENTITY

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("it-helpdesk")

CONFIG = Config.from_env()
AGENT = build_agent(CONFIG)
log.info(
    "IT helpdesk agent ready (company=%s, tone=%s, llm_provider=%s)",
    CONFIG.company_name,
    CONFIG.tone,
    "agent-manager" if CONFIG.use_llm_provider else "openai-direct",
)

# Best-effort eager mint at startup: makes `kubectl logs` right after a
# deploy/promote/rotation immediately show whether the injected AgentID
# credentials actually work, without waiting for a /chat or /identity call.
# AGENT_IDENTITY itself already logged AGENTID_STATUS in its constructor;
# this adds the first AGENTID_TOKEN_MINTED/AGENTID_TOKEN_MINT_FAILED line.
if AGENT_IDENTITY.available:
    try:
        AGENT_IDENTITY.get_token()
    except Exception:  # noqa: BLE001 — startup must never crash on identity failure
        log.exception("Startup AgentID token mint failed — see AGENTID_TOKEN_MINT_FAILED above")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    context: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str | None = None


app = FastAPI(title="IT Helpdesk Agent", version="0.1.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "company": CONFIG.company_name}


@app.get("/identity")
def identity(force: bool = False) -> dict[str, Any]:
    """Non-secret AgentID credential status — for manually verifying the
    Gateway Binding feature (injection, promotion isolation, rotation).

    Never returns the client_secret or the access token itself. Set
    ``?force=true`` to force a fresh token mint right now, instead of
    waiting for the natural refresh window — useful right after a
    regenerate/revoke call to see the new state immediately.
    """
    if not AGENT_IDENTITY.available:
        return AGENT_IDENTITY.status()
    try:
        AGENT_IDENTITY.get_token(force_refresh=force)
    except Exception as exc:  # noqa: BLE001 — surface the error in the response, not a 500
        return {**AGENT_IDENTITY.status(), "mint_attempt_error": str(exc)}
    return AGENT_IDENTITY.status()


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        result = AGENT.invoke({"messages": [HumanMessage(content=req.message)]})
    except Exception as exc:  # noqa: BLE001
        log.exception("agent invocation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    final: Any = None
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage):
            final = m.content
            break
    if final is None:
        final = "(no response)"
    if isinstance(final, list):
        final = "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in final
        )
    return ChatResponse(response=str(final), session_id=req.session_id)
