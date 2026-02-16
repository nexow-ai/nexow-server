"""External data tools for discretionary agents — web search, news, economic calendar."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from nexow.config import settings

logger = structlog.get_logger(__name__)


async def search_web(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search the web using Tavily API."""
    if not settings.tavily_api_key:
        logger.warning("tavily_not_configured")
        return []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.tavily_api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            if data.get("answer"):
                results.append({"title": "AI Summary", "url": "", "content": data["answer"]})

            for r in data.get("results", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", "")[:500],
                })

            return results
    except Exception as e:
        logger.error("tavily_search_error", error=str(e))
        return []


INSTRUMENT_KEYWORDS: dict[str, str] = {
    "EUR_USD": "EUR USD euro dollar forex",
    "GBP_USD": "GBP USD pound sterling forex",
    "USD_JPY": "USD JPY yen dollar forex",
    "XAU_USD": "gold XAU price",
    "USD_CAD": "USD CAD canadian dollar forex",
    "AUD_USD": "AUD USD australian dollar forex",
    "NZD_USD": "NZD USD new zealand dollar forex",
    "USD_CHF": "USD CHF swiss franc forex",
}


async def get_financial_news(instrument: str, max_articles: int = 5) -> list[dict[str, str]]:
    """Fetch recent financial news relevant to an instrument using NewsAPI."""
    if not settings.newsapi_key:
        logger.warning("newsapi_not_configured")
        return []

    keywords = INSTRUMENT_KEYWORDS.get(instrument, instrument.replace("_", " "))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": keywords,
                    "sortBy": "publishedAt",
                    "pageSize": max_articles,
                    "language": "en",
                    "apiKey": settings.newsapi_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            return [
                {
                    "title": a.get("title", ""),
                    "description": a.get("description", "")[:300] if a.get("description") else "",
                    "source": a.get("source", {}).get("name", ""),
                    "published_at": a.get("publishedAt", ""),
                    "url": a.get("url", ""),
                }
                for a in data.get("articles", [])[:max_articles]
            ]
    except Exception as e:
        logger.error("newsapi_error", error=str(e))
        return []


async def get_economic_calendar(instruments: list[str] | None = None) -> list[dict[str, str]]:
    """Get upcoming economic events using Tavily search as a proxy."""
    currencies = set()
    for inst in (instruments or ["EUR_USD"]):
        parts = inst.split("_")
        currencies.update(parts)

    currency_str = " ".join(currencies)
    query = f"economic calendar today {currency_str} forex events interest rate decision"
    return await search_web(query, max_results=3)


async def gather_external_context(
    instruments: list[str],
    use_web_search: bool = True,
    use_news_feed: bool = True,
) -> dict[str, Any]:
    """Gather all external data for discretionary agent reasoning."""
    context: dict[str, Any] = {"news": [], "web_search": [], "economic_calendar": []}

    if use_news_feed:
        all_news = []
        for inst in instruments[:3]:
            news = await get_financial_news(inst, max_articles=3)
            all_news.extend(news)
        context["news"] = all_news

    if use_web_search:
        instrument_names = [i.replace("_", "/") for i in instruments[:3]]
        query = f"forex market analysis today {' '.join(instrument_names)} outlook"
        context["web_search"] = await search_web(query, max_results=5)
        context["economic_calendar"] = await get_economic_calendar(instruments)

    return context
