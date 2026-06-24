from __future__ import annotations

from aegis_alpha.mcp import server
from aegis_alpha.models import MarketEvent, SignalSnapshot


def _store(monkeypatch, tmp_path):
    from aegis_alpha.storage import AegisAlphaStore
    store = AegisAlphaStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(server, "get_store", lambda: store)
    return store


def _event(event_id: str, event_type: str, symbol: str, score: float, theme: str = "AI算力", fresh="fresh"):
    return MarketEvent(
        event_id=event_id, event_type=event_type, symbol=symbol, theme=theme,
        score=score, received_at="2026-06-23T09:40:00+08:00", freshness_status=fresh,
    )


def _snapshot(symbol: str, fresh="fresh"):
    return SignalSnapshot(
        symbol=symbol, data_timestamp="2026-06-23T09:40:00+08:00",
        provider_timestamp="2026-06-23T09:40:00+08:00",
        received_at="2026-06-23T09:40:00+08:00", freshness_status=fresh,
        change_pct=8.8, speed_5m_pct=2.1,
    )


# --- get_realtime_symbol_context ---

def test_symbol_context_empty_db(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    res = server.get_realtime_symbol_context("002491")
    assert res["data_mode"] == "ok"
    assert res["snapshot"] is None
    assert res["recent_event_count"] == 0
    assert res["freshness"] == "no_data"
    assert any("结构化快照" in g for g in res["data_gaps"])


def test_symbol_context_requires_symbol(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    res = server.get_realtime_symbol_context("")
    assert res["data_mode"] == "unavailable"


def test_symbol_context_with_data(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.save_signal_snapshot(_snapshot("002491"))
    store.save_market_events([
        _event("e1", "APPROACHING_LIMIT_UP", "002491", 78.0),
        _event("e2", "BIG_ORDER_INFLOW_SPIKE", "300475", 60.0),  # different symbol
    ])
    res = server.get_realtime_symbol_context("002491")
    assert res["data_mode"] == "ok"
    assert res["snapshot"] is not None
    assert res["snapshot"]["symbol"] == "002491"
    assert res["recent_event_count"] == 1  # only 002491's event
    assert res["recent_events"][0]["event_id"] == "e1"
    assert res["freshness"] == "has_fresh"


def test_symbol_context_honors_recent_window(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.save_market_events([
        MarketEvent(
            event_id="old", event_type="APPROACHING_LIMIT_UP", symbol="002491", theme="AI算力",
            score=90.0, received_at="2026-06-23T09:00:00+08:00", freshness_status="fresh",
        ),
        MarketEvent(
            event_id="new", event_type="BIG_ORDER_INFLOW_SPIKE", symbol="002491", theme="AI算力",
            score=60.0, received_at="2026-06-23T09:40:00+08:00", freshness_status="fresh",
        ),
    ])
    res = server.get_realtime_symbol_context("002491", lookback_minutes=30)
    assert [e["event_id"] for e in res["recent_events"]] == ["new"]


def test_symbol_context_stale_only(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.save_market_events([_event("e1", "APPROACHING_LIMIT_UP", "002491", 50.0, fresh="stale")])
    res = server.get_realtime_symbol_context("002491")
    assert res["freshness"] == "all_stale"


def test_symbol_context_compacts_and_caps_repeated_events(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.save_signal_snapshot(_snapshot("002491"))
    store.save_market_events([
        MarketEvent(
            event_id=f"e{i}",
            event_type="SECOND_BOARD_CANDIDATE_REPRICE",
            symbol="002491",
            theme="AI算力",
            score=float(i),
            evidence=["price", "speed"],
            data={"large": "payload"},
            received_at=f"2026-06-23T09:40:{i:02d}+08:00",
            freshness_status="fresh",
        )
        for i in range(10)
    ])

    res = server.get_realtime_symbol_context("002491")

    assert res["recent_event_count"] == 8
    assert all("data" not in event for event in res["recent_events"])


# --- get_intraday_theme_context ---

def test_theme_context_with_theme_name(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.save_market_events([
        _event("e1", "APPROACHING_LIMIT_UP", "002491", 90.0, theme="AI算力"),
        _event("e2", "BIG_ORDER_INFLOW_SPIKE", "300475", 70.0, theme="AI算力"),
        _event("e3", "SEAL_ORDER_DECAY", "600519", 55.0, theme="白酒"),
    ])
    res = server.get_intraday_theme_context("AI算力")
    assert res["data_mode"] == "ok"
    assert res["theme"] == "AI算力"
    assert res["recent_event_count"] == 2
    assert res["active_symbol_count"] == 2
    assert res["event_count_by_type"]["APPROACHING_LIMIT_UP"] == 1
    assert res["same_theme_events"][0]["symbol"] == "002491"


def test_theme_context_resolves_symbol_theme_from_snapshot(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.save_signal_snapshot(SignalSnapshot(
        symbol="002491", theme="机器人",
        data_timestamp="2026-06-23T09:40:00+08:00",
        provider_timestamp="2026-06-23T09:40:00+08:00",
        received_at="2026-06-23T09:40:00+08:00", freshness_status="fresh",
    ))
    store.save_market_events([
        _event("e1", "APPROACHING_LIMIT_UP", "002491", 90.0, theme="机器人"),
        _event("e2", "BIG_ORDER_INFLOW_SPIKE", "300475", 70.0, theme="机器人"),
    ])
    res = server.get_intraday_theme_context("002491")
    assert res["resolved_from_symbol"] is True
    assert res["theme"] == "机器人"
    assert res["active_symbol_count"] == 2


def test_theme_context_empty(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    res = server.get_intraday_theme_context("AI算力")
    assert res["data_mode"] == "ok"
    assert res["recent_event_count"] == 0
    assert any("无事件" in gap for gap in res["data_gaps"])


# --- get_intraday_market_context ---

def test_market_context_empty_db(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "status_payload", lambda: {"state": "WAITING", "subscribed": []})
    res = server.get_intraday_market_context()
    assert res["data_mode"] == "ok"
    assert res["total_recent_events"] == 0
    assert res["event_count_by_type"] == {}
    assert any("无结构化事件" in g for g in res["data_gaps"])
    assert any("runner 状态" in g for g in res["data_gaps"])


def test_market_context_with_events(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server, "status_payload",
        lambda: {"state": "RUNNING", "subscribed": ["002491", "300475", "600519"]},
    )
    store.save_market_events([
        _event("e1", "APPROACHING_LIMIT_UP", "002491", 90.0),
        _event("e2", "APPROACHING_LIMIT_UP", "300475", 70.0),
        _event("e3", "BIG_ORDER_INFLOW_SPIKE", "600519", 55.0),
    ])
    res = server.get_intraday_market_context()
    assert res["data_mode"] == "ok"
    assert res["runner_state"] == "RUNNING"
    assert res["monitored_symbol_count"] == 3
    assert res["event_count_by_type"]["APPROACHING_LIMIT_UP"] == 2
    assert res["total_recent_events"] == 3
    # strongest first
    assert res["strongest_events"][0]["event_id"] == "e1"
    assert len(res["approaching_limit_up"]) == 2
    assert res["freshness"] == "has_fresh"


def test_market_context_runner_status_failure_soft(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    def _boom():
        raise RuntimeError("status file gone")
    monkeypatch.setattr(server, "status_payload", _boom)
    res = server.get_intraday_market_context()
    assert res["data_mode"] == "ok"
    assert res["runner_state"] == "STOPPED"
