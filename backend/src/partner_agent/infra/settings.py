from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PARTNER_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    environment: Literal["development", "production", "test"] = "development"
    database_url: str
    public_origin: str = "http://localhost:8080"
    qdrant_url: str = "http://qdrant:6333"
    s3_endpoint_url: str = "http://garage:3900"
    s3_region: str = "garage"
    s3_access_key_id: str = ""
    s3_secret_access_key: SecretStr = SecretStr("")
    s3_originals_bucket: str = "originals"
    s3_derived_bucket: str = "derived"
    anonymous_ttl_seconds: int = 600
    idle_ttl_seconds: int = 43200
    absolute_ttl_seconds: int = 604800
    login_failures_per_minute: int = 5
    api_requests_per_minute: int = 120
    runtime_profile: Literal["offline_fixture", "openai_compatible"] = "offline_fixture"
    model_endpoint: str = ""
    model_name: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_timeout_seconds: float = 45.0
    model_max_output_tokens: int = Field(default=3000, ge=128, le=12000)
    model_wire_api: Literal["chat_completions", "responses"] = "chat_completions"
    model_input_usd_per_million: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    model_output_usd_per_million: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    model_request_cap_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    model_daily_cap_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    model_pricing_declared: bool = False
    model_contest_mode: bool = False
    model_catalog_id: str = Field(default="", max_length=200)
    model_provider_variant: str = Field(default="", max_length=200)
    model_price_snapshot: str = Field(default="", max_length=200)
    searxng_url: str = ""
    web_official_domains: str = ""
    runtime_automatic_takeover: bool = False
    runtime_lease_seconds: int = 30
    runtime_heartbeat_seconds: int = 10
    runtime_wall_seconds: int = 300
    runtime_graph_version: str = "fixture-v1"
    runtime_fixture_chunk_size: int = 24
    runtime_fixture_delay_seconds: float = 0.1

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use postgresql+psycopg")
        return value

    @model_validator(mode="after")
    def secure_production(self) -> "Settings":
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in ("http", "https")
            or not origin.hostname
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
            or origin.username is not None
            or origin.password is not None
        ):
            raise ValueError("public_origin must be an origin, without path or credentials")
        self.public_origin = self.public_origin.rstrip("/")
        if self.environment == "production" and origin.scheme != "https":
            raise ValueError("production requires HTTPS")
        if self.environment != "production" and origin.hostname not in (
            "localhost",
            "127.0.0.1",
            "testserver",
        ):
            raise ValueError("insecure development cookie is only allowed on localhost")
        for ttl in (self.anonymous_ttl_seconds, self.idle_ttl_seconds, self.absolute_ttl_seconds):
            if ttl <= 0:
                raise ValueError("session lifetimes must be positive")
        if self.idle_ttl_seconds > self.absolute_ttl_seconds:
            raise ValueError("idle TTL cannot exceed absolute TTL")
        if self.login_failures_per_minute <= 0 or self.api_requests_per_minute <= 0:
            raise ValueError("rate limits must be positive")
        if not 0 < self.runtime_heartbeat_seconds < self.runtime_lease_seconds:
            raise ValueError("runtime heartbeat must be positive and shorter than the lease")
        if self.runtime_wall_seconds <= 0 or not 1 <= self.runtime_fixture_chunk_size <= 4096:
            raise ValueError("invalid runtime wall time or fixture chunk size")
        if self.runtime_fixture_delay_seconds < 0 or not self.runtime_graph_version:
            raise ValueError("invalid fixture delay or graph version")
        if self.model_timeout_seconds <= 0:
            raise ValueError("model timeout must be positive")
        if min(self.model_input_usd_per_million, self.model_output_usd_per_million,
            self.model_request_cap_usd, self.model_daily_cap_usd) < 0:
            raise ValueError("model pricing and cap cannot be negative")
        if self.runtime_profile == "openai_compatible":
            endpoint = urlsplit(self.model_endpoint)
            if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
                raise ValueError("openai-compatible profile requires model endpoint")
            if not self.model_name or not self.model_api_key.get_secret_value():
                raise ValueError("openai-compatible profile requires model name and API key")
        if self.model_contest_mode and (
            self.runtime_profile != "openai_compatible"
            or not (self.model_pricing_declared or self.model_input_usd_per_million
                    or self.model_output_usd_per_million)
            or self.model_input_usd_per_million > 0.10
            or not all(value.strip() for value in (
                self.model_catalog_id, self.model_provider_variant, self.model_price_snapshot,
            ))
        ):
            raise ValueError("contest mode requires an eligible declared model tariff and route")
        return self

    @property
    def cookie_name(self) -> str:
        return "__Host-session" if self.environment == "production" else "partner-dev-session"


@lru_cache
def get_settings() -> Settings:
    return Settings()
