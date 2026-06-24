from __future__ import annotations

import json

from aegis_alpha.alerts import weclaw_notifier
from aegis_alpha.alerts.weclaw_notifier import (
    allowed_observation_grades,
    post_observation_to_weclaw,
    render_weclaw_observation_text,
    should_post_observation_to_weclaw,
)
from aegis_alpha.models import AgentObservation


def _obs(**kw) -> AgentObservation:
    base = dict(
        observation_id="ob_1", trading_day="2026-06-23",
        title="算力主题轮动", summary="多只算力股盘中拉升",
        observation_type="theme_rotation", stance="actionable_watch",
        confidence="high", theme="AI算力",
        created_at="2026-06-23T09:42:00+08:00",
    )
    base.update(kw)
    return AgentObservation(**base)


def test_observation_disabled_by_default(monkeypatch):
    monkeypatch.setenv("AEGIS_ALPHA_WECLAW_ENABLED", "false")
    assert should_post_observation_to_weclaw(_obs(), {}) is False


def test_observation_enabled_by_env(monkeypatch):
    monkeypatch.setenv("AEGIS_ALPHA_WECLAW_ENABLED", "true")
    config = {"weclaw_notification": {"enabled": False}}
    assert should_post_observation_to_weclaw(_obs(), config) is True


def test_observation_important_pushed_when_enabled():
    config = {"weclaw_notification": {"enabled": True}}
    # theme_rotation + actionable_watch + high → important (in default grades)
    assert should_post_observation_to_weclaw(_obs(), config) is True


def test_observation_urgent_pushed():
    config = {"weclaw_notification": {"enabled": True}}
    obs = _obs(observation_type="buy_point_quality", confidence="high")  # → urgent
    assert should_post_observation_to_weclaw(obs, config) is True


def test_observation_watch_suppressed_by_default():
    config = {"weclaw_notification": {"enabled": True}}
    obs = _obs(observation_type="watchlist_observation", stance="monitor_only")  # → watch
    assert should_post_observation_to_weclaw(obs, config) is False


def test_observation_suppress_grade_never_pushed():
    config = {"weclaw_notification": {"enabled": True}}
    obs = _obs(stance="reject")  # → suppress
    assert should_post_observation_to_weclaw(obs, config) is False


def test_observation_grades_configurable():
    config = {"weclaw_notification": {"enabled": True, "observation_grades": ["urgent"]}}
    assert allowed_observation_grades(config) == ("urgent",)
    # important no longer allowed
    assert should_post_observation_to_weclaw(_obs(), config) is False
    assert should_post_observation_to_weclaw(
        _obs(observation_type="buy_point_quality", confidence="high"), config
    ) is True


def test_render_observation_text_follows_plan_format():
    text = render_weclaw_observation_text(_obs(
        evidence=["3只同题材5分钟涨速>5%"],
        counter_evidence=["龙头未封板"],
        data_gaps=["无分钟级主动买盘方向"],
    ))
    assert "[Aegis] 算力主题轮动" in text
    assert "结论：多只算力股盘中拉升" in text
    assert "依据：3只同题材5分钟涨速>5%" in text
    assert "风险/缺口：" in text
    assert "龙头未封板" in text
    assert "无分钟级主动买盘方向" in text
    assert "置信：high" in text


def test_post_observation_posts_to_api(monkeypatch):
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(weclaw_notifier.urllib.request, "urlopen", _fake_urlopen)
    config = {
        "weclaw_notification": {
            "enabled": True,
            "api_url": "http://127.0.0.1:18011/api/send",
            "target": "wxid_test",
        }
    }
    ok = post_observation_to_weclaw(_obs(), config)
    assert ok is True
    assert captured["body"]["to"] == "wxid_test"
    assert "[Aegis]" in captured["body"]["text"]


def test_post_observation_skips_when_grade_not_allowed(monkeypatch):
    called = {"hit": False}

    def _fake_urlopen(request, timeout=0):
        called["hit"] = True
        raise AssertionError("should not POST a suppressed observation")

    monkeypatch.setattr(weclaw_notifier.urllib.request, "urlopen", _fake_urlopen)
    config = {"weclaw_notification": {"enabled": True, "api_url": "http://x", "target": "t"}}
    ok = post_observation_to_weclaw(_obs(stance="reject"), config)
    assert ok is False
    assert called["hit"] is False
