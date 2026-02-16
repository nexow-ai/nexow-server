"""Bot management endpoints — generation + CRUD."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.ai.bot_factory import generate_bot
from server.db.client import SupabaseClient

router = APIRouter(prefix="/api/bots", tags=["bots"])

_db: SupabaseClient | None = None


def get_db() -> SupabaseClient:
    global _db
    if _db is None:
        _db = SupabaseClient()
    return _db


class GenerateBotRequest(BaseModel):
    prompt: str
    preferred_provider: str = "openai"


@router.post("/generate")
async def generate_bot_config(request: GenerateBotRequest):
    """Generate a bot config with Python strategy code from a natural language prompt."""
    try:
        result = await generate_bot(request.prompt, request.preferred_provider)
        return result.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{bot_id}")
async def get_bot(bot_id: str):
    """Get a single bot by ID."""
    bot = get_db().get_agent_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.get("type") != "bot":
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


@router.get("/")
async def list_active_bots():
    """List all active bots."""
    all_active = get_db().get_active_agents()
    return [a for a in all_active if a.get("type") == "bot"]


@router.patch("/{bot_id}/status")
async def update_bot_status(bot_id: str, status: str):
    """Update a bot's status (active, paused, killed)."""
    if status not in ("active", "paused", "killed"):
        raise HTTPException(status_code=400, detail="Invalid status")
    bot = get_db().get_agent_by_id(bot_id)
    if not bot or bot.get("type") != "bot":
        raise HTTPException(status_code=404, detail="Bot not found")
    get_db().update_agent_status(bot_id, status)
    return {"ok": True}
