"""Application settings.

Single source of truth for every environment variable the backend reads.
Owned by A0 — frozen after Wave 0. Import the module-level `settings`
singleton; never re-instantiate `Settings()` elsewhere.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Reads from the environment (and `.env` when running outside Docker).

    Field names are lowercase snake_case; pydantic-settings matches them to
    upper-case env vars case-insensitively (DATABASE_URL -> database_url).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database -----------------------------------------------------
    database_url: str = "postgresql+asyncpg://hr:hr@localhost:5432/hrdb"

    # --- LLM provider / throttling -------------------------------------
    # Default is "mock" here (not "openrouter") so that anything run
    # outside docker-compose (bare pytest, a stray script) never
    # accidentally dials a real provider. docker-compose.yml supplies the
    # real default (openrouter) as an env var for the containerized run.
    llm_provider: str = "mock"
    llm_fallback_provider: str = "groq"
    llm_rpm: int = 18
    llm_daily_budget: int = 45
    eval_concurrency: int = 2
    rag_rerank: bool = False
    embedding_provider: str = "fastembed"

    # --- Provider API keys (optional; presence gates reachability) -----
    openrouter_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    cerebras_api_key: str | None = None

    # --- Misc ------------------------------------------------------------
    app_url: str = "http://localhost:5173"
    env: str = "dev"
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
