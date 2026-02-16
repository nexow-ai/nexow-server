"""Strategy Labs Architect — conversational AI for building trading strategies.

Uses PydanticAI to handle multi-turn conversations where users describe
trading ideas, asks clarifying questions, and produces structured strategy
configs + Python evaluate() code. Streams responses via SSE.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import structlog
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider

from nexow.ai.bot_factory import BOT_SYSTEM_PROMPT
from nexow.ai.code_validator import CodeValidationError, sanitize_strategy_code, validate_strategy_code
from nexow.config import settings
from nexow.strategies.wasm_client import dry_run_strategy

logger = structlog.get_logger(__name__)

AI_TIMEOUT_SECONDS = 120.0
HEARTBEAT_INTERVAL_SECONDS = 3.0
MAX_CODE_RETRIES = 3


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LabInstrument(BaseModel):
    instrument: str
    timeframe: str


class LabExitConfig(BaseModel):
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None


class LabStrategy(BaseModel):
    name: str = ""
    description: str = ""
    type: str = "bot"
    instruments: list[LabInstrument] = Field(default_factory=list)
    entryRules: str = ""
    exitConfig: LabExitConfig = Field(default_factory=LabExitConfig)
    strategyCode: str = ""
    config: dict = Field(default_factory=dict)
    portfolioSummary: str = ""
    completeness: int = 0  # 0-100


class LabArchitectOutput(BaseModel):
    """Structured output from the strategy architect."""

    response: str = Field(description="Conversational reply to the user")
    strategy: LabStrategy | None = Field(
        default=None,
        description="Updated strategy if the user has provided enough info. "
        "Set completeness 0-100 based on how complete the strategy is.",
    )
    suggestBacktest: bool = Field(
        default=False,
        description="True if the strategy is ready to be backtested",
    )


class LabMessageInput(BaseModel):
    role: str
    content: str


class LabRequest(BaseModel):
    messages: list[LabMessageInput]
    currentStrategy: LabStrategy | None = None
    provider: str = "openai"
    model: str | None = None


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

LAB_ARCHITECT_SYSTEM = f"""\
You are Nexow's **Strategy Architect** — a friendly, expert trading strategy designer.

## Your Role
Help users build trading strategies through conversation. You:
1. Listen to their idea and ask clarifying questions (max 2 at a time)
2. Suggest improvements based on your trading knowledge
3. Generate the strategy configuration once you have enough info
4. Analyze backtest results and suggest refinements

## Conversation Flow
- **Phase 1 (Gathering):** Ask about instruments, timeframe, entry/exit logic, risk management.
  Be concise. Ask max 2 questions per turn.
- **Phase 2 (Building):** Once enough info → generate the strategy (set completeness ≥ 70)
- **Phase 3 (Refining):** After backtests, suggest improvements. Keep iterating.

## CRITICAL: When To Generate The Strategy
When the user has provided enough detail (instrument, timeframe, entry logic, risk parameters)
you MUST populate the `strategy` field in that SAME response. Do NOT say "I'll build it now"
or "Let me generate that" and leave strategy as null — that breaks the UI.

If the user confirms their parameters, you have enough info. Generate immediately:
- Fill in ALL strategy fields (name, description, instruments, entryRules, exitConfig, strategyCode, etc.)
- Set completeness ≥ 70
- Include working strategyCode with a valid evaluate() function
- Set suggestBacktest = true

A response that promises to build but returns strategy=null is ALWAYS wrong.

## Strategy Completeness Guide
- 0-30: Only has a vague idea, missing most details
- 30-50: Has instrument/timeframe but unclear entry logic
- 50-70: Has entry logic but missing exits or risk management
- 70-85: Complete enough to backtest but could be improved
- 85-100: Well-defined strategy with clear entry, exit, risk management

## CRITICAL: Strategy Code Requirement
When completeness >= 70, you MUST always include `strategyCode` containing a valid Python
`evaluate(df, current_price, open_trades)` function. A strategy without code cannot be
backtested. Never set completeness >= 70 without providing strategyCode.

## CRITICAL: Sandbox Constraints
- There is NO `pl.rsi()`, `pl.macd()`, `pl.bollinger()`, `pl.ta`, or similar built-in indicators.
- **numpy is NOT installed** — do NOT use `np`, `numpy`, or any numpy-based operations.
- ONLY `pl` (polars) and `math` are available. Nothing else.
- You MUST compute every indicator from scratch using basic Polars operations
  (rolling, ewm_mean, diff, clip, etc.). See the recipes below.

## Strategy Code Reference
{BOT_SYSTEM_PROMPT.split("## Function Signature")[1]}

