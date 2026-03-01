"""Price ingestion — Massive flat files (source of truth) + Oanda real-time fill."""

from __future__ import annotations

import gzip
from datetime import date, datetime, timezone
from typing import Any

import boto3
import structlog
from botocore.config import Config

from nexow.broker.oanda import OandaClient
from nexow.config import settings

logger = structlog.get_logger(__name__)

# Mapping from Oanda-style to Massive ticker format
INSTRUMENT_TO_MASSIVE: dict[str, str] = {
    "EUR_USD": "C:EUR-USD",
    "GBP_USD": "C:GBP-USD",
    "USD_JPY": "C:USD-JPY",
    "USD_CAD": "C:USD-CAD",
    "AUD_USD": "C:AUD-USD",
    "NZD_USD": "C:NZD-USD",
    "USD_CHF": "C:USD-CHF",
    "XAU_USD": "C:XAU-USD",
}


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.massive_s3_endpoint,
        aws_access_key_id=settings.massive_s3_access_key_id,
        aws_secret_access_key=settings.massive_s3_secret_access_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )


def download_minute_aggs(target_date: date, instrument: str = "EUR_USD") -> list[dict[str, Any]]:
    """Download a day's minute aggregates from Massive S3 flat files.

    Returns list of dicts with keys: instrument, ts, open, high, low, close, volume, transactions.
    """
    massive_ticker = INSTRUMENT_TO_MASSIVE.get(instrument, f"C:{instrument.replace('_', '-')}")
    key = f"global_forex/minute_aggs_v1/{target_date.year}/{target_date.month:02d}/{target_date.isoformat()}.csv.gz"

    logger.info("massive_download_start", key=key, instrument=instrument)

    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=settings.massive_s3_bucket, Key=key)
        raw = obj["Body"].read()
    except Exception as e:
        logger.error("massive_download_failed", key=key, error=str(e))
        return []

    try:
        csv_data = gzip.decompress(raw).decode("utf-8")
    except Exception as e:
        logger.error("massive_decompress_failed", error=str(e))
        return []

    rows: list[dict[str, Any]] = []
    lines = csv_data.split("\n")

    # header: ticker,volume,open,close,high,low,window_start,transactions
    for line in lines[1:]:
        if not line or not line.startswith(massive_ticker):
            continue

        parts = line.split(",")
        if len(parts) < 8:
            continue

        # window_start is nanoseconds epoch
        ts_ns = int(parts[6])
        ts = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)

        rows.append({
            "instrument": instrument,
            "ts": ts.isoformat(),
            "open": float(parts[2]),
            "high": float(parts[4]),
            "low": float(parts[5]),
            "close": float(parts[3]),
            "volume": int(parts[1]),
            "transactions": int(parts[7]),
            "source": "massive",
        })

    logger.info("massive_download_done", instrument=instrument, date=target_date.isoformat(), rows=len(rows))
    return rows


async def fetch_oanda_minute_bars(
    instrument: str,
    from_time: datetime,
    oanda: OandaClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch M1 candles from Oanda, from `from_time` until now.

    Returns DB-ready dicts with source='oanda'.
    """
    _oanda = oanda or OandaClient()
    now = datetime.now(timezone.utc)

    try:
        candles = await _oanda.get_candles_range(
            instrument=instrument,
            granularity="M1",
            from_time=from_time,
            to_time=now,
        )
    except Exception as e:
        logger.error("oanda_fetch_failed", instrument=instrument, error=str(e))
        return []
    finally:
        if oanda is None:
            await _oanda.close()

    rows = [
        {
            "instrument": instrument,
            "ts": c.time.isoformat(),
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "transactions": 0,
            "source": "oanda",
        }
        for c in candles
    ]

    logger.info("oanda_fetch_done", instrument=instrument, rows=len(rows))
    return rows
