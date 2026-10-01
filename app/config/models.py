from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.providers.base import QuotaDimension

ENV_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]*$"

_CFG = ConfigDict(extra="forbid", frozen=True)

PAID_NOT_SUPPORTED = "paid inference is not supported: this project is free-only"
_PAID_ROUTING_KEYS = (
    "mode",
    "paid_requires_approval",
    "daily_paid_budget_usd",
    "monthly_paid_budget_usd",
)


def is_free_openrouter_model(model: str) -> bool:
    """OpenRouter bills credits for any model that is not a free variant."""
    return model == "openrouter/free" or model.endswith(":free")


class PolicyConfig(BaseModel):
    model_config = _CFG

    provider: str
    enabled: bool = True
    model_order: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _reject_cost_class(cls, data: Any) -> Any:
        if isinstance(data, dict) and "cost_class" in data:
            raise ValueError(f"cost_class is not a setting; {PAID_NOT_SUPPORTED}")
        return data

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

    max_fallback_attempts: int = Field(3, ge=1)
    large_context_min_tokens: int = Field(128_000, ge=1)
    provider_order: list[str] = Field(default_factory=list)
    policies: dict[str, PolicyConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_paid_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            found = [k for k in _PAID_ROUTING_KEYS if k in data]
            if found:
                raise ValueError(f"{', '.join(found)}: {PAID_NOT_SUPPORTED}")
        return data

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


class ModelMetadataConfig(BaseModel):
    model_config = _CFG

    supports_tool_calling: bool | None = None
    supports_structured_output: bool | None = None
    supports_vision: bool | None = None
    context_window: int | None = Field(None, ge=1)
    max_output_tokens: int | None = Field(None, ge=1)


class LimitConfig(BaseModel):
    model_config = _CFG

    dimension: QuotaDimension
    limit: float = Field(gt=0, allow_inf_nan=False)
    model: str | None = None

    @field_validator("dimension")
    @classmethod
    def _no_spend(cls, v: QuotaDimension) -> QuotaDimension:
        if v == QuotaDimension.SPEND_USD:
            raise ValueError(f"spend limits do not apply; {PAID_NOT_SUPPORTED}")
        return v

    @field_validator("model")
    @classmethod
    def _model_valid(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("model must be non-empty when given")
        return v


class ProviderConfig(BaseModel):
    model_config = _CFG

    enabled: bool = True
    api_key_env: str = Field(pattern=ENV_NAME_PATTERN)
    base_url: str | None = None
    timeout_s: float = Field(10, gt=0, le=120, allow_inf_nan=False)
    models: dict[str, ModelMetadataConfig] = Field(default_factory=dict)
    limits: list[LimitConfig] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def _base_url_valid(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("https://", "http://")):
            raise ValueError("base_url must start with https:// or http://")
        return v

    @field_validator("models")
    @classmethod
    def _models_keys(cls, v: dict[str, ModelMetadataConfig]) -> dict[str, ModelMetadataConfig]:
        out: dict[str, ModelMetadataConfig] = {}
        for key, meta in v.items():
            k = key.strip()
            if not k:
                raise ValueError("models keys must be non-empty")
            if k in out:
                raise ValueError(f"models contains duplicate key {k!r}")
            out[k] = meta
        return out

    @model_validator(mode="after")
    def _limits_unique(self) -> ProviderConfig:
        seen: set[tuple[str | None, QuotaDimension]] = set()
        for lim in self.limits:
            key = (lim.model, lim.dimension)
            if key in seen:
                raise ValueError(f"duplicate limit for (model, dimension) {key}")
            seen.add(key)
        return self


class CooldownConfig(BaseModel):
    model_config = _CFG

    rate_limit_default_s: float = Field(60, gt=0, allow_inf_nan=False)
    provider_unavailable_s: float = Field(120, gt=0, allow_inf_nan=False)
    network_failure_threshold: int = Field(3, ge=1)
    network_failure_window_s: float = Field(300, gt=0, allow_inf_nan=False)
    network_failure_cooldown_s: float = Field(120, gt=0, allow_inf_nan=False)
    max_cooldown_s: float = Field(3600, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _check(self) -> CooldownConfig:
        for name in (
            "rate_limit_default_s",
            "provider_unavailable_s",
            "network_failure_cooldown_s",
        ):
            if getattr(self, name) > self.max_cooldown_s:
                raise ValueError(f"{name} must be <= max_cooldown_s")
        return self


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
    cooldown: CooldownConfig = Field(default_factory=CooldownConfig)

    @model_validator(mode="after")
    def _providers_defined(self) -> AppConfig:
        for pid, pol in self.routing.policies.items():
            if pol.provider not in self.providers:
                raise ValueError(f"policy {pid!r} references undefined provider {pol.provider!r}")
            if pol.provider == "openrouter":
                for m in pol.model_order:
                    if not is_free_openrouter_model(m):
                        raise ValueError(
                            f"policy {pid!r}: OpenRouter model {m!r} is not a free variant"
                            f" (use 'openrouter/free' or an id ending in ':free'); "
                            f"{PAID_NOT_SUPPORTED}"
                        )
        return self
