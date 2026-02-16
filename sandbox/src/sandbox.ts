/**
 * Sandboxed code execution via Pyodide WASM.
 *
 * Injects candle data as a Polars DataFrame, runs the user's evaluate()
 * function, and captures the result. Enforces timeout.
 */

import type { PyodideInterface } from "pyodide";

export interface ExecuteRequest {
  code: string;
  candles: Array<Record<string, unknown>>;
  current_price: number;
  open_trade_count: number;
  timeout_ms: number;
}

export interface ExecuteResult {
  action: "buy" | "sell" | "close" | "hold";
  error: string | null;
}

const VALID_ACTIONS = new Set(["buy", "sell", "close", "hold"]);

export async function executeSandboxed(
  py: PyodideInterface,
  req: ExecuteRequest
): Promise<ExecuteResult> {
  const { code, candles, current_price, open_trade_count, timeout_ms } = req;

  // Build a timeout race
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);

  try {
    const result = await Promise.race([
      runInPyodide(py, code, candles, current_price, open_trade_count),
      abortPromise(controller.signal),
    ]);

    return result;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);

    if (message === "TIMEOUT") {
      return { action: "hold", error: `Execution timed out after ${timeout_ms}ms` };
    }

    return { action: "hold", error: message };
  } finally {
    clearTimeout(timer);
  }
}

async function runInPyodide(
  py: PyodideInterface,
  code: string,
  candles: Array<Record<string, unknown>>,
  currentPrice: number,
  openTradeCount: number
): Promise<ExecuteResult> {
  // Convert candles to JSON string for Pyodide ingestion
  const candlesJson = JSON.stringify(candles);

  // Build the execution script:
  // 1. Load candle data into a Polars DataFrame
  // 2. Define the user's evaluate function
  // 3. Call it and capture the result
  const script = `
import json as _json

# Load candle data as Polars DataFrame
_candles_raw = _json.loads('''${candlesJson.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}''')
_df = pl.DataFrame(_candles_raw)

# Define user strategy
${code}

# Execute
_result = evaluate(_df, ${currentPrice}, ${openTradeCount})
str(_result).lower().strip()
`;

  const rawResult = py.runPython(script);
  const action = String(rawResult).toLowerCase().trim();

  if (!VALID_ACTIONS.has(action)) {
    return {
      action: "hold",
      error: `evaluate() returned '${action}', expected buy/sell/close/hold`,
    };
  }

  return { action: action as ExecuteResult["action"], error: null };
}

function abortPromise(signal: AbortSignal): Promise<never> {
  return new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new Error("TIMEOUT")));
  });
}
