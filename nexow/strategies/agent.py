"""Agent strategy — LLM-powered reasoning agents that analyze market context."""

from __future__ import annotations

from typing import Any

import structlog

from nexow.strategies.base import BaseStrategy, Signal, SignalType
from nexow.broker.models import Candle

logger = structlog.get_logger(__name__)


class AgentStrategy(BaseStrategy):
    """
    LLM-powered trading agent that reasons about market conditions,
    news sentiment, and technical context before making a decision.

    Powered by LangGraph reasoning chains with Tavily + NewsAPI data.
    """

    def __init__(self, strategy_id: str, config: dict[str, Any]) -> None:
        super().__init__(strategy_id, config)
        self.personality: str = config.get("personality", "cautious")
        self.reasoning_depth: int = config.get("reasoning_depth", 3)
        self.llm_provider: str = config.get("llm_provider", "openai")
        self.llm_model: str = config.get("llm_model", "gpt-4o-mini")

        portfolio = config.get("portfolio", {})
        self.instruments: list[str] = [
            ic["instrument"] for ic in portfolio.get("instruments", [])
        ]

    async def evaluate(self, candles: list[Candle], current_price: float) -> Signal:
        """Use the LangGraph reasoning engine to produce a trading signal."""
        instrument = candles[0].instrument if candles else "UNKNOWN"

        try:
            from nexow.ai.reasoning import run_reasoning_chain

            market_context = self._build_market_context(candles, current_price)
            result = await run_reasoning_chain(
                agent_config=self.config,
                market_context=market_context,
                personality=self.personality,
                instruments=self.instruments or [instrument],
            )

            signal_type = SignalType(result.get("action", "hold"))

            exit_config = self.config.get("exit", {})
            sl_pct = result.get("stop_loss_pct", exit_config.get("stop_loss_pct"))
            tp_pct = result.get("take_profit_pct", exit_config.get("take_profit_pct"))

            data_sources = []
            if self.config.get("use_web_search", True):
                data_sources.append("web_search")
            if self.config.get("use_news_feed", True):
                data_sources.append("news_sentiment")
            data_sources.append("technical_analysis")

            return Signal(
                type=signal_type,
                instrument=result.get("instrument", instrument),
                confidence=result.get("confidence", 0.5),
                stop_loss_pct=sl_pct if signal_type in (SignalType.BUY, SignalType.SELL) else None,
                take_profit_pct=tp_pct if signal_type in (SignalType.BUY, SignalType.SELL) else None,
                reason=result.get("reasoning", "LLM decision"),
                metadata={
                    "technical_summary": result.get("technical_summary", ""),
                    "sentiment_summary": result.get("sentiment_summary", ""),
                    "prompt_tokens": result.get("prompt_tokens", 0),
                    "completion_tokens": result.get("completion_tokens", 0),
                    "total_tokens": result.get("total_tokens", 0),
                    "duration_ms": result.get("duration_ms"),
                    "data_sources_used": data_sources,
                    "llm_provider": self.llm_provider,
                    "llm_model": self.llm_model,
                },
            )
        except Exception as e:
            logger.error("agent_strategy_error", strategy_id=self.strategy_id, error=str(e))
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                reason=f"Error in reasoning: {e}",
                metadata={
                    "llm_provider": self.llm_provider,
                    "llm_model": self.llm_model,
                },
            )

    def _build_market_context(self, candles: list[Candle], current_price: float) -> dict[str, Any]:
        if not candles:
            return {"current_price": current_price}

        recent = candles[-20:] if len(candles) >= 20 else candles
        prices = [c.close for c in recent]
        high = max(c.high for c in recent)
        low = min(c.low for c in recent)

        return {
            "instrument": candles[0].instrument,
            "current_price": current_price,
            "recent_high": high,
            "recent_low": low,
            "price_change_pct": ((prices[-1] - prices[0]) / prices[0]) * 100 if prices[0] else 0,
            "avg_volume": sum(c.volume for c in recent) / len(recent),
            "num_candles": len(candles),
            "latest_candles": [
                {"time": str(c.time), "o": c.open, "h": c.high, "l": c.low, "c": c.close}
                for c in candles[-5:]
            ],
        }
