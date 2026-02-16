"""LangGraph reasoning chains for discretionary agents — with real LLM calls and external data."""

from __future__ import annotations

import time
from typing import Any, TypedDict

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from nexow.ai.tools import gather_external_context
from nexow.config import settings

logger = structlog.get_logger(__name__)


class ReasoningState(TypedDict):
    """State that flows through the reasoning graph."""

    agent_config: dict[str, Any]
    market_context: dict[str, Any]
    personality: str
    instruments: list[str]

    external_data: dict[str, Any]
    technical_analysis: str
    sentiment_analysis: str
    correlation_analysis: str
    final_reasoning: str

    action: str
    instrument: str
    confidence: float
    stop_loss_pct: float | None
    take_profit_pct: float | None
    reasoning: str
    step: int

    prompt_tokens: int
    completion_tokens: int


def _get_llm(provider: str = "openai", model: str = "gpt-4o-mini") -> ChatOpenAI:
    """Get a LangChain LLM instance."""
    if provider == "anthropic" and settings.anthropic_api_key:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model or "claude-sonnet-4-20250514",
            api_key=settings.anthropic_api_key,
            max_tokens=1024,
        )
    return ChatOpenAI(
        model=model or "gpt-4o-mini",
        api_key=settings.openai_api_key,
        max_tokens=1024,
        temperature=0.3,
    )


def _extract_token_usage(response: Any) -> tuple[int, int]:
    """Extract prompt/completion token counts from a LangChain response."""
    usage = getattr(response, "usage_metadata", None) or {}
    prompt = usage.get("input_tokens", 0)
    completion = usage.get("output_tokens", 0)
    return prompt, completion


async def fetch_external_data(state: ReasoningState) -> dict:
    config = state["agent_config"]
    instruments = state["instruments"]
    use_search = config.get("use_web_search", True)
    use_news = config.get("use_news_feed", True)

    external = await gather_external_context(
        instruments=instruments, use_web_search=use_search, use_news_feed=use_news,
    )
    logger.info("external_data_fetched", news_count=len(external.get("news", [])), search_count=len(external.get("web_search", [])))
    return {"external_data": external, "step": state.get("step", 0) + 1}


async def analyze_technicals(state: ReasoningState) -> dict:
    ctx = state["market_context"]
    personality = state["personality"]
    price = ctx.get("current_price", 0)
    change = ctx.get("price_change_pct", 0)
    high = ctx.get("recent_high", 0)
    low = ctx.get("recent_low", 0)
    candles = ctx.get("latest_candles", [])

    analysis = (
        f"Instrument: {ctx.get('instrument', 'N/A')}\n"
        f"Current price: {price:.5f}\n"
        f"Recent change: {change:.2f}%\n"
        f"Range: {low:.5f} - {high:.5f}\n"
        f"Position in range: {((price - low) / (high - low) * 100) if high != low else 50:.1f}%\n"
        f"Last {len(candles)} candles: {[c.get('c', 0) for c in candles]}\n"
        f"Personality: {personality}"
    )
    return {"technical_analysis": analysis, "step": state.get("step", 0) + 1}


async def analyze_sentiment(state: ReasoningState) -> dict:
    external = state.get("external_data", {})
    news = external.get("news", [])
    web = external.get("web_search", [])

    if not news and not web:
        return {"sentiment_analysis": "No external data available.", "step": state.get("step", 0) + 1}

    config = state["agent_config"]
    llm = _get_llm(provider=config.get("llm_provider", "openai"), model=config.get("llm_model", "gpt-4o-mini"))

    news_text = "\n".join(f"- [{n.get('source', '?')}] {n.get('title', '')}: {n.get('description', '')}" for n in news[:5])
    web_text = "\n".join(f"- {w.get('title', '')}: {w.get('content', '')[:200]}" for w in web[:5])

    prompt = (
        f"Analyze the following market news and web data for forex trading sentiment.\n\n"
        f"NEWS:\n{news_text}\n\nWEB ANALYSIS:\n{web_text}\n\n"
        f"Instruments of interest: {', '.join(state['instruments'])}\n\n"
        f"Provide a concise sentiment analysis (bullish/bearish/neutral) for each instrument "
        f"and note any key events that could impact prices. Keep it under 200 words."
    )

    try:
        response = await llm.ainvoke([SystemMessage(content="You are a financial sentiment analyst."), HumanMessage(content=prompt)])
        sentiment = response.content
        p_tok, c_tok = _extract_token_usage(response)
    except Exception as e:
        logger.error("sentiment_analysis_error", error=str(e))
        sentiment = f"Sentiment analysis unavailable: {e}"
        p_tok, c_tok = 0, 0

    return {
        "sentiment_analysis": str(sentiment),
        "step": state.get("step", 0) + 1,
        "prompt_tokens": state.get("prompt_tokens", 0) + p_tok,
        "completion_tokens": state.get("completion_tokens", 0) + c_tok,
    }


