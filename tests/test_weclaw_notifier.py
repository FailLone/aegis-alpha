from __future__ import annotations

import json
import urllib.error

from aegis_alpha.alerts.weclaw_notifier import (
    post_alert_to_weclaw,
    render_weclaw_alert_text,
    should_post_alert_to_weclaw,
)
from aegis_alpha.models import AgentAlert


def _alert(title: str = "BUYPOINT_ALERT 600000") -> AgentAlert:
    return AgentAlert(
        alert_id="alert-1",
        event_id="event-1",
        symbol="600000",
        theme="AI",
        severity="warning",
        title=title,
        body="breakout evidence",
        created_at="2026-06-23T10:00:00+08:00",
    )


def test_weclaw_notification_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_ALPHA_WECLAW_ENABLED", "false")
    assert should_post_alert_to_weclaw(_alert(), {}) is False


def test_weclaw_notification_filters_noisy_event_alerts() -> None:
    config = {"weclaw_notification": {"enabled": True}}

    assert should_post_alert_to_weclaw(_alert("BUYPOINT_ALERT 600000"), config) is True
    assert should_post_alert_to_weclaw(_alert("SELECTION_VALIDATION 2026-06-22->2026-06-23"), config) is True
    assert should_post_alert_to_weclaw(_alert("SEAL_ORDER_DECAY 688268"), config) is False


def test_render_weclaw_alert_text_is_compact() -> None:
    text = render_weclaw_alert_text(_alert())

    assert "[Aegis] BUYPOINT_ALERT 600000" in text
    assert "标的：600000" in text
    assert "依据：breakout evidence" in text


def test_post_alert_to_weclaw_posts_to_api(monkeypatch) -> None:
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    ok = post_alert_to_weclaw(
        _alert(),
        {
            "weclaw_notification": {
                "enabled": True,
                "api_url": "http://127.0.0.1:18011/api/send",
                "target": "user_id@im.wechat",
                "timeout_seconds": 1.5,
            }
        },
    )

    assert ok is True
    assert captured["url"] == "http://127.0.0.1:18011/api/send"
    assert captured["timeout"] == 1.5
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["payload"]["to"] == "user_id@im.wechat"
    assert "BUYPOINT_ALERT 600000" in captured["payload"]["text"]


def test_post_alert_to_weclaw_returns_false_on_http_error(monkeypatch) -> None:
    class _Body:
        def read(self):
            return b"ret=-2"

        def close(self):
            return None

    def _fake_urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="http://127.0.0.1:18011/api/send",
            code=500,
            msg="failed",
            hdrs={},
            fp=_Body(),
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    ok = post_alert_to_weclaw(
        _alert(),
        {
            "weclaw_notification": {
                "enabled": True,
                "target": "user_id@im.wechat",
            }
        },
    )

    assert ok is False
