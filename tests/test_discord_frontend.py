from __future__ import annotations

import logging

import discord
from conftest import NOW

from app.bot.commands import COMMAND_NAMES
from app.bot.discord_frontend import DiscordFrontend, create_frontend
from app.redaction import SecretRedactor
from app.runtime.daemon import ControllerEvent
from app.runtime.interfaces import NullFrontend

TOKEN = "fake-discord-token-value-123456"


class FakeMessage:
    def __init__(self, text, kwargs):
        self.text = text
        self.kwargs = kwargs
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        msg = FakeMessage(text, kwargs)
        self.sent.append(msg)
        return msg


class StubClient:
    def __init__(self, channel):
        self.channel = channel
        self.asked = []

    def get_channel(self, cid):
        self.asked.append(cid)
        return self.channel


def ev(name, task_id="t1", **fields):
    return ControllerEvent(name, at=NOW, task_id=task_id, fields=fields)


def test_create_frontend_without_token_is_null(caplog):
    cfg = _cfg()
    with caplog.at_level(logging.WARNING):
        fe = create_frontend(cfg, redactor=None, environ={})
    assert isinstance(fe, NullFrontend)
    assert any("DISCORD_BOT_TOKEN" in r.getMessage() for r in caplog.records)


def test_create_frontend_with_token_hides_it():
    fe = create_frontend(_cfg(), redactor=None, environ={"DISCORD_BOT_TOKEN": TOKEN})
    assert isinstance(fe, DiscordFrontend)
    assert TOKEN not in repr(fe) and TOKEN not in str(vars(fe))


def _cfg():
    from app.config.models import AppConfig

    return AppConfig()


def test_build_client_registers_commands_without_connecting():
    fe = DiscordFrontend(_cfg(), redactor=None, environ={})
    client, tree = fe.build_client()
    cmds = {c.name: c for c in tree.get_commands()}
    assert tuple(sorted(cmds)) == tuple(sorted(COMMAND_NAMES))
    options = {o["name"]: o for o in cmds["code"].to_dict(tree)["options"]}
    assert options["prompt"]["max_length"] == 4000 and options["prompt"]["required"]
    assert not options["project"].get("required") and not options["session"].get("required")
    assert client.intents.guilds and not client.intents.message_content
    assert not client.is_ready()


async def test_progress_edits_single_message_and_finish_sends_new():
    fe = DiscordFrontend(_cfg(), redactor=None, environ={})
    channel = FakeChannel()
    fe._client = StubClient(channel)
    await fe.on_event(ev("progress", channel_id="42", opencode_session_id="s"))
    await fe.on_event(ev("progress", channel_id="42", tool="bash", tool_status="run", llm_steps=1))
    await fe.on_event(ev("progress", channel_id="42", tool="bash", tool_status="run", llm_steps=2))
    assert len(channel.sent) == 1 and len(channel.sent[0].edits) == 2
    assert fe._client.asked[0] == 42
    await fe.on_event(ev("task_finished", channel_id="42", status="CANCELLED", short_id="abcdef12"))
    assert len(channel.sent) == 2 and "cancelled" in channel.sent[1].text
    assert fe._messages == {}
    for m in channel.sent:
        assert isinstance(m.kwargs["allowed_mentions"], discord.AllowedMentions)
        assert m.kwargs["allowed_mentions"].everyone is False
    assert channel.sent[0].edits[0]["allowed_mentions"].users is False


async def test_missing_channel_id_sends_nothing():
    fe = DiscordFrontend(_cfg(), redactor=None, environ={})
    channel = FakeChannel()
    fe._client = StubClient(channel)
    await fe.on_event(ev("progress", channel_id=None, opencode_session_id="s"))
    await fe.on_event(ev("task_routing", channel_id="42"))
    assert channel.sent == []


async def test_unknown_channel_and_errors_are_swallowed():
    fe = DiscordFrontend(_cfg(), redactor=SecretRedactor(), environ={})
    fe._client = StubClient(None)
    await fe.on_event(
        ev("provider_selected", channel_id="42", provider="p", model="m", attempt_no=1)
    )

    class Boom(FakeChannel):
        async def send(self, text, **kwargs):
            raise RuntimeError("x")

    fe._client = StubClient(Boom())
    await fe.on_event(
        ev("provider_selected", channel_id="42", provider="p", model="m", attempt_no=1)
    )


async def test_message_cache_is_bounded():
    fe = DiscordFrontend(_cfg(), redactor=None, environ={})
    fe._client = StubClient(FakeChannel())
    for i in range(150):
        await fe.on_event(ev("progress", task_id=f"t{i}", channel_id="1", opencode_session_id="s"))
    assert len(fe._messages) == 100 and "t0" not in fe._messages


async def test_stop_without_start():
    fe = DiscordFrontend(_cfg(), redactor=None, environ={})
    await fe.stop()
    await fe.stop()
