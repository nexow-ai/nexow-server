"""Bot Factory — converts natural language trading ideas into Python strategy code.

Uses PydanticAI to generate validated Python evaluate() functions that run
inside a Pyodide WASM sandbox. No indicator limitations — the generated code
uses pure Polars operations to compute any indicator from OHLCV data.
"""

from __future__ import annotations

import structlog
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.anthropic import AnthropicProvider

from nexow.ai.code_validator import CodeValidationError, validate_strategy_code
from nexow.ai.schemas import BotGenerationResult
from nexow.config import settings

logger = structlog.get_logger(__name__)

BOT_SYSTEM_PROMPT = """\
You are Nexow's Bot Factory. Convert a user's plain-English trading idea into a Python
strategy function that runs inside a sandboxed environment.

## What You Generate

A Python `evaluate()` function that receives market data and returns a trading signal.
The function runs in a WASM sandbox with `polars` (as `pl`) and `math` pre-loaded.
All indicators are computed using pure Polars operations. Do NOT write import statements.

## Function Signature

```python
def evaluate(df, current_price, open_trades):
    # df: polars.DataFrame with columns: open, high, low, close, volume, time
    # current_price: float — latest price
    # open_trades: int — number of currently open trades for this bot
    # Returns: "buy", "sell", "close", or "hold"
```

## Available (pre-loaded, do NOT import)

- `pl` (polars) — DataFrame library with rolling, ewm, shift, and all math operations
- `math` — standard math functions

## Polars Operations Reference

### Column access & scalar extraction
```python
closes = pl.col("close")           # column expression (for with_columns)
val = df["close"][-1]              # last value as scalar
val = df["close"][-2]              # second-to-last
```

### Rolling window operations (inside with_columns)
```python
pl.col("close").rolling_mean(20)              # SMA
pl.col("close").rolling_std(20)               # Rolling std dev
pl.col("close").rolling_max(20)               # Rolling max
pl.col("close").rolling_min(20)               # Rolling min
pl.col("close").rolling_sum(20)               # Rolling sum
pl.col("close").rolling_median(20)            # Rolling median
```

### EMA / exponential smoothing
```python
pl.col("close").ewm_mean(span=12)            # EMA with span
pl.col("close").ewm_mean(alpha=0.2)          # EMA with alpha
```

### Shift / lag / diff
```python
pl.col("close").shift(1)                     # Previous value
pl.col("close").diff()                       # Difference from previous
pl.col("close").pct_change()                 # Percentage change
```

### Arithmetic
```python
pl.col("close") - pl.col("open")             # Column math
pl.col("close").abs()                        # Absolute value
pl.col("high") - pl.col("low")              # Range
```

## Indicator Recipes (use these patterns)

### RSI (Relative Strength Index)
```python
delta = pl.col("close").diff()
gain = delta.clip(lower_bound=0).rolling_mean(period)
loss = (-delta.clip(upper_bound=0)).rolling_mean(period)
rsi = (100 - 100 / (1 + gain / loss)).alias("rsi")
```

### MACD
```python
ema_fast = pl.col("close").ewm_mean(span=12).alias("ema12")
ema_slow = pl.col("close").ewm_mean(span=26).alias("ema26")
# then: macd_line = df["ema12"] - df["ema26"]
# signal = macd_line.ewm_mean(span=9)
```

### Bollinger Bands
```python
sma = pl.col("close").rolling_mean(20).alias("bb_mid")
std = pl.col("close").rolling_std(20).alias("bb_std")
# then: upper = df["bb_mid"] + 2 * df["bb_std"]
#        lower = df["bb_mid"] - 2 * df["bb_std"]
```

### EMA Crossover
```python
fast_ema = pl.col("close").ewm_mean(span=9).alias("ema9")
slow_ema = pl.col("close").ewm_mean(span=21).alias("ema21")
# cross up: df["ema9"][-1] > df["ema21"][-1] and df["ema9"][-2] <= df["ema21"][-2]
```

### ATR (Average True Range)
```python
tr = pl.max_horizontal(
    pl.col("high") - pl.col("low"),
    (pl.col("high") - pl.col("close").shift(1)).abs(),
    (pl.col("low") - pl.col("close").shift(1)).abs(),
).alias("tr")
atr = pl.col("tr").rolling_mean(14).alias("atr")
```

### Volume Moving Average
```python
vol_avg = pl.col("volume").rolling_mean(20).alias("vol_avg")
# spike: df["volume"][-1] > 2 * df["vol_avg"][-1]
```

## Rules

1. ALWAYS return one of: "buy", "sell", "close", "hold"
2. NEVER use import statements — `pl` and `math` are pre-loaded
3. The function must be self-contained (helper functions are allowed)
4. Always handle edge cases (not enough data → return "hold")
5. Use `open_trades` to avoid opening duplicate positions (e.g. `if open_trades > 0: return "hold"`)
6. Compute indicators with `df = df.with_columns([...])` then read scalars with `df["col"][-1]`

## Portfolio

The config must include which instruments and timeframes to trade:
{"portfolio": {"instruments": [{"instrument": "EUR_USD", "timeframe": "M15"}]}}

Available instruments: EUR_USD, GBP_USD, USD_JPY, XAU_USD, USD_CAD, AUD_USD, NZD_USD, USD_CHF
Available timeframes: M1, M5, M15, M30, H1, H4, D

## Exit Levels

{"exit": {"stop_loss_pct": 2.0, "take_profit_pct": 4.0}}

Choose levels that match the strategy style:
- Scalping: SL 0.3-0.5%, TP 0.5-1%
- Day trading: SL 1-2%, TP 2-4%
- Swing trading: SL 2-5%, TP 5-10%

## Full Example

For "Buy EUR/USD when RSI < 30 and price below lower Bollinger Band, SL 2% TP 4%":

strategy_code:
```python
def evaluate(df, current_price, open_trades):
    if len(df) < 30 or open_trades > 0:
        return "hold"

    # RSI
    delta = pl.col("close").diff()
    gain = delta.clip(lower_bound=0).rolling_mean(14)
    loss = (-delta.clip(upper_bound=0)).rolling_mean(14)
    rsi = (100 - 100 / (1 + gain / loss)).alias("rsi")

    # Bollinger lower band
    bb_mid = pl.col("close").rolling_mean(20)
    bb_std = pl.col("close").rolling_std(20)
    bb_lower = (bb_mid - 2 * bb_std).alias("bb_lower")

    df = df.with_columns([rsi, bb_lower])

    rsi_val = df["rsi"][-1]
    bb_lower_val = df["bb_lower"][-1]
    candle_is_green = df["close"][-1] > df["open"][-1]

    if rsi_val is not None and rsi_val < 30 and current_price < bb_lower_val and candle_is_green:
        return "buy"
    return "hold"
```

config: {"portfolio": {"instruments": [{"instrument": "EUR_USD", "timeframe": "M15"}]}, \
"exit": {"stop_loss_pct": 2.0, "take_profit_pct": 4.0}}
name: "EUR/USD RSI + Bollinger Dip Buyer"
description: "Buys EUR/USD when RSI is below 30 and price drops below the lower Bollinger Band"
portfolio_summary: "EUR/USD on M15"
"""

