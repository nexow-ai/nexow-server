/**
 * WASM Sandbox Sidecar — Fastify HTTP server.
 *
 * Exposes:
 *   POST /execute  — run a Python evaluate() function in a Pyodide sandbox
 *   GET  /health   — pool status
 */

import Fastify from "fastify";
import { PyodidePool } from "./pool.js";
import { executeSandboxed, type ExecuteRequest } from "./sandbox.js";

const PORT = parseInt(process.env.SANDBOX_PORT ?? "3001", 10);
const POOL_SIZE = parseInt(process.env.SANDBOX_POOL_SIZE ?? "4", 10);
const DEFAULT_TIMEOUT_MS = 5000;

// ── Pyodide pool ──────────────────────────────────────────

const pool = new PyodidePool({
  size: POOL_SIZE,
  packages: ["polars"],
});

// ── Fastify server ────────────────────────────────────────

const app = Fastify({ logger: true });

// Health check
app.get("/health", async () => {
  return { ok: pool.status.ready, pool: pool.status };
});

// Execute strategy code
app.post<{ Body: ExecuteRequest }>("/execute", async (request, reply) => {
  if (!pool.status.ready) {
    return reply.status(503).send({
      action: "hold",
      error: "Sandbox warming up. Try again shortly.",
    });
  }

  const body = request.body;

  if (!body.code || !body.candles || body.current_price == null) {
    return reply.status(400).send({
      action: "hold",
      error: "Missing required fields: code, candles, current_price",
    });
  }

  const req: ExecuteRequest = {
    code: body.code,
    candles: body.candles,
    current_price: body.current_price,
    open_trade_count: body.open_trade_count ?? 0,
    timeout_ms: body.timeout_ms ?? DEFAULT_TIMEOUT_MS,
  };

  const py = await pool.acquire();
  try {
    const result = await executeSandboxed(py, req);
    return result;
  } finally {
    pool.release(py);
  }
});

// ── Start ─────────────────────────────────────────────────

async function main() {
  await app.listen({ port: PORT, host: "0.0.0.0" });
  console.log(`[sandbox] Listening on http://0.0.0.0:${PORT}`);

  // Warm the pool in the background so health checks come up immediately.
  console.log("[sandbox] Initializing Pyodide pool...");
  pool.init().catch((err) => {
    console.error("[sandbox] Pool init failed:", err);
    process.exit(1);
  });
}

main().catch((err) => {
  console.error("[sandbox] Fatal error:", err);
  process.exit(1);
});
