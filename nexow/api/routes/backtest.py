"""Backtest endpoints — run historical simulations with SSE streaming."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from nexow.backtest.engine import BacktestEngine

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    config: dict
    instruments: list[dict]
    exit_config: dict = {}
    period_start: str
    period_end: str


@router.post("")
async def run_backtest(request: BacktestRequest):
    """Run a backtest and stream progress via SSE."""
    try:
        period_start = datetime.fromisoformat(request.period_start)
        period_end = datetime.fromisoformat(request.period_end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    engine = BacktestEngine()

    async def event_stream():
        async for update in engine.run(
            config=request.config,
            instruments=request.instruments,
            exit_config=request.exit_config,
            period_start=period_start,
            period_end=period_end,
        ):
            data = {
                "phase": update.phase,
                "progress_pct": update.progress_pct,
                "message": update.message,
            }

            if update.equity_curve:
                data["equity_curve"] = [
                    {"time": p.time, "equity": p.equity} for p in update.equity_curve
                ]

            if update.result:
                r = update.result
                data["result"] = {
                    "stats": {
                        "total_trades": r.stats.total_trades,
                        "winning_trades": r.stats.winning_trades,
                        "losing_trades": r.stats.losing_trades,
                        "win_rate": r.stats.win_rate,
                        "total_return_pct": r.stats.total_return_pct,
                        "avg_return_pct": r.stats.avg_return_pct,
                        "max_drawdown": r.stats.max_drawdown,
                        "sharpe_ratio": r.stats.sharpe_ratio,
                        "profit_factor": r.stats.profit_factor,
                        "best_trade_pct": r.stats.best_trade_pct,
                        "worst_trade_pct": r.stats.worst_trade_pct,
                        "avg_trade_duration_hours": r.stats.avg_trade_duration_hours,
                    },
                    "trades": [
                        {
                            "instrument": t.instrument,
                            "direction": t.direction,
                            "entry_price": t.entry_price,
                            "entry_time": t.entry_time,
                            "exit_price": t.exit_price,
                            "exit_time": t.exit_time,
                            "return_pct": t.return_pct,
                        }
                        for t in r.trades
                    ],
                    "equity_curve": [
                        {"time": p.time, "equity": p.equity} for p in r.equity_curve
                    ],
                }

            yield f"data: {json.dumps(data)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
