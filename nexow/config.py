"""Unified configuration — single settings file for the entire backend."""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Service ---
    port: int = 8000
    environment: str = "development"

    # --- Supabase ---
    supabase_url: str = ""
    supabase_secret_key: str = ""

    # --- Oanda ---
    oanda_api_url: str = "https://api-fxpractice.oanda.com"
    oanda_account_id: str = ""
    oanda_api_token: str = ""

    # --- Redis ---
    redis_url: str = "redis://localhost:6379"
    redis_channel: str = "nexow:market:prices"

    # --- LLM Providers ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    deepseek_api_key: str = ""

    # --- External Data ---
    tavily_api_key: str = ""
    newsapi_key: str = ""

    # --- Market Data Poller ---
    poll_interval_seconds: int = 5
    default_instruments: list[str] = ["EUR_USD", "GBP_USD", "USD_JPY"]

    # --- Worker ---
    tick_interval_seconds: int = 5
    pending_check_interval_seconds: int = 10
    max_concurrent_evaluations: int = 20
    claim_lock_min_ttl_seconds: int = 60

    # --- WASM Sandbox ---
    sandbox_url: str = Field(
        default="http://localhost:3001",
        validation_alias=AliasChoices("SANDBOX_URL", "WASM_EXECUTOR_URL"),
    )

    # --- CORS ---
    cors_origins: list[str] = ["*"]


settings = Settings()
