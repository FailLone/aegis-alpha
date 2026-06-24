from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_real_notification_side_effects(monkeypatch, tmp_path):
    """Tests must opt into mocked delivery instead of using local credentials."""
    from aegis_alpha.alerts import hermes_webhook, weclaw_notifier

    monkeypatch.setenv(
        "AEGIS_ALPHA_PROVIDER_HEALTH_EVENT_PATH",
        str(tmp_path / "provider_health_events.jsonl"),
    )
    monkeypatch.setenv(
        "AEGIS_ALPHA_PROVIDER_HEALTH_STATE_PATH",
        str(tmp_path / "provider_health_state.json"),
    )
    monkeypatch.setattr(hermes_webhook, "post_alert_to_hermes", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(weclaw_notifier, "post_alert_to_weclaw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(weclaw_notifier, "post_observation_to_weclaw", lambda *_args, **_kwargs: False)
