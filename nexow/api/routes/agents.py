"""Agent management endpoints — CRUD + AI generation."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from nexow.ai.factory import generate_strategy
from nexow.db.client import SupabaseClient

router = APIRouter(prefix="/api/agents", tags=["agents"])

_db: SupabaseClient | None = None


def get_db() -> SupabaseClient:
    global _db
    if _db is None:
        _db = SupabaseClient()
    return _db


class GenerateRequest(BaseModel):
    prompt: str
    preferred_provider: str = "openai"


@router.post("/generate")
async def generate_agent_config(request: GenerateRequest):
    """Generate an agent config from a natural language prompt."""
    try:
        result = await generate_strategy(request.prompt, request.preferred_provider)
        return result.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}")
async def get_agent(agent_id: str):
    """Get a single agent by ID."""
    agent = get_db().get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("/")
async def list_active_agents():
    """List all active agents."""
    return get_db().get_active_agents()


@router.patch("/{agent_id}/status")
async def update_status(agent_id: str, status: str):
    """Update an agent's status (active, paused, killed)."""
    if status not in ("active", "paused", "killed"):
        raise HTTPException(status_code=400, detail="Invalid status")
    get_db().update_agent_status(agent_id, status)
    return {"ok": True}