async def analyze_correlations(state: ReasoningState) -> dict:
    instruments = state["instruments"]
    if len(instruments) <= 1:
        return {"correlation_analysis": "Single instrument, no correlation analysis needed.", "step": state.get("step", 0) + 1}

    known_correlations = {
        ("EUR_USD", "GBP_USD"): 0.85, ("EUR_USD", "USD_CHF"): -0.90,
        ("AUD_USD", "NZD_USD"): 0.88, ("XAU_USD", "USD_JPY"): -0.40, ("EUR_USD", "USD_JPY"): -0.30,
    }

    lines = [f"Portfolio: {', '.join(instruments)}"]
    for i, a in enumerate(instruments):
        for b in instruments[i + 1:]:
            pair = (a, b) if (a, b) in known_correlations else (b, a)
            corr = known_correlations.get(pair, 0.0)
            direction = "positive" if corr > 0 else "negative"
            strength = "strong" if abs(corr) > 0.7 else "moderate" if abs(corr) > 0.4 else "weak"
            lines.append(f"  {a} vs {b}: {corr:.2f} ({strength} {direction})")

    return {"correlation_analysis": "\n".join(lines), "step": state.get("step", 0) + 1}


async def make_decision(state: ReasoningState) -> dict:
    config = state["agent_config"]
    llm = _get_llm(provider=config.get("llm_provider", "openai"), model=config.get("llm_model", "gpt-4o-mini"))
    personality = state["personality"]

    prompt = (
        f"You are a {personality} forex trader making a decision.\n\n"
        f"## Technical Analysis\n{state.get('technical_analysis', 'N/A')}\n\n"
        f"## Sentiment Analysis\n{state.get('sentiment_analysis', 'N/A')}\n\n"
        f"## Correlation Analysis\n{state.get('correlation_analysis', 'N/A')}\n\n"
        f"Based on ALL the above analysis, decide:\n"
        f"1. ACTION: buy, sell, or hold\n2. INSTRUMENT: which one from {state['instruments']}\n"
        f"3. CONFIDENCE: 0.0 to 1.0\n4. REASONING: 1-2 sentences explaining why\n\n"
        f"Respond in exactly this format:\nACTION: <buy|sell|hold>\nINSTRUMENT: <instrument>\n"
        f"CONFIDENCE: <0.0-1.0>\nREASONING: <explanation>"
    )

    try:
        response = await llm.ainvoke([SystemMessage(content="You are an expert forex trading AI. Be decisive but risk-aware."), HumanMessage(content=prompt)])
        text = str(response.content)
        p_tok, c_tok = _extract_token_usage(response)

        action = "hold"
        instrument = state["instruments"][0] if state["instruments"] else "EUR_USD"
        confidence = 0.5
        reasoning = text

        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("ACTION:"):
                action = line.split(":", 1)[1].strip().lower()
                if action not in ("buy", "sell", "hold", "close"):
                    action = "hold"
            elif line.upper().startswith("INSTRUMENT:"):
                parsed_inst = line.split(":", 1)[1].strip().upper().replace("/", "_")
                if parsed_inst in state["instruments"]:
                    instrument = parsed_inst
            elif line.upper().startswith("CONFIDENCE:"):
                try:
                    confidence = max(0.0, min(1.0, float(line.split(":", 1)[1].strip())))
                except ValueError:
                    pass
            elif line.upper().startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()

        thresholds = {"aggressive": 0.3, "balanced": 0.5, "cautious": 0.6, "conservative": 0.7}
        threshold = thresholds.get(personality, 0.5)
        if confidence < threshold and action != "hold":
            reasoning += f" [Filtered: confidence {confidence:.2f} < {personality} threshold {threshold}]"
            action = "hold"

        exit_config = config.get("exit", {})
        sl_pct = exit_config.get("stop_loss_pct", 2.0)
        tp_pct = exit_config.get("take_profit_pct", 4.0)

        return {
            "action": action, "instrument": instrument, "confidence": confidence,
            "stop_loss_pct": sl_pct if action in ("buy", "sell") else None,
            "take_profit_pct": tp_pct if action in ("buy", "sell") else None,
            "reasoning": reasoning, "final_reasoning": text, "step": state.get("step", 0) + 1,
            "prompt_tokens": state.get("prompt_tokens", 0) + p_tok,
            "completion_tokens": state.get("completion_tokens", 0) + c_tok,
        }
    except Exception as e:
        logger.error("decision_llm_error", error=str(e))
        return {
            "action": "hold", "instrument": state["instruments"][0] if state["instruments"] else "EUR_USD",
            "confidence": 0.0, "reasoning": f"LLM error: {e}", "step": state.get("step", 0) + 1,
            "prompt_tokens": state.get("prompt_tokens", 0),
            "completion_tokens": state.get("completion_tokens", 0),
        }


