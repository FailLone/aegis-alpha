"""Tests for Task 6.3 — window-gated live buy-point detection + dedup paper alert.

TDD: written BEFORE the implementation. RED → GREEN workflow.

Covers:
- buffer.rolling_points() public accessor (CHANGE A)
- detect_buypoints_in_window() — outside window gate (returns [])
- detect_buypoints_in_window() — inside window + firing pattern → one alert
- dedup: second call with same data → no second alert (event_id dedup)
- previous_high resolution: fact-first vs opening_window_fallback
- no-order safety: alert body must not contain imperative order directives
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from aegis_alpha.agent_eval import PROHIBITED_DIRECTIVE_PATTERNS
from aegis_alpha.alerts.store import AlertStore
from aegis_alpha.measurements.buypoint_replay import replay_buypoint
from aegis_alpha.models import BuyPointThresholds, MinuteReplayBar, MinuteReplaySnapshot

SH_TZ = ZoneInfo("Asia/Shanghai")

# ---------------------------------------------------------------------------
# Shared thresholds — same as test_buypoint_replay to guarantee a firing
# ---------------------------------------------------------------------------
THRESHOLDS = BuyPointThresholds(
    breakout_volume_ratio_min=1.5,
    pullback_volume_shrink_max=0.7,
    resurge_strength_min=0.5,
    pullback_max_drawdown_pct=5.0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(tmp_path: Path) -> object:
    """Build an AegisAlphaRunner with connect=False and a fresh SQLite store."""
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
  default_symbols: ["000001"]
  levels: ["lv1"]
storage:
  sqlite_path: "{db_path}"
  status_path: "{tmp_path / 'runner_status.json'}"
""".strip()
    )
    return AegisAlphaRunner(str(config_path), connect=False)


def _seed_firing_pattern(runner, symbol: str = "000001") -> None:
    """Inject a breakout→pullback→resurge tick sequence into the buffer.

    Uses CUMULATIVE turnover (turnover_is_cumulative=True, the production default)
    so that rolling_points_to_minute_bars produces the exact per-bar volumes
    expected by the default BuyPointThresholds.

    One tick per minute (at :30s within the minute) so each tick lands in its
    own minute bucket.  The per-minute delta between consecutive ticks gives us:
      bar 09:31: volume = 0   (first bar — no prior baseline)
      bar 09:32: volume = 100  (300-200 cumulative delta)
      bar 09:33: volume = 100  (400-300)
        → baseline_volume = mean([0, 100, 100]) = 66.67
      bar 09:34: volume = 200  (600-400) → ratio 200/66.67 ≈ 3.0 >= 1.5 ✓
      bar 09:35: volume =  60  (660-600) → shrink 60/200=0.30 <= 0.7 ✓
      bar 09:36: volume =  50  (710-660) → shrink 50/200=0.25 <= 0.7 ✓
      bar 09:37: volume =  80  (790-710) → resurge strength = (10.10-9.90)/(10.20-9.90)=0.667 >= 0.5 ✓

    previous_high = 10.0 (provided via opening_window_fallback: max of baseline bars)
    NOTE: max of 09:31-09:33 bars = 9.85 < 10.0, so the breakout at 10.20 clears it.
    We therefore set previous_high via fallback to 9.85 — and 10.20 > 9.85, so the
    breakout fires correctly regardless.
    """
    day = "2026-06-12"
    # (timestamp, price, CUMULATIVE_turnover)
    ticks = [
        (f"{day}T09:31:30+08:00",  9.80, 200.0),  # cumulative = 200
        (f"{day}T09:32:30+08:00",  9.82, 300.0),  # delta = 100
        (f"{day}T09:33:30+08:00",  9.85, 400.0),  # delta = 100 → baseline mean = (0+100+100)/3 = 66.67
        (f"{day}T09:34:30+08:00", 10.20, 600.0),  # delta = 200 → ratio 3.0 >= 1.5 ✓
        (f"{day}T09:35:30+08:00", 10.05, 660.0),  # delta = 60  → shrink 0.30 ✓
        (f"{day}T09:36:30+08:00",  9.90, 710.0),  # delta = 50  → shrink 0.25 ✓
        (f"{day}T09:37:30+08:00", 10.10, 790.0),  # delta = 80  → resurge ✓
    ]
    for ts, price, vol in ticks:
        runner.buffer.add_price(symbol, ts, price, vol)


