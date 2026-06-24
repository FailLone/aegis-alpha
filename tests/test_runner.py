from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aegis_alpha.runner import (
    AegisAlphaRunner,
    is_trading_session_active,
    load_runner_config,
    reconnect_delay_seconds,
    realtime_feed_health_error,
    status_payload,
    subscription_levels,
    subscription_symbols,
)
from aegis_alpha.models import RealtimeConnectionStatus, RunnerStatus
from aegis_alpha.storage import write_runner_status


def test_runner_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("JVQUANT_SUBSCRIBE_SYMBOLS", raising=False)
    config = load_runner_config()

    assert config["provider"] == "jvQuant"
    assert subscription_symbols(config)
    assert subscription_levels(config) == ["lv1", "lv2", "lv10"]


def test_runner_session_detection() -> None:
    config = load_runner_config()
    tz = ZoneInfo("Asia/Shanghai")

    assert is_trading_session_active(config, datetime(2026, 5, 28, 9, 30, tzinfo=tz))
    assert not is_trading_session_active(config, datetime(2026, 5, 28, 12, 0, tzinfo=tz))


def test_runner_status_payload_when_missing(tmp_path) -> None:
    config_path = tmp_path / "runner.yaml"
    config_path.write_text(
        """
storage:
  status_path: missing-status.json
trading_sessions: []
""".strip()
    )

    payload = status_payload(str(config_path))

    assert payload["state"] == "STOPPED"
    assert payload["notes"]


