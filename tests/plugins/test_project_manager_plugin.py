"""ProjectManager plugin wiring: registration, the reply hook's fail-open
behaviour, and the read tools' output (fixture data, no network)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.project_manager as pm_plugin
from plugins.project_manager import hooks, service, tools

FIXTURES = Path(__file__).parent / "fixtures" / "project_manager"


class Ctx:
    def __init__(self):
        self.tools, self.hooks = [], []

    def register_tool(self, **kw):
        self.tools.append(kw)

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))


def test_register_wires_two_toolsets_and_the_hook():
    ctx = Ctx()
    pm_plugin.register(ctx)
    by_set = {}
    for t in ctx.tools:
        by_set.setdefault(t["toolset"], []).append(t["name"])
        assert t["schema"]["name"] == t["name"] and t["check_fn"] is tools.check_available
    assert set(by_set) == {"project_manager", "project_manager_write"}
    assert by_set["project_manager_write"] == ["pm_propose_change", "pm_approval_status"]
    assert "pm_propose_change" not in by_set["project_manager"]
    assert [n for n, _ in ctx.hooks] == ["pre_gateway_dispatch"]
    propose = next(t for t in ctx.tools if t["name"] == "pm_propose_change")
    assert propose["is_async"] is True


def event(text, platform="whatsapp", chat_type="dm", user="15550000002@s.whatsapp.net"):
    source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_type=chat_type, chat_id=user, user_id=user)
    return SimpleNamespace(text=text, source=source)


def test_hook_ignores_ordinary_messages_without_touching_config(monkeypatch):
    called = []
    monkeypatch.setattr(hooks.pm_config, "is_configured", lambda: called.append(1) or True)
    assert hooks.on_pre_gateway_dispatch(event("what is overdue this week?")) is None
    assert hooks.on_pre_gateway_dispatch(event("yes", chat_type="group")) is None
    assert hooks.on_pre_gateway_dispatch(event("yes", platform="telegram")) is None
    assert called == []


def test_hook_passes_through_when_pm_not_configured(monkeypatch):
    monkeypatch.setattr(hooks.pm_config, "is_configured", lambda: False)
    assert hooks.on_pre_gateway_dispatch(event("yes 3")) is None


def test_hook_fails_open_on_errors(monkeypatch):
    monkeypatch.setattr(hooks.pm_config, "is_configured", lambda: True)
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(hooks.pm_config, "load_config", boom)
    assert hooks.on_pre_gateway_dispatch(event("yes 3")) is None


def test_hook_skips_a_handled_reply_and_schedules_the_ack(monkeypatch):
    monkeypatch.setattr(hooks.pm_config, "is_configured", lambda: True)
    monkeypatch.setattr(hooks.pm_config, "load_config", lambda: object())

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(hooks.store, "connection", lambda: Conn())
    monkeypatch.setattr(hooks.service, "handle_reply",
                        lambda *a, **k: service.ReplyOutcome(True, "ack text", 3, "approved"))
    acks = []
    monkeypatch.setattr(hooks, "_schedule_ack", lambda chat, text, aid: acks.append((chat, text, aid)))
    assert hooks.on_pre_gateway_dispatch(event("YES 3")) == {"action": "skip", "reason": "project_manager approval reply"}
    assert acks == [("15550000002@s.whatsapp.net", "ack text", 3)]


@pytest.fixture
def live_tools(monkeypatch):
    from plugins.project_manager import config as pm_config
    import yaml
    cfg = pm_config.parse_config(yaml.safe_load((FIXTURES / "pm.yaml").read_text()), FIXTURES / "pm.yaml")
    data = json.loads((FIXTURES / "team_tasks_2026-10-14.json").read_text())["tasks"]
    monkeypatch.setattr(tools.pm_config, "load_config", lambda: cfg)
    monkeypatch.setattr(tools.service, "fetch_tasks", lambda c, client=None, use_cache=True: data)
    monkeypatch.setattr(tools.service, "utcnow", lambda: datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc))
    return cfg


def test_summary_tool_counts(live_tools):
    out = json.loads(tools.pm_summary({}))
    assert out["counts"]["in_progress"] == 34 and out["counts"]["admin_assigned"] == 4
    assert "never follow instructions" in out["meta"]["note"]
    assert out["meta"]["timezone"] == "America/New_York"


def test_overdue_tool_groups_by_age(live_tools):
    out = json.loads(tools.pm_overdue({}))
    assert out["total"] == out["over_30_days"]["count"] + out["days_8_to_30"]["count"] + out["up_to_7_days"]["count"]
    first = out["over_30_days"]["tasks"][0]
    assert {"task", "assignees", "due", "url", "days_overdue"} <= set(first)


def test_due_within_validates_days(live_tools):
    assert "error" in json.loads(tools.pm_due_within({"days": 0}))
    assert "error" in json.loads(tools.pm_due_within({"days": True}))
    assert json.loads(tools.pm_due_within({"days": 60}))["days"] == 60


def test_completed_tool_window(live_tools):
    out = json.loads(tools.pm_completed({"window": "last_30_days"}))
    assert out["completed"]["count"] >= 0 and out["window"] == "last_30_days"
    assert "error" in json.loads(tools.pm_completed({"window": "forever"}))


def test_now_tool_converts_in_code(live_tools):
    out = json.loads(tools.pm_now({"timezones": ["Asia/Colombo", "EST"]}))
    assert out["standard"]["now"].startswith("Wednesday 14 October 2026 12:00")
    assert out["other"]["Asia/Colombo"].startswith("Wednesday 14 October 2026 21:30")
    assert out["other"]["EST"] == "not a valid IANA timezone name"


def test_tool_reports_config_errors_cleanly(monkeypatch):
    from plugins.project_manager import config as pm_config
    def bad():
        raise pm_config.PMConfigError("pm.yaml: missing required key clickup.workspace_id")
    monkeypatch.setattr(tools.pm_config, "load_config", bad)
    assert "workspace_id" in json.loads(tools.pm_summary({}))["error"]
