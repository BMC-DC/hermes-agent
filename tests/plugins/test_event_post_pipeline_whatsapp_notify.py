"""Tests for the event-post-pipeline plugin's WhatsApp send path
(``plugins/platforms/event_post_pipeline/whatsapp_notify.py``).

Decided 2026-09-21: every notification goes to the shared social-media
group (``send_whatsapp_link``, resolving ``platforms.whatsapp.home_channel
.chat_id``) — the earlier v2 per-curator DM resolution path
(``send_whatsapp_to_curator``/``notify_curators``/etc.) was removed as dead
code once every call site stopped using it.
"""

from __future__ import annotations

import pytest

from plugins.platforms.event_post_pipeline import whatsapp_notify


def _config_with(platforms: dict):
    return lambda: {"platforms": platforms}


def test_load_whatsapp_config_defaults_to_test_group(monkeypatch):
    import hermes_cli.config as config_module
    monkeypatch.setattr(
        config_module, "load_config",
        _config_with({"event_post_pipeline": {"extra": {"whatsapp_test_group_chat_id": "test@g.us"}}}),
    )
    _, chat_id = whatsapp_notify._load_whatsapp_config()
    assert chat_id == "test@g.us"


def test_load_whatsapp_config_switches_to_production_when_set(monkeypatch):
    import hermes_cli.config as config_module
    monkeypatch.setattr(
        config_module, "load_config",
        _config_with({
            "event_post_pipeline": {"extra": {
                "whatsapp_group_mode": "production",
                "whatsapp_test_group_chat_id": "test@g.us",
                "whatsapp_production_group_chat_id": "real-social-media-group@g.us",
            }},
        }),
    )
    _, chat_id = whatsapp_notify._load_whatsapp_config()
    assert chat_id == "real-social-media-group@g.us"


def test_load_whatsapp_config_production_mode_falls_back_to_test_when_unset(monkeypatch):
    import hermes_cli.config as config_module
    monkeypatch.setattr(
        config_module, "load_config",
        _config_with({
            "event_post_pipeline": {"extra": {
                "whatsapp_group_mode": "production",
                "whatsapp_test_group_chat_id": "test@g.us",
                # whatsapp_production_group_chat_id deliberately absent
            }},
        }),
    )
    _, chat_id = whatsapp_notify._load_whatsapp_config()
    assert chat_id == "test@g.us"  # never silently sends nowhere


def test_load_whatsapp_config_falls_back_to_home_channel_for_backward_compat(monkeypatch):
    import hermes_cli.config as config_module
    monkeypatch.setattr(
        config_module, "load_config",
        _config_with({
            "event_post_pipeline": {"extra": {}},  # no whatsapp_test_group_chat_id set at all
            "whatsapp": {"home_channel": {"chat_id": "legacy-home-channel@g.us"}},
        }),
    )
    _, chat_id = whatsapp_notify._load_whatsapp_config()
    assert chat_id == "legacy-home-channel@g.us"


def test_load_whatsapp_config_returns_whatsapp_platform_extra_not_pipeline_extra(monkeypatch):
    """Regression guard: _via_standalone_sender passes this straight to the WhatsApp
    bridge's own standalone_sender_fn as its platform config — it must be the WhatsApp
    platform's own extra (session_path, bridge_script, ...), never this pipeline's."""
    import hermes_cli.config as config_module
    monkeypatch.setattr(
        config_module, "load_config",
        _config_with({
            "whatsapp": {"extra": {"session_path": "/opt/data/whatsapp/session"}},
            "event_post_pipeline": {"extra": {"whatsapp_test_group_chat_id": "test@g.us"}},
        }),
    )
    extra, _ = whatsapp_notify._load_whatsapp_config()
    assert extra == {"session_path": "/opt/data/whatsapp/session"}


