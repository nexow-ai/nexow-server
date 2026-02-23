"""LLM Analyzer — produces structured scores from a MarketSnapshot.

Called once per instrument per minute. Scores are shared across all users;
personalization is applied via user-defined weights (no extra LLM call).
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from nexow.config import settings
from nexow.db.client import SupabaseClient

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are an expert forex market analyst. Given a market snapshot containing \
technical indicators, price structure, momentum, session context, and \
economic events, produce a structured analysis with scores.

For each category, output a score from -1.0 (strongly bearish) to +1.0 (strongly bullish):
- technical_score: Based on RSI, MACD, Bollinger, EMA alignment, ATR
- momentum_score: Based on price changes, consecutive candles, volatility
- fundamental_score: Based on economic events, sentiment, upcoming news impact
- structure_score: Based on trends, support/resistance, market phase
- session_score: Based on session timing, day of week, liquidity expectations

Also provide:
- direction: "buy", "sell", or "hold"
- reasoning: A concise 1-2 sentence explanation

Respond ONLY with valid JSON, no markdown, no extra text:
{"technical_score": 0.0, "momentum_score": 0.0, "fundamental_score": 0.0, "structure_score": 0.0, "session_score": 0.0, "direction": "hold", "reasoning": "..."}\
"""


def _get_llm() -> ChatOpenAI:
    """Get the LLM instance for snapshot analysis."""
    if settings.openai_api_key:
        return ChatOpenAI(
            model="gpt-4o-mini",
            api_key=settings.openai_api_key,
            max_tokens=256,
            temperature=0.2,
        )
    if settings.anthropic_api_key:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model="claude-sonnet-4-20250514",
            api_key=settings.anthropic_api_key,
            max_tokens=256,
        )
    raise RuntimeError("No LLM API key configured (openai or anthropic)")


def _extract_token_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None) or {}
    return usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _parse_scores(text: str) -> dict[str, Any]:
    """Parse JSON scores from LLM response, with fallback."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("analyzer_parse_failed", raw=text[:200])
        return {
            "technical_score": 0, "momentum_score": 0, "fundamental_score": 0,
            "structure_score": 0, "session_score": 0,
            "direction": "hold", "reasoning": "Parse error",
        }

    # Clamp scores to [-1, 1]
    for key in ("technical_score", "momentum_score", "fundamental_score", "structure_score", "session_score"):
        val = data.get(key, 0)
        try:
            data[key] = max(-1.0, min(1.0, float(val)))
        except (TypeError, ValueError):
            data[key] = 0.0

    if data.get("direction") not in ("buy", "sell", "hold"):
        data["direction"] = "hold"

    return data


async def analyze_snapshot(
    snapshot_json: str,
    instrument: str,
    timestamp: str,
    db: SupabaseClient | None = None,
) -> dict[str, Any]:
    """Call LLM to analyze a snapshot and return structured scores.

    Also upserts the result into snapshot_analyses table if db is provided.
    """
    start = time.monotonic()

    llm = _get_llm()
    response = await llm.ainvoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=snapshot_json),
    ])

    duration_ms = int((time.monotonic() - start) * 1000)
    prompt_tokens, completion_tokens = _extract_token_usage(response)

    text = response.content if hasattr(response, "content") else str(response)
    scores = _parse_scores(text)

    # Compute overall as simple average of all section scores
    section_scores = [
        scores["technical_score"], scores["momentum_score"],
        scores["fundamental_score"], scores["structure_score"],
        scores["session_score"],
    ]
    scores["overall_score"] = round(sum(section_scores) / len(section_scores), 2)

    result = {
        "instrument": instrument,
        "timestamp": timestamp,
        "technical_score": scores["technical_score"],
        "momentum_score": scores["momentum_score"],
        "fundamental_score": scores["fundamental_score"],
        "structure_score": scores["structure_score"],
        "session_score": scores["session_score"],
        "overall_score": scores["overall_score"],
        "direction": scores["direction"],
        "reasoning": scores.get("reasoning", ""),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "llm_model": llm.model_name if hasattr(llm, "model_name") else "unknown",
        "duration_ms": duration_ms,
    }

    # Store in DB
    if db:
        try:
            db.upsert_snapshot_analysis(result)
        except Exception as e:
            logger.warning("analyzer_db_upsert_failed", error=str(e))

    logger.info(
        "snapshot_analyzed",
        instrument=instrument,
        direction=scores["direction"],
        overall=scores["overall_score"],
        duration_ms=duration_ms,
        tokens=prompt_tokens + completion_tokens,
    )

    return result