## Style
- Be concise but warm. Use emoji sparingly (1-2 per message).
- Use **bold** for key terms.
- When showing numbers use them inline, don't create tables unless asked.
- Always explain your reasoning briefly.
- If suggesting a backtest, set suggestBacktest=true.
"""


# ---------------------------------------------------------------------------
# Model builder
# ---------------------------------------------------------------------------


# Default models per provider
DEFAULT_MODELS = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-20250514",
    "google": "gemini-2.0-flash",
    "deepseek": "deepseek-chat",
}


def _build_model(
    provider: str = "openai",
    model: str | None = None,
) -> OpenAIModel | AnthropicModel | GoogleModel:
    model_name = model or DEFAULT_MODELS.get(provider, "gpt-4.1-mini")

    if provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(api_key=settings.anthropic_api_key),
        )
    if provider == "google" and settings.google_api_key:
        return GoogleModel(
            model_name,
            provider=GoogleProvider(api_key=settings.google_api_key),
        )
    if provider == "deepseek" and settings.deepseek_api_key:
        return OpenAIModel(
            model_name,
            provider=OpenAIProvider(
                api_key=settings.deepseek_api_key,
                base_url="https://api.deepseek.com",
            ),
        )
    # Default to OpenAI
    return OpenAIModel(
        model_name,
        provider=OpenAIProvider(api_key=settings.openai_api_key),
    )


# ---------------------------------------------------------------------------
# Main conversation handler
# ---------------------------------------------------------------------------


async def _run_with_heartbeat(
    coro,
    provider: str,
) -> tuple[object | None, Exception | None]:
    """Run a coroutine while yielding heartbeat events to keep SSE alive.

    Returns (result, error) tuple.
    """
    done_event = asyncio.Event()
    result_holder: dict = {"output": None, "error": None}

    async def _task():
        try:
            result_holder["output"] = await asyncio.wait_for(
                coro, timeout=AI_TIMEOUT_SECONDS
            )
        except Exception as exc:
            result_holder["error"] = exc
        finally:
            done_event.set()

    asyncio.create_task(_task())
    return done_event, result_holder


async def process_lab_message(
    request: LabRequest,
) -> AsyncIterator[str]:
    """Process a lab conversation message and yield SSE events.

    Yields "data: {json}\n\n" formatted strings for SSE streaming.
    Sends heartbeat events every few seconds to keep the connection alive
    while the AI model is generating.
    """
    logger.info(
        "lab_architect_processing",
        message_count=len(request.messages),
        has_strategy=request.currentStrategy is not None,
        provider=request.provider,
        model=request.model,
    )

    yield f"data: {json.dumps({'type': 'status', 'status': 'processing'})}\n\n"

    model = _build_model(request.provider, request.model)
    architect = Agent(
        model,
        output_type=LabArchitectOutput,
        system_prompt=LAB_ARCHITECT_SYSTEM,
    )

    # Build the prompt from conversation history
    history_parts = []
    for msg in request.messages:
        prefix = "User" if msg.role == "user" else "Assistant"
        history_parts.append(f"{prefix}: {msg.content}")

    # Include current strategy context if available
    context = "\n".join(history_parts)
    if request.currentStrategy:
        strategy_json = request.currentStrategy.model_dump_json(indent=2)
        context += f"\n\n[CURRENT STRATEGY STATE]\n{strategy_json}"

    # 1. Run the architect with heartbeat keepalives
    done_event, result_holder = await _run_with_heartbeat(
        architect.run(context), request.provider
    )

    while not done_event.is_set():
        try:
            await asyncio.wait_for(done_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    if result_holder["error"]:
        err = result_holder["error"]
        if isinstance(err, asyncio.TimeoutError):
            logger.error("lab_architect_timeout", provider=request.provider)
            yield f"data: {json.dumps({'type': 'error', 'error': f'AI Provider Timeout ({request.provider}: >{int(AI_TIMEOUT_SECONDS)}s)'})}\n\n"
        else:
            logger.error("lab_architect_error", error=str(err), provider=request.provider)
            yield f"data: {json.dumps({'type': 'error', 'error': f'AI Provider Error ({request.provider}): {err}'})}\n\n"
        return

    run_result = result_holder["output"]
    output: LabArchitectOutput | None = run_result.output if run_result else None

    if not output:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Empty response from AI provider'})}\n\n"
        return

    # 1b. Safety net: if the AI promised to build but returned no strategy,
    #     re-run with an explicit instruction to generate it now.
    _build_keywords = ("build", "generate", "create", "craft", "construct", "hold on", "moment")
    response_lower = output.response.lower()
    promised_but_empty = (
        not output.strategy
        and any(kw in response_lower for kw in _build_keywords)
    )
    if promised_but_empty:
        logger.warning("lab_architect_promised_no_strategy", provider=request.provider)
        force_context = (
            context
            + "\n\n[SYSTEM] You said you would build the strategy but returned strategy=null. "
            "This is WRONG. You MUST populate the strategy field NOW with:\n"
            "- name, description, instruments, entryRules, exitConfig\n"
            "- strategyCode with a complete evaluate() function\n"
            "- completeness >= 70\n"
            "Do NOT ask more questions. Generate the full strategy IMMEDIATELY."
        )
        try:
            yield f"data: {json.dumps({'type': 'status', 'status': 'generating'})}\n\n"

            force_done, force_holder = await _run_with_heartbeat(
                architect.run(force_context), request.provider
            )
            while not force_done.is_set():
                try:
                    await asyncio.wait_for(force_done.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

            if force_holder["error"]:
                logger.error("lab_architect_force_failed", error=str(force_holder["error"]))
            else:
                force_result = force_holder["output"]
                if force_result and force_result.output and force_result.output.strategy:
                    output = force_result.output
                    logger.info("lab_architect_force_generated", provider=request.provider)
                else:
                    logger.warning("lab_architect_force_still_empty", provider=request.provider)
        except Exception as e:
            logger.error("lab_architect_force_failed", error=str(e))

    # 2. Validate strategy code: sanitize → AST check → WASM dry-run → retry
    code_validated = False
    if output.strategy and output.strategy.strategyCode:
        for attempt in range(MAX_CODE_RETRIES):
            code = output.strategy.strategyCode if output.strategy else ""
            if not code:
                break

            # Strip harmless imports (import polars as pl, import math, etc.)
            code = sanitize_strategy_code(code)
            output.strategy.strategyCode = code

            # 2a. Static AST validation
            error_msg: str | None = None
            try:
                validate_strategy_code(code)
            except CodeValidationError as e:
                error_msg = f"[CODE VALIDATION ERROR]\n{e}"

            # 2b. Dry-run in WASM sandbox (only if AST passed)
            if not error_msg:
                yield f"data: {json.dumps({'type': 'status', 'status': 'validating'})}\n\n"
                _, runtime_error = await dry_run_strategy(code)
                if runtime_error:
                    error_msg = (
                        f"[RUNTIME EXECUTION ERROR]\n"
                        f"The code failed when executed with sample data:\n{runtime_error}\n\n"
                        f"CRITICAL RULES:\n"
                        f"- polars has NO built-in indicator functions (no pl.rsi, pl.macd, pl.ta, etc.)\n"
                        f"- numpy is NOT available — do NOT use np or numpy in any way\n"
                        f"- Only `pl` (polars) and `math` are available\n"
                        f"- Compute RSI, MACD, Bollinger Bands, etc. from scratch using:\n"
                        f"  pl.col().diff(), .clip(), .rolling().mean(), .ewm_mean(), .shift(), .abs()\n"
                        f"- Follow the exact indicator recipes from the system prompt"
                    )

            if not error_msg:
                code_validated = True
                break

            # Retry with error context
            logger.warning(
                "lab_strategy_code_failed",
                attempt=attempt + 1,
                error=error_msg[:200],
            )
            if attempt < MAX_CODE_RETRIES - 1:
                yield f"data: {json.dumps({'type': 'status', 'status': 'retrying'})}\n\n"
                retry_context = (
                    context + f"\n\n{error_msg}\nPlease fix the strategy code."
                )
                try:
                    retry_result = await asyncio.wait_for(
                        architect.run(retry_context), timeout=AI_TIMEOUT_SECONDS
                    )
                    output = retry_result.output
                except asyncio.TimeoutError:
                    logger.error("lab_architect_retry_timeout", provider=request.provider)
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Code fix retry timed out'})}\n\n"
                    return
                except Exception as retry_e:
                    logger.error("lab_architect_retry_failed", error=str(retry_e))
                    yield f"data: {json.dumps({'type': 'error', 'error': f'Code fix retry failed: {retry_e}'})}\n\n"
                    return
            else:
                logger.error("lab_strategy_code_max_retries", error=error_msg[:200])
                yield f"data: {json.dumps({'type': 'error', 'error': f'Strategy code failed validation after {MAX_CODE_RETRIES} attempts. Please rephrase your strategy.'})}\n\n"
                return

    # 2c. Auto-correct completeness: if code exists and validated, ensure >= 75
    if output.strategy and code_validated and output.strategy.completeness < 75:
        logger.warning(
            "lab_architect_completeness_autocorrected",
            original=output.strategy.completeness,
        )
        output.strategy.completeness = 75

    # 3. Stream the response content
    response_text = output.response
    chunk_size = 10
    for i in range(0, len(response_text), chunk_size):
        chunk = response_text[i : i + chunk_size]
        yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"

    # 4. Send strategy update if available
    if output.strategy:
        strategy_data = output.strategy.model_dump()
        strategy_data["codeValidated"] = code_validated
        yield f"data: {json.dumps({'type': 'strategy', 'strategy': strategy_data, 'suggestBacktest': output.suggestBacktest})}\n\n"

    # 5. Send done event
    strategy_done = None
    if output.strategy:
        strategy_done = output.strategy.model_dump()
        strategy_done["codeValidated"] = code_validated
    yield f"data: {json.dumps({'type': 'done', 'strategy': strategy_done, 'suggestBacktest': output.suggestBacktest})}\n\n"

    logger.info(
        "lab_architect_complete",
        has_strategy=output.strategy is not None,
        completeness=output.strategy.completeness if output.strategy else 0,
        suggest_backtest=output.suggestBacktest,
        code_validated=code_validated,
    )
