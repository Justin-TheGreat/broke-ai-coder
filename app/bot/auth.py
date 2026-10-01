from __future__ import annotations

from dataclasses import dataclass

from app.config.models import DiscordConfig


@dataclass(frozen=True, slots=True)
class AuthDecision:
    allowed: bool
    reason: str  # "ok" | "user_not_allowed" | "guild_not_allowed" | "channel_not_allowed" | ...


def authorize(
    cfg: DiscordConfig, *, user_id: int, guild_id: int | None, channel_id: int | None
) -> AuthDecision:
    """Empty allowlists deny everyone. DMs are always denied. Pure; no logging."""
    if user_id not in cfg.allowed_user_ids:
        return AuthDecision(False, "user_not_allowed")
    if guild_id is None:
        return AuthDecision(False, "dm_not_allowed")
    if guild_id not in cfg.allowed_guild_ids:
        return AuthDecision(False, "guild_not_allowed")
    if channel_id is None or channel_id not in cfg.allowed_channel_ids:
        return AuthDecision(False, "channel_not_allowed")
    return AuthDecision(True, "ok")
