"""Market data endpoints — prices, candles, instruments, economic calendar."""

from datetime import date, datetime

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from nexow.broker.oanda import OandaClient
from nexow.config import settings
from nexow.db.client import SupabaseClient

router = APIRouter(prefix="/api/data", tags=["data"])

_oanda: OandaClient | None = None


def get_oanda() -> OandaClient:
    global _oanda
    if _oanda is None:
        _oanda = OandaClient()
    return _oanda


@router.get("/prices/{instrument}")
async def get_price(instrument: str):
    """Get current bid/ask/mid price for an instrument."""
    try:
        oanda = get_oanda()
        url = f"{oanda.account_url}/pricing"
        params = {"instruments": instrument}
        resp = await oanda._http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        price_data = data["prices"][0]
        bid = float(price_data["bids"][0]["price"])
        ask = float(price_data["asks"][0]["price"])
        mid = (bid + ask) / 2
        return {"instrument": instrument, "bid": bid, "ask": ask, "mid": mid,
                "time": int(datetime.fromisoformat(price_data["time"].replace("Z", "+00:00")).timestamp())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prices")
async def get_prices(instruments: str):
    """Get current prices for multiple instruments (comma-separated)."""
    try:
        instrument_list = [i.strip() for i in instruments.split(",")]
        prices = await get_oanda().get_prices(instrument_list)
        return {"prices": prices, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CandlesRequest(BaseModel):
    instrument: str
    granularity: str = "M5"
    count: int = 100


class CandlesRangeRequest(BaseModel):
    instrument: str
    granularity: str
    from_time: str
    to_time: str


@router.post("/candles")
async def get_candles(request: CandlesRequest):
    """Fetch recent candles for an instrument."""
    try:
        candles = await get_oanda().get_candles(
            instrument=request.instrument, granularity=request.granularity, count=request.count,
        )
        return {"instrument": request.instrument, "granularity": request.granularity,
                "candles": [c.model_dump() for c in candles]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/candles/range")
async def get_candles_range(request: CandlesRangeRequest):
    """Fetch candles for a date range."""
    try:
        from_time = datetime.fromisoformat(request.from_time)
        to_time = datetime.fromisoformat(request.to_time)
        candles = await get_oanda().get_candles_range(
            instrument=request.instrument, granularity=request.granularity,
            from_time=from_time, to_time=to_time,
        )
        return {"instrument": request.instrument, "granularity": request.granularity,
                "from_time": request.from_time, "to_time": request.to_time,
                "candles": [c.model_dump() for c in candles], "count": len(candles)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candles")
async def get_candles_get(
    instrument: str = Query(default="EUR_USD"),
    granularity: str = Query(default="M5"),
    count: int = Query(default=200),
):
    """Fetch recent candles (GET variant for frontend proxy)."""
    try:
        candles = await get_oanda().get_candles(
            instrument=instrument, granularity=granularity, count=count,
        )
        return {
            "instrument": instrument,
            "candles": [
                {
                    "time": int(c.time.timestamp()),
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in candles
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/balance")
async def get_balance():
    """Get Oanda account balance."""
    try:
        balance = await get_oanda().get_balance()
        return {"balance": balance, "currency": "USD"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/economic-calendar")
async def get_economic_calendar(
    target_date: str | None = Query(default=None, description="Date YYYY-MM-DD (default: today)"),
    currency: str | None = Query(default=None, description="Filter by currency (e.g. USD)"),
):
    """Get economic events scraped from Forex Factory."""
    try:
        db = SupabaseClient()
        if target_date is None:
            target_date = date.today().isoformat()
        events = db.get_economic_events(target_date=target_date, currency=currency)
        return {"date": target_date, "currency": currency, "events": events, "count": len(events)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/snapshot/{instrument}")
async def get_snapshot(instrument: str):
    """Get the latest market snapshot for an instrument.

    Tries Redis cache first; if empty, computes on-the-fly from DB data.
    """
    try:
        from nexow.snapshot.redis_store import SnapshotRedisStore

        store = SnapshotRedisStore()
        await store.connect()
        snapshot = await store.get_snapshot(instrument)
        await store.close()

        if snapshot is not None:
            return snapshot

        # Fallback: compute on-the-fly
        from nexow.snapshot.service import SnapshotService

        svc = SnapshotService()
        await svc._load_history(instrument)
        df = svc._dataframes.get(instrument)
        if df is None or df.height < 50:
            raise HTTPException(status_code=404, detail="Not enough price data to build snapshot.")
        result = svc._build_snapshot(instrument, df)
        return result.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/instruments")
async def get_instruments():
    """Get all available instruments from Oanda (raw)."""
    try:
        oanda = get_oanda()
        url = f"{oanda.account_url}/instruments"
        resp = await oanda._http.get(url)
        resp.raise_for_status()
        data = resp.json()
        return {"instruments": data.get("instruments", [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