def test_runner_status_payload_resolves_relative_path_from_project_root(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    status_path = project_root / "data" / "runner_status.json"
    config_path = tmp_path / "runner.yaml"
    config_path.write_text("storage:\n  status_path: data/runner_status.json\n")
    write_runner_status(
        RunnerStatus(state="RUNNING", updated_at="2026-06-24T10:00:00+08:00"),
        status_path,
    )
    monkeypatch.setenv("AEGIS_ALPHA_PROJECT_ROOT", str(project_root))
    monkeypatch.chdir(tmp_path)

    payload = status_payload(str(config_path))

    assert payload["state"] == "RUNNING"


def test_reconnect_delay_uses_exponential_backoff_with_jitter() -> None:
    config = {
        "reconnect_interval_seconds": 10,
        "reconnect_max_interval_seconds": 45,
        "reconnect_jitter_ratio": 0.2,
    }

    assert reconnect_delay_seconds(config, 0, jitter=0.0) == 10
    assert reconnect_delay_seconds(config, 1, jitter=0.0) == 20
    assert reconnect_delay_seconds(config, 2, jitter=0.1) == 44
    assert reconnect_delay_seconds(config, 3, jitter=0.0) == 45


def test_realtime_feed_health_marks_no_messages_after_grace() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    connection = RealtimeConnectionStatus(provider="jvQuant", connected=True)

    error = realtime_feed_health_error(
        {"stale_after_seconds": 180},
        connection,
        connected_at="2026-06-23T09:15:00+08:00",
        now=datetime(2026, 6, 23, 9, 18, 1, tzinfo=tz),
    )

    assert error == "no_realtime_messages_after_181s"


def test_realtime_feed_health_marks_stale_messages() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    connection = RealtimeConnectionStatus(
        provider="jvQuant",
        connected=True,
        last_message_at="2026-06-23T09:15:00+08:00",
    )

    error = realtime_feed_health_error(
        {"stale_after_seconds": 180},
        connection,
        connected_at="2026-06-23T09:14:00+08:00",
        now=datetime(2026, 6, 23, 9, 18, 1, tzinfo=tz),
    )

    assert error == "stale_realtime_messages_after_181s"


def test_realtime_feed_health_graces_message_from_previous_connection() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    connection = RealtimeConnectionStatus(
        provider="jvQuant",
        connected=True,
        last_message_at="2026-06-24T11:30:15+08:00",
    )

    error = realtime_feed_health_error(
        {
            "stale_after_seconds": 180,
            "realtime_no_message_grace_seconds": 180,
        },
        connection,
        connected_at="2026-06-24T13:00:09+08:00",
        now=datetime(2026, 6, 24, 13, 0, 10, tzinfo=tz),
    )

    assert error == ""


def test_runner_persists_buffer_outputs(tmp_path) -> None:
    config_path = tmp_path / "runner.yaml"
    db_path = tmp_path / "runner.db"
    status_path = tmp_path / "runner_status.json"
    config_path.write_text(
        f"""
market: ab
loop_interval_seconds: 5
trading_sessions:
  - name: all_day
    start: "00:00"
    end: "23:59"
subscription:
  default_symbols: ["600000"]
  levels: ["lv1"]
storage:
  sqlite_path: "{db_path}"
  status_path: "{status_path}"
""".strip()
    )
    runner = AegisAlphaRunner(str(config_path), connect=False)
    runner.buffer.add_price("600000", "2026-05-28T09:30:00+08:00", 10.0, 100_000_000, change_pct=8.8)
    runner.buffer.add_price("600000", "2026-05-28T09:35:00+08:00", 10.4, 100_000_000, change_pct=9.4)
    runner.buffer.add_big_order_flow("600000", 50_000_000)

    runner.persist_buffer_outputs(["600000"])

    assert runner.store.signal_snapshot_count("600000") == 1
    assert runner.store.market_event_count() >= 1


def test_runner_expands_runtime_symbols_from_realtime_discovery(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("JVQUANT_SUBSCRIBE_SYMBOLS", "600000")

    config_path = tmp_path / "runner.yaml"
    db_path = tmp_path / "runner.db"
    status_path = tmp_path / "runner_status.json"
    config_path.write_text(
        f"""
market: ab
loop_interval_seconds: 5
trading_sessions:
  - name: all_day
    start: "00:00"
    end: "23:59"
subscription:
  default_symbols: ["600000"]
  levels: ["lv1"]
realtime_discovery:
  enabled: true
  interval_seconds: 30
  max_symbols: 5
  seed_turnover_yi: 30
  include_current_large_turnover: true
  include_current_limitup: true
storage:
  sqlite_path: "{db_path}"
  status_path: "{status_path}"
""".strip()
    )

    class FakeClient:
        def __init__(self) -> None:
            self.connected = False
            self.subscribed: list[str] = []
            self.calls: list[list[str]] = []

        def status(self) -> RealtimeConnectionStatus:
            return RealtimeConnectionStatus(
                provider="jvQuant",
                connected=self.connected,
                subscribed=self.subscribed,
            )

        def subscribe(self, symbols: list[str], levels: list[str]) -> RealtimeConnectionStatus:
            self.connected = True
            self.calls.append(list(symbols))
            for symbol in symbols:
                if symbol not in self.subscribed:
                    self.subscribed.append(symbol)
            return self.status()

        def disconnect(self) -> RealtimeConnectionStatus:
            self.connected = False
            return self.status()

    class FakeAdapter:
        def _query(self, query: str, sort_key: str = "") -> dict:
            return {
                "data": {
                    "fields": ["股票代码", "股票简称", "成交额"],
                    "list": [["000001", "平安银行", 8_000_000_000]],
                }
            }

        def get_limitup_pool(self) -> list:
            return [SimpleNamespace(symbol="002281")]

        def get_theme_leaders(self, theme: str, trading_day: str) -> list:
            return []

    monkeypatch.setattr(
        "aegis_alpha.runner.create_market_data_adapter",
        lambda: FakeAdapter(),
        raising=False,
    )

    runner = AegisAlphaRunner(str(config_path), connect=True)
    fake_client = FakeClient()
    runner.client = fake_client  # type: ignore[assignment]

    status = runner.run_once()

    assert status.state == "RUNNING"
    assert runner._runtime_symbols == ["600000", "000001", "002281"]
    assert fake_client.calls[0] == ["600000"]
    assert fake_client.calls[1] == ["000001", "002281"]


def test_discovered_symbols_use_lv1_only_while_base_keeps_paid_levels(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("JVQUANT_SUBSCRIBE_SYMBOLS", "600000")
    config_path = tmp_path / "runner.yaml"
    config_path.write_text(
        f"""
market: ab
trading_sessions:
  - name: all_day
    start: "00:00"
    end: "23:59"
subscription:
  default_symbols: ["600000"]
  levels: ["lv1", "lv2", "lv10"]
realtime_discovery:
  enabled: true
  max_symbols: 5
  subscription_levels: ["lv1"]
paid_depth_promotion:
  enabled: true
  max_symbols: 2
  levels: ["lv2", "lv10"]
storage:
  sqlite_path: "{tmp_path / 'runner.db'}"
  status_path: "{tmp_path / 'runner_status.json'}"
""".strip()
    )

    class FakeClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[list[str], list[str]]] = []

        def status(self) -> RealtimeConnectionStatus:
            return RealtimeConnectionStatus(provider="jvQuant", connected=self.connected)

        def subscribe(self, symbols: list[str], levels: list[str]) -> RealtimeConnectionStatus:
            self.connected = True
            self.calls.append((list(symbols), list(levels)))
            return self.status()

        def disconnect(self) -> RealtimeConnectionStatus:
            self.connected = False
            return self.status()

    class FakeAdapter:
        def _query(self, query: str, sort_key: str = "") -> dict:
            return {"data": {"fields": ["股票代码", "股票简称", "行业"], "list": [["002491", "通鼎互联", "通信设备"]]}}

        def get_limitup_pool(self) -> list:
            return [SimpleNamespace(symbol="600105", name="永鼎股份", theme="通信设备")]

        def get_theme_leaders(self, theme: str, trading_day: str) -> list:
            return []

    monkeypatch.setattr("aegis_alpha.runner.create_market_data_adapter", lambda: FakeAdapter())
    runner = AegisAlphaRunner(str(config_path), connect=True)
    fake_client = FakeClient()
    runner.client = fake_client  # type: ignore[assignment]

    runner.run_once()

    assert fake_client.calls[0] == (["600000"], ["lv1", "lv2", "lv10"])
    assert fake_client.calls[1] == (["002491", "600105"], ["lv1"])
    assert fake_client.calls[2] == (["002491", "600105"], ["lv2", "lv10"])
    assert runner._symbol_metadata["002491"]["name"] == "通鼎互联"
    assert runner._symbol_metadata["600105"]["theme"] == "通信设备"


def test_high_confidence_candidate_creates_one_investigation_alert_per_symbol_day(tmp_path) -> None:
    from aegis_alpha.models import MarketEvent

    config_path = tmp_path / "runner.yaml"
    config_path.write_text(
        f"""
subscription:
  default_symbols: ["002491"]
  levels: ["lv1"]
candidate_investigation:
  enabled: true
  min_score: 75
storage:
  sqlite_path: "{tmp_path / 'runner.db'}"
  status_path: "{tmp_path / 'runner_status.json'}"
""".strip()
    )
    runner = AegisAlphaRunner(str(config_path), connect=False)

    def event(event_id: str, score: float) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            event_type="SECOND_BOARD_CANDIDATE_REPRICE",
            symbol="002491",
            name="通鼎互联",
            theme="通信设备",
            confidence="high",
            score=score,
            evidence=["candidate current change is 10.01%."],
            provider_timestamp="2026-06-24T11:14:00+08:00",
            received_at="2026-06-24T11:14:00+08:00",
            freshness_status="fresh",
            suggested_agent_action=[],
            data={},
        )

    runner._maybe_alert_from_events([event("low", 74.99)])
    runner._maybe_alert_from_events([event("high-1", 75.03)])
    runner._maybe_alert_from_events([event("high-2", 80.0)])

    alerts = runner.store.list_alerts(status="pending", limit=20)
    candidate_alerts = [a for a in alerts if a.title == "CANDIDATE_INVESTIGATION 002491"]
    assert len(candidate_alerts) == 1
    assert candidate_alerts[0].event_id == "candidate-investigation:2026-06-24:002491"


def test_persist_buffer_outputs_appends_sector_events_when_leader_breaks(tmp_path, monkeypatch):
    """persist_buffer_outputs should fetch theme leaders and append
    THEME_LEADER_BREAK_BOARD events when a leader's status is 'broken'."""
    from unittest.mock import MagicMock
    from aegis_alpha.models import ThemeLeader
    from aegis_alpha.runner import AegisAlphaRunner

    config_path = tmp_path / "runner.yaml"
    db_path = tmp_path / "runner.db"
    config_path.write_text(
        f"""
market: ab
loop_interval_seconds: 5
trading_sessions:
  - name: all_day
    start: "00:00"
    end: "23:59"
subscription:
  default_symbols: ["600000"]
  levels: ["lv1"]
storage:
  sqlite_path: "{db_path}"
  status_path: "{tmp_path / 'runner_status.json'}"
""".strip()
    )

    # Ensure the runner builds; we then patch internals.
    runner = AegisAlphaRunner(config_path=str(config_path), connect=False)

    # No buffer snapshots → detector contributes 0 events.
    # We just need persist_buffer_outputs to call the sector-event detector.

    broken_leader = ThemeLeader(
        theme="AI", trading_day="2026-06-01",
        leader_symbol="600519", leader_name="L",
        leader_consecutive_boards=3,
        leader_first_limit_up_time="09:30:00",
        leader_seal_amount_cny=200_000_000.0,
        leader_status="broken",
        co_leader_symbols=[],
        member_count=4,
    )

    fake_adapter = MagicMock()
    fake_adapter.get_theme_leaders = MagicMock(return_value=[broken_leader])

    monkeypatch.setattr(
        "aegis_alpha.runner.create_market_data_adapter",
        lambda: fake_adapter,
        raising=False,
    )

    captured: list = []
    runner._maybe_alert_from_events = lambda events: captured.extend(events)  # type: ignore[assignment]

    runner.persist_buffer_outputs(["600000"])

    types = {e.event_type for e in captured}
    assert "THEME_LEADER_BREAK_BOARD" in types


def test_maybe_alert_from_events_includes_p6_event_types(tmp_path, monkeypatch):
    """P6/P7 added 3 new MarketEventType values; runner alert pipeline must
    surface them. Otherwise THEME_LEADER_BREAK_BOARD / SECTOR_ROTATION /
    MARKET_BOTTOM_REVERSAL events are detected and silently dropped."""
    from aegis_alpha.models import MarketEvent
    from aegis_alpha.runner import AegisAlphaRunner

    config_path = tmp_path / "runner.yaml"
    db_path = tmp_path / "runner.db"
    config_path.write_text(
        f"""
market: ab
loop_interval_seconds: 5
trading_sessions:
  - name: all_day
    start: "00:00"
    end: "23:59"
subscription:
  default_symbols: ["600000"]
  levels: ["lv1"]
storage:
  sqlite_path: "{db_path}"
  status_path: "{tmp_path / 'runner_status.json'}"
""".strip()
    )
    runner = AegisAlphaRunner(config_path=str(config_path), connect=False)

    triggered: list[str] = []

    def _capture(_alert):
        triggered.append(_alert.title)

    monkeypatch.setattr("aegis_alpha.runner.notify_macos", _capture, raising=False)
    import aegis_alpha.alerts.notifier as notifier_mod

    monkeypatch.setattr(notifier_mod, "notify_macos", _capture, raising=False)

    events = [
        MarketEvent(
            event_id=f"e{i}",
            event_type=event_type,  # type: ignore[arg-type]
            symbol="600519",
            name="x",
            theme="AI",
            confidence="medium",
            score=70.0,
            evidence=["test"],
            provider_timestamp="2026-06-01T09:30:00+08:00",
            received_at="2026-06-01T09:30:00+08:00",
            freshness_status="fresh",
            suggested_agent_action=[],
            data={},
        )
        for i, event_type in enumerate(
            [
                "THEME_LEADER_BREAK_BOARD",
                "SECTOR_ROTATION",
                "MARKET_BOTTOM_REVERSAL",
            ]
        )
    ]
    runner._maybe_alert_from_events(events)
    assert len(triggered) == 3, (
        f"all 3 P6 event types should trigger notify_macos; got titles: {triggered}"
    )

    runner._maybe_alert_from_events(events)
    assert len(triggered) == 3, "persisted event alerts must not be delivered twice"


def test_collect_sector_events_preserves_partial_results_when_one_detector_fails(
    tmp_path, monkeypatch
):
    """If detect_theme_leader_break_board succeeds but detect_sector_rotation
    raises, the break_board events should NOT be dropped."""
    from unittest.mock import MagicMock

    from aegis_alpha.models import ThemeLeader
    from aegis_alpha.runner import AegisAlphaRunner

    config_path = tmp_path / "runner.yaml"
    db_path = tmp_path / "runner.db"
    config_path.write_text(
        f"""
market: ab
loop_interval_seconds: 5
trading_sessions:
  - name: all_day
    start: "00:00"
    end: "23:59"
subscription:
  default_symbols: ["600000"]
  levels: ["lv1"]
storage:
  sqlite_path: "{db_path}"
  status_path: "{tmp_path / 'runner_status.json'}"
""".strip()
    )
    runner = AegisAlphaRunner(config_path=str(config_path), connect=False)

    broken_leader = ThemeLeader(
        theme="AI",
        trading_day="2026-06-01",
        leader_symbol="600519",
        leader_name="L",
        leader_consecutive_boards=3,
        leader_first_limit_up_time="09:30:00",
        leader_seal_amount_cny=200_000_000.0,
        leader_status="broken",
        co_leader_symbols=[],
        member_count=4,
    )
    fake_adapter = MagicMock()
    fake_adapter.get_theme_leaders = MagicMock(return_value=[broken_leader])
    monkeypatch.setattr(
        "aegis_alpha.runner.create_market_data_adapter",
        lambda: fake_adapter,
        raising=False,
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("rotation detector exploded")

    monkeypatch.setattr(
        "aegis_alpha.runner.detect_sector_rotation", _explode, raising=False,
    )

    events = runner._collect_sector_events()
    types = {e.event_type for e in events}
    assert "THEME_LEADER_BREAK_BOARD" in types


def test_collect_sector_events_caches_adapter_instance(tmp_path, monkeypatch):
    """create_market_data_adapter should be called at most once across multiple
    runner ticks, not once per tick."""
    from unittest.mock import MagicMock

    from aegis_alpha.models import ThemeLeader
    from aegis_alpha.runner import AegisAlphaRunner

    config_path = tmp_path / "runner.yaml"
    db_path = tmp_path / "runner.db"
    config_path.write_text(
        f"""
market: ab
loop_interval_seconds: 5
trading_sessions:
  - name: all_day
    start: "00:00"
    end: "23:59"
subscription:
  default_symbols: ["600000"]
  levels: ["lv1"]
storage:
  sqlite_path: "{db_path}"
  status_path: "{tmp_path / 'runner_status.json'}"
""".strip()
    )
    runner = AegisAlphaRunner(config_path=str(config_path), connect=False)

    fake_adapter = MagicMock()
    fake_adapter.get_theme_leaders = MagicMock(
        return_value=[
            ThemeLeader(
                theme="AI",
                trading_day="2026-06-01",
                leader_symbol="600519",
                leader_name="L",
                leader_consecutive_boards=2,
                leader_first_limit_up_time="09:30:00",
                leader_seal_amount_cny=100_000_000.0,
                leader_status="sealed",
                co_leader_symbols=[],
                member_count=2,
            )
        ]
    )
    factory = MagicMock(return_value=fake_adapter)
    monkeypatch.setattr(
        "aegis_alpha.runner.create_market_data_adapter", factory, raising=False,
    )

    runner._collect_sector_events()
    runner._collect_sector_events()
    runner._collect_sector_events()

    assert factory.call_count == 1, (
        f"adapter factory should be called once across 3 ticks; got {factory.call_count}"
    )
