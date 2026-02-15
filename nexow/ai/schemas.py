"""Pydantic schemas for AI-generated strategy configurations."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


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


class AgentGenerationResult(BaseModel):
    """Result of the AI agent factory: the generated config + metadata."""
    agent_type: AgentType = Field(description="'bot' or 'agent'")
    name: str = Field(description="Generated name for the agent")
    description: str = Field(description="Human-readable description of the strategy")
    config: dict = Field(description="The full strategy config as JSON (portfolio, rules, exit)")
    portfolio_summary: str = Field(default="", description="One-line summary of instruments")
