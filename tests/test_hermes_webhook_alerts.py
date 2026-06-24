from __future__ import annotations

import hashlib
import hmac
import json

from aegis_alpha.alerts.hermes_webhook import build_hermes_alert_payload, post_alert_to_hermes
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
        created_at="2026-06-21T10:00:00+08:00",
    )


def test_build_hermes_payload_uses_aegis_event_type() -> None:
    payload = build_hermes_alert_payload(_alert())

    assert payload["event_type"] == "aegis.buy_point_alert"
    assert payload["summary"]["symbol"] == "600000"
    assert payload["alert"]["event_id"] == "event-1"


def test_post_alert_to_hermes_signs_like_hermes_webhook(monkeypatch) -> None:
    captured = {}

    class _Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _fake_urlopen(request, timeout):
        body = request.data
        secret = "secret-1"
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        captured["timeout"] = timeout
        captured["url"] = request.full_url
        captured["event"] = request.headers["X-github-event"]
        captured["delivery"] = request.headers["X-github-delivery"]
        captured["signature"] = request.headers["X-hub-signature-256"]
        captured["payload"] = json.loads(body.decode("utf-8"))
        assert captured["signature"] == expected
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    ok = post_alert_to_hermes(
        _alert(),
        {
            "hermes_webhook": {
                "enabled": True,
                "url": "http://127.0.0.1:8644/webhooks/aegis-alerts",
                "secret": "secret-1",
                "timeout_seconds": 1.5,
            }
        },
    )

    assert ok is True
    assert captured["timeout"] == 1.5
    assert captured["url"] == "http://127.0.0.1:8644/webhooks/aegis-alerts"
    assert captured["event"] == "aegis.buy_point_alert"
    assert captured["delivery"] == "event-1"
    assert captured["payload"]["summary"]["title"] == "BUYPOINT_ALERT 600000"


def test_post_alert_to_hermes_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_ALPHA_HERMES_WEBHOOK_ENABLED", "false")

    called = False

    def _fake_urlopen(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    assert post_alert_to_hermes(_alert(), {}) is False
    assert called is False
