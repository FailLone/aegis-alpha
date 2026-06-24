from __future__ import annotations

import json

from aegis_alpha.mcp import server


def _store(monkeypatch, tmp_path):
    from aegis_alpha.storage import AegisAlphaStore
    store = AegisAlphaStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(server, "get_store", lambda: store)
    return store


def test_record_and_get_observation_roundtrip(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    rec = server.record_agent_observation(
        trading_day="2026-06-23",
        title="算力主题轮动",
        summary="多只算力股盘中拉升",
        source="periodic_market_scan",
        observation_type="theme_rotation",
        theme="AI算力",
        stance="actionable_watch",
        confidence="medium",
        evidence_json=json.dumps(["3只同题材5分钟涨速>5%"]),
        data_gaps_json=json.dumps(["无分钟级主动买盘方向"]),
    )
    assert rec["data_mode"] == "ok"
    assert rec["notification_grade"] == "important"
    assert rec["observation_quality"] == "ok"
    oid = rec["observation_id"]

    got = server.get_agent_observation(oid)
    assert got["data_mode"] == "ok"
    assert got["theme"] == "AI算力"
    assert got["notification_grade"] == "important"


def test_get_observation_unavailable(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    res = server.get_agent_observation("nope")
    assert res["data_mode"] == "unavailable"


def test_notify_observation_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AEGIS_ALPHA_WECLAW_ENABLED", "false")
    _store(monkeypatch, tmp_path)
    rec = server.record_agent_observation(
        trading_day="2026-06-23",
        title="可通知观察",
        summary="同题材多只标的拉升",
        observation_type="theme_rotation",
        theme="AI算力",
        stance="actionable_watch",
        confidence="medium",
        evidence_json=json.dumps(["APPROACHING_LIMIT_UP count=2"]),
        data_gaps_json=json.dumps(["主动买盘方向为代理数据"]),
    )
    res = server.notify_agent_observation(rec["observation_id"])
    assert res["data_mode"] == "ok"
    assert res["notification_grade"] == "important"
    assert res["eligible"] is False
    assert res["posted"] is False
    assert any("disabled" in note for note in res["notes"])


def test_notify_observation_missing_id(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    res = server.notify_agent_observation("missing")
    assert res["data_mode"] == "unavailable"
    assert res["posted"] is False


def test_record_flags_incomplete_when_no_evidence(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    rec = server.record_agent_observation(
        trading_day="2026-06-23",
        title="缺依据的观察",
        observation_type="watchlist_observation",
    )
    assert rec["observation_quality"] == "incomplete"
    assert any("evidence" in w for w in rec["observation_quality_warnings"])
    assert any("data_gaps" in w for w in rec["observation_quality_warnings"])
    assert any("summary" in w for w in rec["observation_quality_warnings"])


def test_record_dedup_same_facts_one_record(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    first = server.record_agent_observation(
        trading_day="2026-06-23", title="first",
        observation_type="theme_rotation", theme="AI算力",
        stance="actionable_watch", confidence="high",
        evidence_json=json.dumps(["x"]), data_gaps_json=json.dumps(["y"]),
        summary="s",
    )
    second = server.record_agent_observation(
        trading_day="2026-06-23", title="second-ignored",
        observation_type="theme_rotation", theme="AI算力",
        stance="reject", confidence="low",
        evidence_json=json.dumps(["x"]), data_gaps_json=json.dumps(["y"]),
        summary="s",
    )
    assert first["observation_id"] == second["observation_id"]
    # dedup returns the FIRST stored content
    assert second["title"] == "first"
    listed = server.list_agent_observations(trading_day="2026-06-23")
    assert listed["count"] == 1


def test_list_observations_filters(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    server.record_agent_observation(
        trading_day="2026-06-23", title="t1",
        observation_type="theme_rotation", theme="A",
        evidence_json=json.dumps(["e"]), data_gaps_json=json.dumps(["g"]), summary="s",
    )
    server.record_agent_observation(
        trading_day="2026-06-23", title="t2",
        observation_type="buy_point_quality", symbol="002491",
        evidence_json=json.dumps(["e"]), data_gaps_json=json.dumps(["g"]), summary="s",
    )
    res = server.list_agent_observations(observation_type="buy_point_quality")
    assert res["count"] == 1
    assert res["observations"][0]["symbol"] == "002491"


def test_record_reject_stance_suppresses(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    rec = server.record_agent_observation(
        trading_day="2026-06-23", title="噪声触发",
        observation_type="noise_or_rejected_trigger",
        stance="reject", confidence="high",
        evidence_json=json.dumps(["e"]), data_gaps_json=json.dumps(["g"]), summary="s",
    )
    assert rec["notification_grade"] == "suppress"


def test_record_normalizes_unknown_source_without_alert_to_periodic_scan(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    rec = server.record_agent_observation(
        trading_day="2026-06-23",
        title="周期观察",
        summary="有事实",
        source="cron_observer",
        evidence_json=json.dumps(["e"]),
        data_gaps_json=json.dumps(["g"]),
    )
    assert rec["source"] == "periodic_market_scan"


def test_record_normalizes_unknown_source_with_alert_to_trigger_enrichment(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    rec = server.record_agent_observation(
        trading_day="2026-06-23",
        title="告警增强",
        summary="有事实",
        source="webhook_alert",
        linked_alert_ids_json=json.dumps(["alert-1"]),
        evidence_json=json.dumps(["e"]),
        data_gaps_json=json.dumps(["g"]),
    )
    assert rec["source"] == "trigger_enrichment"


def test_record_normalizes_unknown_observation_type_from_stance(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    rec = server.record_agent_observation(
        trading_day="2026-06-23",
        title="封单风险",
        summary="封单快速衰减",
        observation_type="break_board_risk",
        stance="reject",
        evidence_json=json.dumps(["e"]),
        data_gaps_json=json.dumps(["g"]),
    )
    assert rec["observation_type"] == "noise_or_rejected_trigger"
    assert rec["notification_grade"] == "suppress"
    assert any("observation_type normalized" in note for note in rec["input_normalizations"])
