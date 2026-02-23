"""Supabase client wrapper (service-role access)."""

from __future__ import annotations

from typing import Any

import structlog
from supabase import Client, create_client

from nexow.config import settings

logger = structlog.get_logger(__name__)


class SupabaseClient:
    """Thin wrapper around the Supabase Python client."""

    def __init__(self) -> None:
        self._client: Client = create_client(
            settings.supabase_url,
            settings.supabase_secret_key,
        )

    @property
    def client(self) -> Client:
        return self._client

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def get_active_agents(self) -> list[dict[str, Any]]:
        """Fetch all agents with status='active'."""
        response = (
            self._client.table("agents")
            .select("*")
            .eq("status", "active")
            .execute()
        )
        return response.data

    def insert_agent_log(
        self, agent_id: str, level: str, message: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Insert a log entry for the agent console (real-time display)."""
        self._client.table("agent_logs").insert(
            {
                "agent_id": agent_id,
                "level": level,
                "message": message,
                "metadata": metadata or {},
            }
        ).execute()

    def get_agent_by_id(self, agent_id: str) -> dict[str, Any] | None:
        """Fetch a single agent by ID."""
        response = (
            self._client.table("agents")
            .select("*")
            .eq("id", agent_id)
            .single()
            .execute()
        )
        return response.data

    def update_agent_status(self, agent_id: str, status: str) -> None:
        """Update an agent's status (active, paused, killed)."""
        self._client.table("agents").update({"status": status}).eq("id", agent_id).execute()

    def update_agent_config(self, agent_id: str, config: dict[str, Any]) -> None:
        """Write the generated strategy config back to the agent."""
        self._client.table("agents").update({"config": config}).eq("id", agent_id).execute()

    # ------------------------------------------------------------------
    # Agent Evaluations (agent-only reasoning records)
    # ------------------------------------------------------------------

    def insert_evaluation(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        """Insert an agent evaluation record (reasoning cycle)."""
        response = self._client.table("agent_evaluations").insert(evaluation).execute()
        return response.data[0]

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def insert_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        """Insert a new trade record (entry signal)."""
        response = self._client.table("trades").insert(trade).execute()
        return response.data[0]

    def close_trade(self, trade_id: str, exit_price: float, return_pct: float) -> None:
        """Close an open trade with exit price and gross return %."""
        from datetime import datetime, timezone

        self._client.table("trades").update(
            {
                "status": "closed",
                "exit_price": exit_price,
                "return_pct": return_pct,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", trade_id).execute()

    def get_open_trades(self, agent_id: str) -> list[dict[str, Any]]:
        """Fetch open trades for a given agent."""
        response = (
            self._client.table("trades")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("status", "open")
            .execute()
        )
        return response.data

    def get_all_open_trades(self) -> list[dict[str, Any]]:
        """Fetch all open trades across all agents (for SL/TP sync)."""
        response = (
            self._client.table("trades")
            .select("*")
            .eq("status", "open")
            .execute()
        )
        return response.data or []

    # ------------------------------------------------------------------
    # Performance
    # ------------------------------------------------------------------

    def upsert_performance(self, perf: dict[str, Any]) -> None:
        """Upsert agent performance stats."""
        self._client.table("agent_performance").upsert(perf).execute()

    # ------------------------------------------------------------------
    # Copy subscriptions
    # ------------------------------------------------------------------

    def get_active_copiers(self, agent_id: str) -> list[dict[str, Any]]:
        """Get all active copy subscribers for an agent."""
        response = (
            self._client.table("copy_subscriptions")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("status", "active")
            .execute()
        )
        return response.data

    # ------------------------------------------------------------------
    # Economic Events (Forex Factory calendar)
    # ------------------------------------------------------------------

    def upsert_economic_event(self, event: dict[str, Any]) -> None:
        """Upsert an economic event (deduplicates on date+currency+event)."""
        self._client.table("economic_events").upsert(
            event, on_conflict="date,currency,event"
        ).execute()

    def get_economic_events(self, target_date: str | None = None, currency: str | None = None) -> list[dict[str, Any]]:
        """Fetch economic events, optionally filtered by date and/or currency."""
        query = self._client.table("economic_events").select("*")
        if target_date:
            query = query.eq("date", target_date)
        if currency:
            query = query.eq("currency", currency)
        query = query.order("date", desc=True).order("time")
        return query.execute().data

    # ------------------------------------------------------------------
    # Forex Prices 1m (Massive flat files)
    # ------------------------------------------------------------------

    def upsert_forex_prices(self, rows: list[dict[str, Any]]) -> None:
        """Bulk upsert minute price bars (deduplicates on instrument+ts)."""
        self._client.table("forex_prices_1m").upsert(
            rows, on_conflict="instrument,ts"
        ).execute()

    def delete_oanda_prices(self, instrument: str, target_date: str) -> None:
        """Delete all oanda-sourced rows for a given instrument and date.

        Used when a Massive flat file arrives to replace provisional Oanda data.
        """
        # Delete where source=oanda AND ts falls within the target date
        start = f"{target_date}T00:00:00+00:00"
        end = f"{target_date}T23:59:59+00:00"
        self._client.table("forex_prices_1m") \
            .delete() \
            .eq("instrument", instrument) \
            .eq("source", "oanda") \
            .gte("ts", start) \
            .lte("ts", end) \
            .execute()

    def get_latest_price_ts(self, instrument: str) -> str | None:
        """Get the timestamp of the most recent price bar for an instrument."""
        resp = (
            self._client.table("forex_prices_1m")
            .select("ts")
            .eq("instrument", instrument)
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]["ts"]
        return None

    def get_forex_prices(
        self, instrument: str, from_ts: str | None = None, to_ts: str | None = None, limit: int = 1440,
    ) -> list[dict[str, Any]]:
        """Fetch minute price bars for an instrument."""
        query = self._client.table("forex_prices_1m").select("*").eq("instrument", instrument)
        if from_ts:
            query = query.gte("ts", from_ts)
        if to_ts:
            query = query.lte("ts", to_ts)
        query = query.order("ts", desc=True).limit(limit)
        return query.execute().data

    def get_forex_prices_bulk(
        self, instrument: str, from_ts: str, batch_size: int = 5000,
    ) -> list[dict[str, Any]]:
        """Fetch all M1 bars from from_ts, paginating in batches. Ordered ASC."""
        all_rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = (
                self._client.table("forex_prices_1m")
                .select("ts,open,high,low,close,volume")
                .eq("instrument", instrument)
                .gte("ts", from_ts)
                .order("ts")
                .range(offset, offset + batch_size - 1)
                .execute()
            )
            all_rows.extend(resp.data)
            if len(resp.data) < batch_size:
                break
            offset += batch_size
        return all_rows

    # ------------------------------------------------------------------
    # Snapshot Analyses (LLM scoring)
    # ------------------------------------------------------------------

    def upsert_snapshot_analysis(self, analysis: dict[str, Any]) -> None:
        """Upsert a snapshot analysis (deduplicates on instrument+timestamp)."""
        self._client.table("snapshot_analyses").upsert(
            analysis, on_conflict="instrument,timestamp"
        ).execute()

    def get_latest_analysis(self, instrument: str) -> dict[str, Any] | None:
        """Get the most recent analysis for an instrument."""
        resp = (
            self._client.table("snapshot_analyses")
            .select("*")
            .eq("instrument", instrument)
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    # ------------------------------------------------------------------
    # Agent prompts (pending generation)
    # ------------------------------------------------------------------

    def get_pending_agents(self) -> list[dict[str, Any]]:
        """Fetch agents that have a prompt but status='paused' and empty config."""
        response = (
            self._client.table("agents")
            .select("*")
            .not_("prompt", "is", "null")
            .eq("status", "paused")
            .execute()
        )
        return [
            a for a in response.data
            if not a.get("config") or a["config"] == {} or a["config"] == "{}"
        ]
