"""Tests for the event-post-pipeline plugin's WhatsApp recipient resolution
(``plugins/platforms/event_post_pipeline/whatsapp_notify.py``): the new v2
curator-resolved sends, and that the v1 single-hardcoded-recipient path keeps
working unchanged.
"""

from __future__ import annotations

import pytest

from plugins.platforms.event_post_pipeline import whatsapp_notify


def test_curator_jid_resolves_bare_phone_to_whatsapp_jid():
    curator = {"id": 1, "phone_number": "15551234567", "name": "Amila"}
    assert whatsapp_notify.curator_jid(curator) == "15551234567@s.whatsapp.net"


def test_curator_jid_empty_phone_returns_empty_string():
    assert whatsapp_notify.curator_jid({"phone_number": ""}) == ""
    assert whatsapp_notify.curator_jid({}) == ""


@pytest.mark.asyncio
async def test_send_whatsapp_to_curator_uses_resolved_jid(monkeypatch):
    sent = {}

    async def fake_send(chat_id, message):
        sent["chat_id"] = chat_id
        sent["message"] = message

    monkeypatch.setattr(whatsapp_notify, "send_whatsapp_message", fake_send)
    await whatsapp_notify.send_whatsapp_to_curator({"phone_number": "+15551234567"}, "hello")
    assert sent["chat_id"] == "15551234567@s.whatsapp.net"
    assert sent["message"] == "hello"


@pytest.mark.asyncio
async def test_send_whatsapp_to_curator_without_phone_raises():
    with pytest.raises(whatsapp_notify.WhatsAppNotifyError):
        await whatsapp_notify.send_whatsapp_to_curator({"id": 1}, "hello")


@pytest.mark.asyncio
async def test_send_whatsapp_to_phone_resolves_jid(monkeypatch):
    sent = {}

    async def fake_send(chat_id, message):
        sent["chat_id"] = chat_id

    monkeypatch.setattr(whatsapp_notify, "send_whatsapp_message", fake_send)
    await whatsapp_notify.send_whatsapp_to_phone("6012345678", "hi")
    assert sent["chat_id"] == "6012345678@s.whatsapp.net"


@pytest.mark.asyncio
async def test_notify_curators_dedupes_same_phone_number(monkeypatch):
    calls = []

    async def fake_send(curator, message):
        calls.append(curator["phone_number"])

    monkeypatch.setattr(whatsapp_notify, "send_whatsapp_to_curator", fake_send)
    curators = [
        {"id": 1, "phone_number": "+1555", "is_admin": True},
        {"id": 1, "phone_number": "+1555", "is_publisher": True},  # same person, two role flags
        {"id": 2, "phone_number": "+1666"},
    ]
    await whatsapp_notify.notify_curators(curators, "msg")
    assert calls == ["+1555", "+1666"]


@pytest.mark.asyncio
async def test_notify_curators_one_failure_does_not_stop_the_others(monkeypatch):
    calls = []

    async def fake_send(curator, message):
        if curator["phone_number"] == "+1bad":
            raise whatsapp_notify.WhatsAppNotifyError("boom")
        calls.append(curator["phone_number"])

    monkeypatch.setattr(whatsapp_notify, "send_whatsapp_to_curator", fake_send)
    curators = [{"id": 1, "phone_number": "+1bad"}, {"id": 2, "phone_number": "+1good"}]
    await whatsapp_notify.notify_curators(curators, "msg")
    assert calls == ["+1good"]


@pytest.mark.asyncio
async def test_send_whatsapp_link_unchanged_v1_behavior(monkeypatch):
    """The pre-existing single-hardcoded-recipient function still resolves
    ``platforms.whatsapp.home_channel.chat_id`` from config.yaml, not the curator registry."""
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