def build_reasoning_graph() -> StateGraph:
    graph = StateGraph(ReasoningState)
    graph.add_node("fetch_data", fetch_external_data)
    graph.add_node("technicals", analyze_technicals)
    graph.add_node("sentiment", analyze_sentiment)
    graph.add_node("correlations", analyze_correlations)
    graph.add_node("decide", make_decision)
    graph.set_entry_point("fetch_data")
    graph.add_edge("fetch_data", "technicals")
    graph.add_edge("technicals", "sentiment")
    graph.add_edge("sentiment", "correlations")
    graph.add_edge("correlations", "decide")
    graph.add_edge("decide", END)
    return graph


_compiled_graph = build_reasoning_graph().compile()


async def run_reasoning_chain(
    agent_config: dict[str, Any],
    market_context: dict[str, Any],
    personality: str = "cautious",
    instruments: list[str] | None = None,
) -> dict[str, Any]:
    """Execute the reasoning graph and return the decision with token usage."""
    start_ms = time.monotonic()

    initial_state: ReasoningState = {
        "agent_config": agent_config, "market_context": market_context,
        "personality": personality, "instruments": instruments or [market_context.get("instrument", "EUR_USD")],
        "external_data": {}, "technical_analysis": "", "sentiment_analysis": "",
        "correlation_analysis": "", "final_reasoning": "",
        "action": "hold", "instrument": (instruments or ["EUR_USD"])[0],
        "confidence": 0.0, "stop_loss_pct": None, "take_profit_pct": None, "reasoning": "", "step": 0,
        "prompt_tokens": 0, "completion_tokens": 0,
    }

    result = await _compiled_graph.ainvoke(initial_state)
    elapsed_ms = int((time.monotonic() - start_ms) * 1000)

    prompt_tokens = result.get("prompt_tokens", 0)
    completion_tokens = result.get("completion_tokens", 0)

    return {
        "action": result["action"],
        "instrument": result.get("instrument", initial_state["instrument"]),
        "confidence": result["confidence"],
        "stop_loss_pct": result["stop_loss_pct"],
        "take_profit_pct": result["take_profit_pct"],
        "reasoning": result["reasoning"],
        "technical_summary": result.get("technical_analysis", ""),
        "sentiment_summary": result.get("sentiment_analysis", ""),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "duration_ms": elapsed_ms,
    }
