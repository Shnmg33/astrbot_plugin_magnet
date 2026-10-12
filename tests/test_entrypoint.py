"""Entrypoint contract test with minimal AstrBot API doubles (not a live runtime)."""

import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def plugin_class(monkeypatch):
    api = ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    events = ModuleType("astrbot.api.event")
    events.AstrMessageEvent = object
    events.filter = SimpleNamespace(
        command=lambda name: lambda fn: fn,
        event_message_type=lambda x: lambda fn: fn,
        EventMessageType=SimpleNamespace(ALL="all"),
    )
    star = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Star = Star
    star.Context = object
    star.StarTools = SimpleNamespace(get_data_dir=lambda name: Path("/tmp") / name)
    star.register = lambda *args: lambda cls: cls
    monkeypatch.setitem(sys.modules, "astrbot", ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.event", events)
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)
    sys.modules.pop("astrbot_plugin_magnet.main", None)
    module = importlib.import_module("astrbot_plugin_magnet.main")
    yield module.MagnetPlugin
    sys.modules.pop("astrbot_plugin_magnet.main", None)


class Event:
    def __init__(self, text):
        self.text = text
        self.stopped = False

    def get_message_str(self):
        return self.text

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


@pytest.mark.parametrize("text", ["/bt", "bt", "/bt help", "bt help"])
async def test_help_with_or_without_wake_prefix(plugin_class, text):
    p = plugin_class(None, {})
    e = Event(text)
    replies = [x async for x in p.bt(e)]
    assert len(replies) == 1 and "/bt download" in replies[0]
    assert e.stopped


async def test_disabled_plugin_does_not_connect(plugin_class):
    p = plugin_class(None, {})
    await p.initialize()
    e = Event("/bt magnet:?xt=urn:btih:" + "a" * 40)
    reply = [x async for x in p.bt(e)]
    assert "尚未启用" in reply[0] and p.manager is None


async def test_entrypoint_passes_full_selection(plugin_class):
    captured = []

    class Manager:
        async def command(self, actor, command, args):
            captured.append((command, args))
            return "OK"

    p = plugin_class(None, {"enabled": True})
    p.manager = Manager()
    p.actor = lambda event: object()
    e = Event("bt download T12345678 1,3,5")
    replies = [x async for x in p.bt(e)]
    assert replies == ["OK"]
    assert captured == [("download", ["T12345678", "1,3,5"])]
