"""Application settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # security
    app_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    cookie_secure: bool = True

    # storage
    database_path: Path = Path("/data/veille.db")

    # services
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:8b"
    ollama_embed_model: str = "bge-m3"
    searxng_url: str = "http://searxng:8080"

    # scheduling
    run_at: time = time(5, 30)
    timezone: str = "Europe/Paris"

    # pipeline tuning
    max_items_per_theme: int = Field(default=8, ge=1, le=50)
    relevance_threshold: float = Field(default=0.45, ge=-1.0, le=1.0)
    dedupe_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    lookback_hours: int = Field(default=36, ge=1, le=24 * 14)
    llm_timeout_s: float = Field(default=180.0, gt=0)
    keyword_suggest_timeout_s: float = Field(default=60.0, gt=0)
    max_keywords_queries: int = 5
    searxng_results_per_query: int = 10
    # '' (default) = no SearXNG time filter; 'day' | 'week' | 'month' | 'year' otherwise.
    searxng_time_range: Literal["", "day", "week", "month", "year"] = ""
    http_timeout_s: float = 15.0
    max_page_bytes: int = 2 * 1024 * 1024
    content_max_chars: int = 3000
    content_retention_days: int = 30
    user_agent: str = "VeilleJournal/1.0 (+personal news digest; contact: owner)"

    # web
    web_host: str = "0.0.0.0"  # noqa: S104 - inside the container; host bind is 127.0.0.1
    web_port: int = 8080
    # Addresses/CIDRs whose X-Forwarded-For / X-Forwarded-Proto headers uvicorn trusts.
    # Never "*": anyone could then spoof their IP and dodge the login rate limit. In docker
    # the reverse proxy arrives from a docker network, e.g. "172.16.0.0/12" (see .env.example).
    forwarded_allow_ips: str = "127.0.0.1"

    # misc
    log_level: str = "INFO"
    version: str = "dev"

    @field_validator("run_at", mode="before")
    @classmethod
    def _parse_run_at(cls, value: object) -> object:
        if isinstance(value, str):
            hours, _, minutes = value.strip().partition(":")
            return time(int(hours), int(minutes or 0))
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if unknown
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
