"""Agent Factory — converts natural language into LLM-powered reasoning agent configs.

Uses PydanticAI to generate validated agent configurations for discretionary
trading agents that use LLMs at runtime for market analysis and decision-making.
"""

from __future__ import annotations

import structlog
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.anthropic import AnthropicProvider

from server.ai.schemas import AgentGenerationResult
from server.config import settings

logger = structlog.get_logger(__name__)

AGENT_SYSTEM_PROMPT = """\
You are Nexow's Agent Factory. Convert a user's plain-English trading idea into a precise,
executable **agent** configuration for an LLM-powered reasoning agent.

## What is an Agent?

An agent is an **LLM-powered trading signal provider**. It uses AI reasoning at runtime to
analyze market data, news, sentiment, and context before each trade decision. Agents run on
a schedule and can incorporate external data sources.

Agents emit entry signals (BUY/SELL) and exit signals (CLOSE). There is NO position sizing,
no volume, no risk management in the agent config. Agents are compared purely by the gross
percentage return of their signals.

## Agent Config

The config must include:

### Portfolio
Which instruments and timeframes to monitor:
{"portfolio": {"instruments": [{"instrument": "EUR_USD", "timeframe": "H1"}]}}

Available instruments: EUR_USD, GBP_USD, USD_JPY, XAU_USD, USD_CAD, AUD_USD, NZD_USD, USD_CHF
Available timeframes: M1, M5, M15, M30, H1, H4, D

### LLM Settings
- llm_provider: "openai" or "anthropic"
- llm_model: e.g. "gpt-4o-mini", "claude-sonnet-4-20250514"

### Personality
One of: "aggressive", "balanced", "cautious", "conservative"
This affects the confidence threshold for trade execution.

### Focus Areas
List of data sources the agent will use:
- "technical_analysis", "price_action", "news_sentiment",
  "economic_calendar", "volume_analysis"

### Data Sources
- use_web_search: boolean — whether to search the web for market context
- use_news_feed: boolean — whether to pull news articles

### Evaluation Schedule
How often the agent evaluates the market:
- "every_tick", "5m", "15m", "30m", "hourly", "4h", "daily"

### Exit Levels (percentage-based)
{"exit": {"stop_loss_pct": 2.0, "take_profit_pct": 4.0}}

Choose levels that match the strategy style:
- Scalping: SL 0.3-0.5%, TP 0.5-1%
- Day trading: SL 1-2%, TP 2-4%
- Swing trading: SL 2-5%, TP 5-10%
- Position trading: SL 5-10%, TP 10-20%

## Output

Return valid JSON with: agent_type (always "agent"), name, description, config, portfolio_summary.
The config MUST contain: portfolio, exit, llm_provider, llm_model, personality, focus_areas,
use_web_search, use_news_feed, evaluation_schedule.
"""


def _build_model(provider: str = "openai") -> OpenAIModel | AnthropicModel:
    """Create an explicit model instance with the API key from settings."""
    if provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicModel(
            "claude-sonnet-4-20250514",
            provider=AnthropicProvider(api_key=settings.anthropic_api_key),
        )
    return OpenAIModel(
        "gpt-4o-mini",
        provider=OpenAIProvider(api_key=settings.openai_api_key),
    )


async def generate_agent(
    user_prompt: str,
    preferred_provider: str = "openai",
) -> AgentGenerationResult:
    """Generate a validated agent config (LLM-powered) from a natural language prompt."""
    logger.info("generating_agent", prompt=user_prompt[:100], provider=preferred_provider)

    model = _build_model(preferred_provider)
    factory = Agent(model, output_type=AgentGenerationResult, system_prompt=AGENT_SYSTEM_PROMPT)

    result = await factory.run(user_prompt)
    generation = result.output

    logger.info(
        "agent_generated",
        name=generation.name,
        has_llm_config="llm_provider" in generation.config,
    )
    return generation
