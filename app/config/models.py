from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.providers.base import QuotaDimension

ENV_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]*$"

_CFG = ConfigDict(extra="forbid", frozen=True)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHELL_NAMES = frozenset(
    {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "dash", "wsl", "env", "start"}
)
GATEWAY_PROVIDER_ID = "broke"
# Keys of app.providers.registry.ADAPTER_TYPES (not imported: that would be an import cycle).
SUPPORTED_PROVIDERS = frozenset({"openrouter", "gemini", "groq"})
_ENV_NAME_RE = re.compile(ENV_NAME_PATTERN)
_FORBIDDEN_PASSTHROUGH_PREFIXES = ("OPENCODE_", "AWS_", "AZURE_")

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
        if any("{" in m or "}" in m or any(ch.isspace() for ch in m) for m in out):
            raise ValueError("model_order entries must not contain '{', '}' or whitespace")
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
        if v is not None and v.startswith("http://") and urlsplit(v).hostname not in _LOCAL_HOSTS:
            raise ValueError("base_url must use https:// (http:// only for localhost)")
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
    command: list[str] = Field(default_factory=lambda: ["opencode"], min_length=1)
    managed_server: bool = True
    server_port: int = Field(4096, ge=0, le=65535)
    server_directory: str = "data/opencode-server"
    server_start_timeout_s: float = Field(30, gt=0, le=300, allow_inf_nan=False)
    server_restart_max_backoff_s: float = Field(60, gt=0, le=600, allow_inf_nan=False)
    env_passthrough: list[str] = Field(default_factory=list)
    projects: dict[str, str] = Field(default_factory=dict)
    run_timeout_s: float = Field(1800, gt=0, le=86400, allow_inf_nan=False)
    kill_grace_s: float = Field(5, gt=0, le=60, allow_inf_nan=False)
    health_timeout_s: float = Field(5, gt=0, le=60, allow_inf_nan=False)
    max_line_bytes: int = Field(1_048_576, ge=1024, le=16_777_216)
    output_tail_chars: int = Field(4000, ge=200, le=20000)
    stderr_tail_bytes: int = Field(4096, ge=256, le=65536)
    continue_prompt: str = (
        "Continue the previous task from where it stopped. Do not repeat steps that already"
        " completed."
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_provider_map(cls, data: Any) -> Any:
        if isinstance(data, dict) and "provider_map" in data:
            raise ValueError(
                "opencode.provider_map was removed: OpenCode reaches providers only through"
                " the built-in gateway"
            )
        return data

    @field_validator("server_url")
    @classmethod
    def _server_url_valid(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("server_url must start with https:// or http://")
        if v.startswith("http://") and urlsplit(v).hostname not in _LOCAL_HOSTS:
            raise ValueError("server_url must use https:// (http:// only for localhost)")
        return v

    @field_validator("env_passthrough")
    @classmethod
    def _env_passthrough_valid(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            name = raw.strip()
            if not _ENV_NAME_RE.match(name):
                raise ValueError(f"env_passthrough entry {raw!r} is not a valid env var name")
            if (
                name.startswith(_FORBIDDEN_PASSTHROUGH_PREFIXES)
                or name.endswith("_API_KEY")
                or name == "GOOGLE_APPLICATION_CREDENTIALS"
            ):
                raise ValueError(f"env_passthrough must not pass credential-like variable {name}")
            out.append(name)
        return out

    @field_validator("command")
    @classmethod
    def _command_valid(cls, v: list[str]) -> list[str]:
        out = [c.strip() for c in v]
        if any(not c for c in out):
            raise ValueError("command entries must be non-empty")
        for c in out:
            base = re.split(r"[\\/]", c)[-1].lower()
            if base.endswith((".cmd", ".bat", ".ps1")):
                raise ValueError("command must not use .cmd/.bat/.ps1 launchers")
            stem = base[:-4] if base.endswith(".exe") else base
            if stem in _SHELL_NAMES:
                raise ValueError(f"command must not use a shell or interpreter wrapper ({stem})")
        return out

    @field_validator("projects")
    @classmethod
    def _projects_valid(cls, v: dict[str, str]) -> dict[str, str]:
        for name in v:
            if name == "default":
                raise ValueError("'default' is reserved and cannot be a projects key")
            if not _PROJECT_NAME_RE.match(name):
                raise ValueError(f"invalid project name {name!r}")
        return v

    @field_validator("continue_prompt")
    @classmethod
    def _continue_prompt_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("continue_prompt must be non-empty")
        return v

    def project_directory(self, project: str) -> str | None:
        if project == "default":
            return self.working_directory
        return self.projects.get(project)


class GatewayConfig(BaseModel):
    model_config = _CFG

    port: int = Field(0, ge=0, le=65535)
    max_request_bytes: int = Field(16_777_216, ge=65_536, le=67_108_864)
    max_response_bytes: int = Field(33_554_432, ge=65_536, le=134_217_728)
    max_error_body_bytes: int = Field(65_536, ge=1024, le=1_048_576)
    upstream_connect_timeout_s: float = Field(10, gt=0, le=60, allow_inf_nan=False)
    upstream_read_timeout_s: float = Field(300, gt=0, le=1800, allow_inf_nan=False)
    max_concurrent_requests: int = Field(4, ge=1, le=64)
    small_model: str | None = None


class OrchestratorConfig(BaseModel):
    model_config = _CFG

    provider_refresh_interval_s: float = Field(300, ge=0, allow_inf_nan=False)
    progress_min_interval_s: float = Field(10, ge=0, allow_inf_nan=False)
    session_idle_close_days: float = Field(7, gt=0, allow_inf_nan=False)
    require_tool_calling: bool = False
    small_model_check_ttl_s: float = Field(30, ge=0, allow_inf_nan=False)


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
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)

    @model_validator(mode="after")
    def _providers_defined(self) -> AppConfig:
        for pid, pol in self.routing.policies.items():
            if pol.provider not in self.providers:
                raise ValueError(f"policy {pid!r} references undefined provider {pol.provider!r}")
            if pol.provider not in SUPPORTED_PROVIDERS:
                raise ValueError(
                    f"policy {pid!r}: provider {pol.provider!r} is not one of"
                    f" {sorted(SUPPORTED_PROVIDERS)}; {PAID_NOT_SUPPORTED}"
                )
            if pol.provider == "openrouter":
                for m in pol.model_order:
                    if not is_free_openrouter_model(m):
                        raise ValueError(
                            f"policy {pid!r}: OpenRouter model {m!r} is not a free variant"
                            f" (use 'openrouter/free' or an id ending in ':free'); "
                            f"{PAID_NOT_SUPPORTED}"
                        )
        known_envs = {p.api_key_env for p in self.providers.values()}
        known_envs.add(self.discord.bot_token_env)
        clash = sorted(set(self.opencode.env_passthrough) & known_envs)
        if clash:
            raise ValueError(
                f"opencode.env_passthrough must not include secret env vars: {', '.join(clash)}"
            )
        small = self.gateway.small_model
        if small is not None and small not in self.gateway_models():
            raise ValueError(
                "gateway.small_model must be one of the enabled allowlisted gateway models"
            )
        return self

    def gateway_models(self) -> dict[str, tuple[str, str]]:
        """Ordered ``"<provider>/<model>" -> (provider, model)`` for routable candidates only."""
        out: dict[str, tuple[str, str]] = {}
        for pid in self.routing.provider_order:
            pol = self.routing.policies.get(pid)
            if pol is None or not pol.enabled:
                continue
            prov = self.providers.get(pol.provider)
            if prov is None or not prov.enabled or pol.provider not in SUPPORTED_PROVIDERS:
                continue
            for model in pol.model_order:
                out[f"{pol.provider}/{model}"] = (pol.provider, model)
        return out