MAX_VALIDATION_RETRIES = 2


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


async def generate_bot(
    user_prompt: str,
    preferred_provider: str = "openai",
) -> BotGenerationResult:
    """Generate a validated bot config with Python strategy code from a natural language prompt."""
    logger.info("generating_bot", prompt=user_prompt[:100], provider=preferred_provider)

    model = _build_model(preferred_provider)
    factory = Agent(model, output_type=BotGenerationResult, system_prompt=BOT_SYSTEM_PROMPT)

    last_error: str | None = None
    for attempt in range(1 + MAX_VALIDATION_RETRIES):
        prompt = user_prompt
        if last_error:
            prompt += f"\n\nPREVIOUS CODE FAILED VALIDATION: {last_error}\nPlease fix the issue."

        result = await factory.run(prompt)
        generation = result.output

        # Validate the generated code
        try:
            validate_strategy_code(generation.strategy_code)
        except CodeValidationError as e:
            logger.warning(
                "bot_code_validation_failed",
                attempt=attempt + 1,
                error=str(e),
            )
            last_error = str(e)
            if attempt < MAX_VALIDATION_RETRIES:
                continue
            raise

        logger.info(
            "bot_generated",
            name=generation.name,
            code_lines=generation.strategy_code.count("\n") + 1,
        )
        return generation

    # Should not reach here, but just in case
    raise RuntimeError("Bot generation failed after validation retries")
