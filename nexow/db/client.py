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