def _make_dt(hhmm: str) -> datetime:
    """Return a Shanghai datetime for 2026-06-12 at HH:MM."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 6, 12, h, m, 0, tzinfo=SH_TZ)


# ---------------------------------------------------------------------------
# CHANGE A: buffer.rolling_points() public accessor
# ---------------------------------------------------------------------------

class TestRollingPointsAccessor:
    def test_returns_empty_for_unknown_symbol(self, tmp_path):
        runner = _make_runner(tmp_path)
        result = runner.buffer.rolling_points("UNKNOWN")
        assert result == []

    def test_returns_all_added_points(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.buffer.add_price("000001", "2026-06-12T09:31:00+08:00", 10.0, 100.0)
        runner.buffer.add_price("000001", "2026-06-12T09:32:00+08:00", 10.5, 200.0)
        pts = runner.buffer.rolling_points("000001")
        assert len(pts) == 2
        assert pts[0] == ("2026-06-12T09:31:00+08:00", 10.0, 100.0)
        assert pts[1] == ("2026-06-12T09:32:00+08:00", 10.5, 200.0)

    def test_returns_copy_mutation_does_not_affect_buffer(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.buffer.add_price("000001", "2026-06-12T09:31:00+08:00", 10.0, 100.0)
        pts = runner.buffer.rolling_points("000001")
        pts.clear()  # mutate the returned list
        # buffer should still have 1 point
        assert len(runner.buffer.rolling_points("000001")) == 1


# ---------------------------------------------------------------------------
# CHANGE B: detect_buypoints_in_window — outside window → []
# ---------------------------------------------------------------------------

class TestOutsideWindow:
    def test_outside_window_returns_empty_no_alert(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)

        # Monkeypatch now_dt to 10:00 — outside both default windows
        monkeypatch.setattr(
            "aegis_alpha.runner.now_dt",
            lambda: _make_dt("10:00"),
        )
        # Also patch in measurements module if needed
        from aegis_alpha.measurements import minute_bars as _mb_mod
        import aegis_alpha.runner as runner_mod

        result = runner_mod.AegisAlphaRunner.__dict__["detect_buypoints_in_window"](
            runner, ["000001"]
        )
        assert result == [], f"Expected [] outside window but got {result}"

        # No alert should have been persisted
        alert_store = AlertStore(runner.store)
        pending = alert_store.list_recent(limit=50)
        bp_alerts = [a for a in pending if "BUYPOINT_ALERT" in a.title]
        assert len(bp_alerts) == 0


# ---------------------------------------------------------------------------
# CHANGE B: inside window + buy-point pattern → exactly one alert
# ---------------------------------------------------------------------------

class TestInsideWindowFiring:
    def test_inside_window_fires_one_alert(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)

        # Monkeypatch now_dt to 09:40 — inside open_drive window
        monkeypatch.setattr("aegis_alpha.runner.now_dt", lambda: _make_dt("09:40"))

        # Patch the adapter so previous_high comes from opening_window_fallback
        # (no candidates → fallback path)
        monkeypatch.setattr(
            "aegis_alpha.runner.create_market_data_adapter",
            lambda: _make_empty_adapter(),
            raising=False,
        )

        result = runner.detect_buypoints_in_window(["000001"])

        assert len(result) >= 1, f"Expected at least 1 signal but got: {result}"

        # Verify one alert was persisted
        alert_store = AlertStore(runner.store)
        recent = alert_store.list_recent(limit=50)
        bp_alerts = [a for a in recent if "BUYPOINT_ALERT" in a.title]
        assert len(bp_alerts) == 1, f"Expected 1 alert, got {len(bp_alerts)}"

        # Alert body must contain previous_high_source=
        assert "previous_high_source=" in bp_alerts[0].body, (
            f"Alert body missing previous_high_source=: {bp_alerts[0].body!r}"
        )

    def test_inside_window_alert_body_has_source_fact(self, tmp_path, monkeypatch):
        """When a candidate with previous_high_price>0 exists, source=fact."""
        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)
        monkeypatch.setattr("aegis_alpha.runner.now_dt", lambda: _make_dt("09:40"))

        # Use a simple mock object rather than constructing a full SecondBoardCandidate
        # (which has many required fields).  The runner only reads .symbol and
        # .previous_high_price from candidates, so a MagicMock spec is sufficient.
        candidate = MagicMock()
        candidate.symbol = "000001"
        candidate.previous_high_price = 10.0  # positive — triggers the fact path

        fake_adapter = MagicMock()
        fake_adapter.get_second_board_candidates = MagicMock(return_value=[candidate])
        monkeypatch.setattr(
            "aegis_alpha.runner.create_market_data_adapter",
            lambda: fake_adapter,
            raising=False,
        )

        result = runner.detect_buypoints_in_window(["000001"])
        assert len(result) >= 1

        alert_store = AlertStore(runner.store)
        recent = alert_store.list_recent(limit=50)
        bp_alerts = [a for a in recent if "BUYPOINT_ALERT" in a.title]
        assert len(bp_alerts) == 1
        assert "previous_high_source=fact" in bp_alerts[0].body, (
            f"Expected source=fact but got: {bp_alerts[0].body!r}"
        )

    def test_inside_window_alert_body_has_source_opening_fallback(self, tmp_path, monkeypatch):
        """When no matching candidate, source=opening_window_fallback."""
        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)
        monkeypatch.setattr("aegis_alpha.runner.now_dt", lambda: _make_dt("09:40"))
        monkeypatch.setattr(
            "aegis_alpha.runner.create_market_data_adapter",
            lambda: _make_empty_adapter(),
            raising=False,
        )

        result = runner.detect_buypoints_in_window(["000001"])
        assert len(result) >= 1

        alert_store = AlertStore(runner.store)
        recent = alert_store.list_recent(limit=50)
        bp_alerts = [a for a in recent if "BUYPOINT_ALERT" in a.title]
        assert "previous_high_source=opening_window_fallback" in bp_alerts[0].body, (
            f"Expected opening_window_fallback but got: {bp_alerts[0].body!r}"
        )


def test_reference_candidates_are_cached_between_runner_cycles(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path)
    candidate = MagicMock(symbol="000001", previous_high_price=10.0)
    adapter = MagicMock()
    adapter.get_second_board_candidates.return_value = [candidate]
    runner._sector_events_adapter = adapter

    first = runner._reference_candidates_for_buypoints()
    second = runner._reference_candidates_for_buypoints()

    assert first == [candidate]
    assert second == [candidate]
    assert adapter.get_second_board_candidates.call_count == 1


# ---------------------------------------------------------------------------
# CHANGE B: dedup — second call with identical data → still only one alert
# ---------------------------------------------------------------------------

class TestDedup:
    def test_dedup_second_call_no_duplicate_alert_or_delivery(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)
        monkeypatch.setattr("aegis_alpha.runner.now_dt", lambda: _make_dt("09:40"))
        monkeypatch.setattr(
            "aegis_alpha.runner.create_market_data_adapter",
            lambda: _make_empty_adapter(),
            raising=False,
        )
        delivered: list[str] = []
        monkeypatch.setattr(
            "aegis_alpha.alerts.weclaw_notifier.post_alert_to_weclaw",
            lambda alert, _config: delivered.append(alert.event_id) or True,
        )

        runner.detect_buypoints_in_window(["000001"])
        runner.detect_buypoints_in_window(["000001"])

        alert_store = AlertStore(runner.store)
        recent = alert_store.list_recent(limit=50)
        bp_alerts = [a for a in recent if "BUYPOINT_ALERT" in a.title]
        assert len(bp_alerts) == 1, (
            f"Dedup failed: expected 1 alert after 2 calls, got {len(bp_alerts)}"
        )
        assert delivered == ["buypoint:000001:09:37"]

    def test_historical_replay_trigger_is_not_delivered(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)
        runner.buffer.add_price(
            "000001", "2026-06-12T09:38:30+08:00", 10.08, 850.0
        )
        monkeypatch.setattr("aegis_alpha.runner.now_dt", lambda: _make_dt("09:40"))
        monkeypatch.setattr(
            "aegis_alpha.runner.create_market_data_adapter",
            lambda: _make_empty_adapter(),
            raising=False,
        )

        assert runner.detect_buypoints_in_window(["000001"]) == []
        assert AlertStore(runner.store).list_recent(limit=50) == []


# ---------------------------------------------------------------------------
# No-order safety: alert body must not contain imperative directives
# ---------------------------------------------------------------------------

class TestNoOrderSafety:
    def test_alert_body_has_no_order_directives(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)
        monkeypatch.setattr("aegis_alpha.runner.now_dt", lambda: _make_dt("09:40"))
        monkeypatch.setattr(
            "aegis_alpha.runner.create_market_data_adapter",
            lambda: _make_empty_adapter(),
            raising=False,
        )

        result = runner.detect_buypoints_in_window(["000001"])

        # Non-vacuous guard: must have a signal FIRST
        assert len(result) >= 1, (
            "No buy-point signal fired — test would be vacuous. "
            "Check the seeded pattern in _seed_firing_pattern()."
        )

        alert_store = AlertStore(runner.store)
        recent = alert_store.list_recent(limit=50)
        bp_alerts = [a for a in recent if "BUYPOINT_ALERT" in a.title]
        assert len(bp_alerts) >= 1

        for alert in bp_alerts:
            full_text = alert.title + " " + alert.body
            for pattern in PROHIBITED_DIRECTIVE_PATTERNS:
                match = re.search(pattern, full_text)
                assert match is None, (
                    f"Prohibited directive pattern {pattern!r} matched in alert: {full_text!r}"
                )


# ---------------------------------------------------------------------------
# run_once integration: buying window detection called from run_once,
# but its failure must NOT kill the runner cycle
# ---------------------------------------------------------------------------

class TestRunOnceIntegration:
    def test_detect_failure_does_not_kill_run_once(self, tmp_path, monkeypatch):
        """If detect_buypoints_in_window raises, run_once must still return a status."""
        import aegis_alpha.runner as runner_mod

        runner = _make_runner(tmp_path)
        _seed_firing_pattern(runner)
        monkeypatch.setattr("aegis_alpha.runner.now_dt", lambda: _make_dt("09:40"))
        monkeypatch.setattr(
            "aegis_alpha.runner.create_market_data_adapter",
            lambda: _make_empty_adapter(),
            raising=False,
        )

        # Make detect explode
        def _explode(*args, **kwargs):
            raise RuntimeError("simulated buypoint detection failure")

        runner.detect_buypoints_in_window = _explode  # type: ignore[assignment]

        # run_once should NOT raise
        status = runner.run_once()
        assert status is not None
        assert status.state in {"RUNNING", "WAITING", "DEGRADED"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_empty_adapter():
    """Return a mock adapter whose get_second_board_candidates returns []."""
    fake = MagicMock()
    fake.get_second_board_candidates = MagicMock(return_value=[])
    return fake
