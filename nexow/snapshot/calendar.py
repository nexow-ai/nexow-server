"""Forex Factory economic calendar scraper — Playwright screenshot + Vision LLM OCR."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, datetime, timezone
from typing import Any

import httpx
import structlog
from playwright.async_api import async_playwright

from nexow.config import settings

logger = structlog.get_logger(__name__)

FF_CALENDAR_URL = "https://www.forexfactory.com/calendar?day=today"

VISION_PROMPT = """Analyze this screenshot of the Forex Factory economic calendar table.

Extract EVERY row visible in the table. For each event, return a JSON object with these fields:
- "date": the date in YYYY-MM-DD format (infer from page context or use today's date: {today})
- "time": the time string exactly as shown (e.g. "8:30am", "All Day", "Tentative", "")
- "currency": the 3-letter currency code (e.g. "USD", "EUR", "GBP")
- "impact": one of "high", "medium", "low", "holiday", "none" based on the colored icon:
  - Red/dark red folder icon = "high"
  - Orange folder icon = "medium"
  - Yellow folder icon = "low"
  - Gray/holiday = "holiday"
  - No icon or empty = "none"
- "event": the event name/title (e.g. "Non-Farm Employment Change")
- "actual": the actual value if shown (e.g. "256K", "3.5%"), or null if empty/not yet released
- "forecast": the forecast value if shown, or null
- "previous": the previous value if shown, or null

IMPORTANT:
- The date column may only appear on the first row of each day, then subsequent rows inherit it.
- The time column may only appear on the first row of a time group, then subsequent rows inherit it.
- Return ONLY a JSON array of objects. No markdown, no explanation.
- If actual values are highlighted in green, they beat forecast. If red, they missed. Just extract the raw value.

Return the JSON array:"""


async def screenshot_forex_factory() -> bytes | None:
    """Open Forex Factory calendar and take a screenshot of the events table."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            await page.goto(FF_CALENDAR_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_selector(".calendar__row", state="attached", timeout=20_000)

            # Let dynamic content settle (impact icons, lazy loaded data)
            await asyncio.sleep(3)

            table = page.locator(".calendar__table")
            screenshot = await table.screenshot(type="png")

            await browser.close()
            logger.info("ff_screenshot_taken", size_bytes=len(screenshot))
            return screenshot

    except Exception as e:
        logger.error("ff_screenshot_failed", error=str(e))
        return None


def _parse_llm_json(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array from LLM output, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])

    events = json.loads(text)
    if not isinstance(events, list):
        return []
    return events


async def _ocr_openai(b64_image: str, prompt: str) -> list[dict[str, Any]]:
    """Use OpenAI GPT-4o vision to extract events from the screenshot."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 4096,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64_image}"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return _parse_llm_json(text)


async def _ocr_anthropic(b64_image: str, prompt: str) -> list[dict[str, Any]]:
    """Use Anthropic Claude vision to extract events from the screenshot."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64_image,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]
        return _parse_llm_json(text)


async def ocr_screenshot(screenshot_png: bytes, today: date | None = None) -> list[dict[str, Any]]:
    """Extract economic events from screenshot using available vision LLM (OpenAI or Anthropic)."""
    today_str = (today or date.today()).isoformat()
    b64_image = base64.b64encode(screenshot_png).decode("utf-8")
    prompt = VISION_PROMPT.replace("{today}", today_str)

    # Try OpenAI first (has credits), then Anthropic as fallback
    providers: list[tuple[str, Any]] = []
    if settings.openai_api_key:
        providers.append(("openai", _ocr_openai))
    if settings.anthropic_api_key:
        providers.append(("anthropic", _ocr_anthropic))

    if not providers:
        logger.error("no_vision_api_key_configured")
        return []

    for name, fn in providers:
        try:
            events = await fn(b64_image, prompt)
            logger.info("ocr_extracted", provider=name, event_count=len(events))
            return events
        except json.JSONDecodeError as e:
            logger.warning("ocr_json_parse_error", provider=name, error=str(e))
        except Exception as e:
            logger.warning("ocr_provider_failed", provider=name, error=str(e))

    logger.error("ocr_all_providers_failed")
    return []


def normalize_impact(raw: str) -> str:
    """Normalize impact string to match the DB enum."""
    raw_lower = raw.strip().lower()
    if raw_lower in ("high", "medium", "low", "holiday", "none"):
        return raw_lower
    return "none"


def normalize_events(raw_events: list[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    """Normalize raw OCR events into DB-ready dicts."""
    today_str = (today or date.today()).isoformat()
    normalized = []

    for ev in raw_events:
        currency = (ev.get("currency") or "").strip().upper()
        event_name = (ev.get("event") or "").strip()
        if not currency or not event_name:
            continue

        normalized.append({
            "date": ev.get("date") or today_str,
            "time": ev.get("time") or "",
            "currency": currency,
            "impact": normalize_impact(ev.get("impact", "none")),
            "event": event_name,
            "actual": ev.get("actual") if ev.get("actual") else None,
            "forecast": ev.get("forecast") if ev.get("forecast") else None,
            "previous": ev.get("previous") if ev.get("previous") else None,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    return normalized


async def scrape_calendar_ocr() -> list[dict[str, Any]]:
    """Full pipeline: screenshot Forex Factory → Vision OCR → normalized events."""
    today = date.today()

    logger.info("calendar_ocr_start", date=today.isoformat())

    screenshot = await screenshot_forex_factory()
    if screenshot is None:
        return []

    raw_events = await ocr_screenshot(screenshot, today=today)
    if not raw_events:
        return []

    events = normalize_events(raw_events, today=today)
    logger.info("calendar_ocr_done", events=len(events))
    return events
