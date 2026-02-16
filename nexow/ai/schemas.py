"""Pydantic schemas for AI-generated strategy configurations.

Bots and Agents have separate generation results:
- BotGenerationResult: trading bots with Python strategy code (WASM-sandboxed)
- AgentGenerationResult: LLM-powered reasoning agents
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────
# Shared enums & models
# ──────────────────────────────────────────────────────────

class AgentType(str, Enum):
    BOT = "bot"
    AGENT = "agent"


class LLMProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Personality(str, Enum):
    AGGRESSIVE = "aggressive"
    BALANCED = "balanced"
    CAUTIOUS = "cautious"
    CONSERVATIVE = "conservative"


class FocusArea(str, Enum):
    TECHNICAL_ANALYSIS = "technical_analysis"
    PRICE_ACTION = "price_action"
    NEWS_SENTIMENT = "news_sentiment"
    ECONOMIC_CALENDAR = "economic_calendar"
    VOLUME_ANALYSIS = "volume_analysis"


class InstrumentConfig(BaseModel):
    """A single instrument the agent monitors."""
    instrument: str = Field(description="Instrument symbol, e.g. 'EUR_USD', 'XAU_USD'")
    timeframe: str = Field(default="M5", description="Candle timeframe for this instrument")


class PortfolioConfig(BaseModel):
    """Instruments the agent trades."""
    instruments: list[InstrumentConfig] = Field(min_length=1, description="List of instruments with timeframes")


class ExitConfig(BaseModel):
    """Percentage-based exit levels."""
    stop_loss_pct: float | None = Field(default=2.0, ge=0.1, le=50.0, description="Stop-loss as % from entry price")
    take_profit_pct: float | None = Field(default=4.0, ge=0.1, le=100.0, description="Take-profit as % from entry price")


# ──────────────────────────────────────────────────────────
# Bot generation result (Python strategy code)
# ──────────────────────────────────────────────────────────

class BotGenerationResult(BaseModel):
    """Result of bot generation: Python strategy code executed in WASM sandbox."""
    agent_type: AgentType = Field(default=AgentType.BOT, description="Always 'bot'")
    name: str = Field(description="Generated name for the bot")
    description: str = Field(description="Human-readable description of the strategy")
    strategy_code: str = Field(description="Python evaluate() function code")
    config: dict = Field(description="The bot config: portfolio, exit")
    portfolio_summary: str = Field(default="", description="One-line summary of instruments")


# ──────────────────────────────────────────────────────────
# Agent generation result (LLM-powered)
# ──────────────────────────────────────────────────────────

class AgentGenerationResult(BaseModel):
    """Result of agent generation: LLM-powered reasoning agent config + metadata."""
    agent_type: AgentType = Field(default=AgentType.AGENT, description="Always 'agent'")
    name: str = Field(description="Generated name for the agent")
    description: str = Field(description="Human-readable description of the strategy")
    config: dict = Field(description="The full agent config: portfolio, exit, llm settings, personality, etc.")
    portfolio_summary: str = Field(default="", description="One-line summary of instruments")
