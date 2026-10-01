from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.providers.base import CostClass

ENV_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]*$"

_CFG = ConfigDict(extra="forbid", frozen=True)


class RoutingMode(StrEnum):
    FREE_ONLY = "free-only"
    FREE_FIRST_NO_PAID = "free-first-no-paid"
    FREE_FIRST_PAID_AFTER_APPROVAL = "free-first-paid-after-approval"


class PolicyConfig(BaseModel):
    model_config = _CFG

    provider: str
    cost_class: CostClass
    enabled: bool = True
    model_order: list[str] = Field(min_length=1)

    @field_validator("provider")
    @classmethod
    def _provider_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("provider must be non-empty")
        return v

    @field_validator("model_order")
    @classmethod
    def _models_valid(cls, v: list[str]) -> list[str]:
        out = [m.strip() for m in v]
        if any(not m for m in out):
            raise ValueError("model_order entries must be non-empty")
        if len(set(out)) != len(out):
            raise ValueError("model_order contains duplicate models")
        return out


class RoutingConfig(BaseModel):
    model_config = _CFG

    mode: RoutingMode = RoutingMode.FREE_FIRST_NO_PAID
    paid_requires_approval: bool = True
    daily_paid_budget_usd: float = Field(0.0, ge=0)
    monthly_paid_budget_usd: float | None = Field(None, ge=0)
    max_fallback_attempts: int = Field(3, ge=1)
    large_context_min_tokens: int = Field(128_000, ge=1)
    provider_order: list[str] = Field(default_factory=list)
    policies: dict[str, PolicyConfig] = Field(default_factory=dict)

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_alias(cls, v: object) -> object:
        if v == "free-first-paid-after-confirmation":
            return RoutingMode.FREE_FIRST_PAID_AFTER_APPROVAL
        return v

    @model_validator(mode="after")
    def _check(self) -> RoutingConfig:
        if len(set(self.provider_order)) != len(self.provider_order):
            raise ValueError("provider_order contains duplicate entries")
        for pid in self.provider_order:
            if pid not in self.policies:
                raise ValueError(f"provider_order references unknown policy id: {pid}")
        seen: dict[tuple[str, str], str] = {}
        for pid, pol in self.policies.items():
            for m in pol.model_order:
                key = (pol.provider, m)
                if key in seen:
                    raise ValueError(
                        f"(provider, model) {key} appears in policies {seen[key]!r} and {pid!r}"
                    )
                seen[key] = pid
        return self


class ProviderConfig(BaseModel):
    model_config = _CFG

    enabled: bool = True
    api_key_env: str = Field(pattern=ENV_NAME_PATTERN)


class DiscordConfig(BaseModel):
    model_config = _CFG

    bot_token_env: str = Field("DISCORD_BOT_TOKEN", pattern=ENV_NAME_PATTERN)
    allowed_user_ids: list[int] = Field(default_factory=list)
    allowed_guild_ids: list[int] = Field(default_factory=list)
    allowed_channel_ids: list[int] = Field(default_factory=list)


class OpenCodeConfig(BaseModel):
    model_config = _CFG

    server_url: str = "http://127.0.0.1:4096"
    working_directory: str = "/workspace"


class DatabaseConfig(BaseModel):
    model_config = _CFG

    path: str = "data/agent-controller.db"
    busy_timeout_ms: int = Field(5000, ge=0)
    retention_days: int = Field(60, ge=1)
    cleanup_interval_hours: float = Field(24, gt=0, le=24)
    cleanup_batch_size: int = Field(500, ge=1)


class RuntimeConfig(BaseModel):
    model_config = _CFG

    task_queue_maxsize: int = Field(100, ge=1)
    event_queue_maxsize: int = Field(1000, ge=1)
    shutdown_timeout_s: float = Field(10, gt=0)


class AppConfig(BaseModel):
    model_config = _CFG

    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def _providers_defined(self) -> AppConfig:
        for pid, pol in self.routing.policies.items():
            if pol.provider not in self.providers:
                raise ValueError(f"policy {pid!r} references undefined provider {pol.provider!r}")
        return self
