from __future__ import annotations

import json
import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aegis_alpha.alerts.provider_health import (
    classify_provider_error,
    load_provider_failures,
    mark_failure_notified,
    record_provider_failure,
    should_notify_failure,
)
from aegis_alpha.alerts.weclaw_notifier import post_system_message_to_weclaw
from aegis_alpha.adapters.jvquant.queries import JvQuantQueryClient

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "provider_health_watch.py"
_SCRIPT_SPEC = importlib.util.spec_from_file_location("provider_health_watch", _SCRIPT_PATH)
assert _SCRIPT_SPEC and _SCRIPT_SPEC.loader
provider_health_watch = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(provider_health_watch)


def test_provider_error_classification() -> None:
    assert classify_provider_error("HTTP 402: Insufficient Balance") == "balance_or_quota"
    assert classify_provider_error("当前账户积分不足 60000") == "balance_or_quota"
    assert classify_provider_error("This model is not available in your region. 403") == (
        "access_or_entitlement"
    )
    assert classify_provider_error("HTTP 429: rate limit exceeded") == "rate_limit"
    assert classify_provider_error("connection reset by peer") == ""


def test_record_provider_failure_ignores_normal_disconnect(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-health.jsonl"
    monkeypatch.setenv("AEGIS_ALPHA_PROVIDER_HEALTH_EVENT_PATH", str(path))

    assert record_provider_failure(
        provider="jvQuant",
        component="websocket",
        error="connection reset by peer",
    ) is False
    assert path.exists() is False


def test_record_provider_failure_roundtrip(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-health.jsonl"
    monkeypatch.setenv("AEGIS_ALPHA_PROVIDER_HEALTH_EVENT_PATH", str(path))

    assert record_provider_failure(
        provider="jvQuant",
        component="semantic_query",
        error="积分不足",
        occurred_at="2026-06-24T10:00:00+08:00",
    ) is True

    failures = load_provider_failures()
    assert failures[0]["category"] == "balance_or_quota"
    assert failures[0]["provider"] == "jvQuant"


def test_provider_failure_notification_dedup_and_cooldown() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    failure = {
        "provider": "agent_model",
        "component": "observer",
        "category": "balance_or_quota",
        "error": "HTTP 402: Insufficient Balance",
        "occurred_at": "2026-06-24T10:00:00+08:00",
    }
    state: dict = {"sent": {}}
    now = datetime(2026, 6, 24, 10, 1, tzinfo=tz)

    assert should_notify_failure(failure, state, now=now, cooldown_hours=6) is True
    mark_failure_notified(failure, state, notified_at=now.isoformat())
    assert should_notify_failure(failure, state, now=now, cooldown_hours=6) is False

    later_occurrence = {**failure, "occurred_at": "2026-06-24T11:00:00+08:00"}
    assert should_notify_failure(
        later_occurrence,
        state,
        now=datetime(2026, 6, 24, 12, 0, tzinfo=tz),
        cooldown_hours=6,
    ) is False
    assert should_notify_failure(
        later_occurrence,
        state,
        now=datetime(2026, 6, 24, 16, 2, tzinfo=tz),
        cooldown_hours=6,
    ) is True


def test_system_message_bypasses_market_title_filters(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ok = post_system_message_to_weclaw(
        "[Aegis 系统告警] DeepSeek 余额不足",
        {
            "weclaw_notification": {
                "enabled": True,
                "target": "user@im.wechat",
                "api_url": "http://127.0.0.1:18011/api/send",
                "allowed_title_prefixes": ["SELECTION_VALIDATION"],
            }
        },
    )

    assert ok is True
    assert captured["payload"]["text"].startswith("[Aegis 系统告警]")


def test_jvquant_query_records_provider_quota_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "provider-health.jsonl"
    monkeypatch.setenv("AEGIS_ALPHA_PROVIDER_HEALTH_EVENT_PATH", str(path))

    class Client:
        def query(self, *_args):
            raise RuntimeError("当前账户积分不足")

    query_client = JvQuantQueryClient(
        query_rate_per_second=100,
        query_burst=100,
        timeout_seconds=1,
    )

    try:
        query_client.query(Client(), "测试查询")
    except RuntimeError:
        pass
    else:
        raise AssertionError("provider error should propagate")

    failure = json.loads(path.read_text().strip())
    assert failure["provider"] == "jvQuant"
    assert failure["component"] == "semantic_query"
    assert failure["category"] == "balance_or_quota"


def test_gateway_log_detects_primary_failure_even_when_fallback_can_continue(
    tmp_path, monkeypatch
) -> None:
    gateway_log = tmp_path / "gateway.log"
    gateway_log.write_text(
        """
⚠️  API call failed (attempt 1/1): APIStatusError [HTTP 402]
   🔌 Provider: deepseek  Model: deepseek-v4-pro
   📝 Error: HTTP 402: Insufficient Balance
🔄 Primary model failed — switching to fallback: openai/gpt-5.4 via openrouter
""".strip()
    )
    monkeypatch.setenv("HERMES_GATEWAY_LOG_PATH", str(gateway_log))
    state: dict = {}

    failures = provider_health_watch._gateway_log_failures(state)

    assert len(failures) == 1
    assert failures[0]["provider"] == "deepseek"
    assert failures[0]["category"] == "balance_or_quota"
    assert state["gateway_log_offset"] == gateway_log.stat().st_size
    assert provider_health_watch._gateway_log_failures(state) == []