@pytest.mark.asyncio
async def test_send_whatsapp_link_sends_to_the_configured_home_channel(monkeypatch):
    sent = {}

    async def fake_send(chat_id, message):
        sent["chat_id"] = chat_id
        sent["message"] = message

    monkeypatch.setattr(whatsapp_notify, "_load_whatsapp_config", lambda: ({}, "120363430479652029@g.us"))
    monkeypatch.setattr(whatsapp_notify, "send_whatsapp_message", fake_send)
    await whatsapp_notify.send_whatsapp_link("New event post ready for review: https://example/review/abc")
    assert sent["chat_id"] == "120363430479652029@g.us"


@pytest.mark.asyncio
async def test_send_whatsapp_link_raises_when_home_channel_unconfigured(monkeypatch):
    monkeypatch.setattr(whatsapp_notify, "_load_whatsapp_config", lambda: ({}, None))
    with pytest.raises(whatsapp_notify.WhatsAppNotifyError):
        await whatsapp_notify.send_whatsapp_link("hello")


@pytest.mark.asyncio
async def test_send_whatsapp_message_falls_back_to_standalone_sender_when_no_live_adapter(monkeypatch):
    async def fake_via_live_adapter(chat_id, message):
        return None  # no running gateway in this process — the common worker-process case

    standalone_calls = []

    async def fake_via_standalone_sender(chat_id, message):
        standalone_calls.append((chat_id, message))

    monkeypatch.setattr(whatsapp_notify, "_via_live_adapter", fake_via_live_adapter)
    monkeypatch.setattr(whatsapp_notify, "_via_standalone_sender", fake_via_standalone_sender)
    await whatsapp_notify.send_whatsapp_message("120363430479652029@g.us", "hi")
    assert standalone_calls == [("120363430479652029@g.us", "hi")]


@pytest.mark.asyncio
async def test_via_standalone_sender_actually_runs_its_real_body(monkeypatch):
    """Regression test: a prior refactor deleted the `from types import SimpleNamespace`
    import while this function's body still constructs one, and every existing test
    mocked `_via_standalone_sender` away entirely — so a plain NameError shipped to
    production undetected (only surfaced once the pipeline visualizer showed a real
    "Notify Failed: name 'SimpleNamespace' is not defined" event). This test calls the
    real function body, only mocking its two external dependencies (plugin discovery,
    config loading), so a missing import fails loudly here instead of in production."""
    sender_calls = []

    async def fake_standalone_sender_fn(pconfig, chat_id, message):
        sender_calls.append((pconfig.extra, chat_id, message))
        return {"ok": True}

    fake_entry = type("Entry", (), {"standalone_sender_fn": staticmethod(fake_standalone_sender_fn)})()
    fake_registry = type("Registry", (), {"get": staticmethod(lambda name: fake_entry)})()

    monkeypatch.setitem(
        __import__("sys").modules, "hermes_cli.plugins",
        type("M", (), {"discover_plugins": staticmethod(lambda: None)}),
    )
    monkeypatch.setitem(
        __import__("sys").modules, "gateway.platform_registry",
        type("M", (), {"platform_registry": fake_registry}),
    )
    monkeypatch.setattr(whatsapp_notify, "_load_whatsapp_config", lambda: ({"some": "extra"}, "chat"))

    await whatsapp_notify._via_standalone_sender("120363430479652029@g.us", "hi")

    assert sender_calls == [({"some": "extra"}, "120363430479652029@g.us", "hi")]


@pytest.mark.asyncio
async def test_send_whatsapp_message_raises_on_live_adapter_failure(monkeypatch):
    async def fake_via_live_adapter(chat_id, message):
        return False  # a live adapter tried and reported failure

    monkeypatch.setattr(whatsapp_notify, "_via_live_adapter", fake_via_live_adapter)
    with pytest.raises(whatsapp_notify.WhatsAppNotifyError):
        await whatsapp_notify.send_whatsapp_message("120363430479652029@g.us", "hi")


@pytest.mark.asyncio
async def test_send_whatsapp_message_requires_a_chat_id():
    with pytest.raises(whatsapp_notify.WhatsAppNotifyError):
        await whatsapp_notify.send_whatsapp_message("", "hi")
