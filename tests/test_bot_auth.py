from __future__ import annotations

from app.bot.auth import authorize
from app.config.models import DiscordConfig


def cfg(users=(1,), guilds=(2,), channels=(3,)):
    return DiscordConfig(
        allowed_user_ids=list(users),
        allowed_guild_ids=list(guilds),
        allowed_channel_ids=list(channels),
    )


def test_all_empty_denies():
    d = authorize(DiscordConfig(), user_id=1, guild_id=2, channel_id=3)
    assert not d.allowed and d.reason == "user_not_allowed"


def test_user_allowed_but_other_lists_empty_denies():
    assert not authorize(cfg(guilds=()), user_id=1, guild_id=2, channel_id=3).allowed
    assert not authorize(cfg(channels=()), user_id=1, guild_id=2, channel_id=3).allowed


def test_dm():
    d = authorize(cfg(), user_id=1, guild_id=None, channel_id=3)
    assert d.reason == "dm_not_allowed" and not d.allowed


def test_wrong_ids_give_right_reason():
    assert authorize(cfg(), user_id=9, guild_id=2, channel_id=3).reason == "user_not_allowed"
    assert authorize(cfg(), user_id=1, guild_id=9, channel_id=3).reason == "guild_not_allowed"
    assert authorize(cfg(), user_id=1, guild_id=2, channel_id=9).reason == "channel_not_allowed"
    assert authorize(cfg(), user_id=1, guild_id=2, channel_id=None).reason == "channel_not_allowed"


def test_allowed():
    d = authorize(cfg(), user_id=1, guild_id=2, channel_id=3)
    assert d.allowed and d.reason == "ok"
