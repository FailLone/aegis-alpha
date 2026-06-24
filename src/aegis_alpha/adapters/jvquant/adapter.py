from __future__ import annotations

import logging
import json
import os
import time
from collections import Counter
from datetime import datetime
from typing import Any

from aegis_alpha.adapters.mock_market_data import MockMarketDataAdapter
from aegis_alpha.alerts.provider_health import record_provider_failure
from aegis_alpha.adapters.jvquant import parsers as P
from aegis_alpha.adapters.jvquant import historical_second_board as HSB
from aegis_alpha.adapters.jvquant.parsers import float_or_zero as _float_or_zero
from aegis_alpha.adapters.jvquant.parsers import int_or_zero as _int_or_zero
from aegis_alpha.adapters.jvquant.queries import JvQuantQueryClient
from aegis_alpha.adapters.jvquant.candidates import build_one_candidate
from aegis_alpha.clock import SH_TZ, now_iso as _now
from aegis_alpha.models import (
    AuctionAnalysis,
    BreakBoardStock,
    CandidateExplanation,
    CandidateOutcomeReview,
    CapitalFlowSlice,
    ContrarianPoolEntry,
    DragonTigerRecord,
    EventScoringConfig,
    HistoryStats,
    LadderEntry,
    LimitUpStock,
    MarketEmotion,
    MarketSentimentGate,
    MarketSnapshot,
    MarketEvent,
    MinuteReplaySnapshot,
    NewStockCandidate,
    OrderbookQueueLevel,
    SealTimeline,
    SealTimelineEvent,
    SecondBoardCandidate,
    SignalSnapshot,
    SimilarSetupResult,
    StockOrderbookSnapshot,
    StockRealtimeSnapshot,
    SuspendedStock,
    ThemeLeader,
    WeeklyPosition,
)
from aegis_alpha.events import EventDetector, SignalWindowBuffer, freshness_status, load_event_scoring_config
from aegis_alpha.measurements.theme_lifecycle import STAGE_LABELS_CN
from aegis_alpha.measurements.orderflow_proxy import simulate_historical_orderflow_proxy as simulate_orderflow_proxy
from aegis_alpha.extensions.suspended_stocks import is_symbol_suspended
from aegis_alpha.extensions.weekly_position import compute_weekly_health_score
from aegis_alpha.storage import AegisAlphaStore
from aegis_alpha.adapters.jvquant_websocket import JvQuantRealtimeClient, raw_lv2_large_trade_records
from aegis_alpha.symbols import daily_limit_pct, normalize_symbol
from aegis_alpha.themes.auction import AuctionAnalyzer
from aegis_alpha.themes.ladder import classify_height
from aegis_alpha.themes.leader import ThemeLeaderResolver



def _inferred_change_pct_for_limit_up(symbol: str) -> float:
    """Return the daily-limit percentage to infer when seal metrics indicate limit-up."""
    return daily_limit_pct(symbol)


def _day_query_prefix(trading_day: str) -> str:
    day = (trading_day or "").strip()
    today = datetime.now(SH_TZ).date().isoformat()
    return "今日" if not day or day == today else day


def _time_lte(value: str, boundary: str) -> bool:
    return bool(value and boundary and value <= boundary)


def _intraday_theme_copump(
    item: dict[str, Any],
    day_results: list[dict[str, Any]],
    *,
    triggered_at: str,
) -> dict[str, Any]:
    symbol = normalize_symbol(str(item.get("symbol") or ""))
    theme = str(item.get("theme") or "unknown")
    same_theme = [
        result
        for result in day_results
        if normalize_symbol(str(result.get("symbol") or "")) != symbol
        and str(result.get("theme") or "unknown") == theme
    ]
    crossed = []
    triggered = []
    opening = []
    for peer in same_theme:
        diagnostics = peer.get("pattern_diagnostics") or {}
        first_cross_time = str(diagnostics.get("first_cross_time") or "")
        first_triggered_at = str(peer.get("first_triggered_at") or "")
        opening_cross_time = str(diagnostics.get("opening_window_cross_time") or "")
        if _time_lte(first_cross_time, triggered_at):
            crossed.append(peer)
        if _time_lte(first_triggered_at, triggered_at):
            triggered.append(peer)
        if _time_lte(opening_cross_time, triggered_at):
            opening.append(peer)

    return {
        "theme": theme,
        "universe": "same_theme_strategy_watchlist_candidates",
        "same_theme_candidate_count": len(same_theme),
        "crossed_previous_high_by_trigger_count": len(crossed),
        "triggered_by_trigger_count": len(triggered),
        "opening_breakout_by_trigger_count": len(opening),
        "crossed_symbols": [normalize_symbol(str(peer.get("symbol") or "")) for peer in crossed[:10]],
        "triggered_symbols": [normalize_symbol(str(peer.get("symbol") or "")) for peer in triggered[:10]],
        "notes": [
            "Counts only same-theme names present in this strategy watchlist sample.",
            "This is a market-internal co-pump proxy; full-board realtime breadth is not connected yet.",
        ],
    }


class JvQuantMarketDataAdapter:
    """Read-only jvQuant adapter for single-symbol smoke and MCP tools."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("JVQUANT_TOKEN", "")
        if not self.token:
            record_provider_failure(
                provider="jvQuant",
                component="semantic_client",
                error="JVQUANT_TOKEN missing",
            )
            raise ValueError("JVQUANT_TOKEN missing")
        self._fallback = MockMarketDataAdapter()
        self._client: Any | None = None
        cache_ttl = _float_or_zero(os.environ.get("AEGIS_ALPHA_JVQUANT_CACHE_TTL_SECONDS")) or 30.0
        query_rate = _float_or_zero(os.environ.get("AEGIS_ALPHA_JVQUANT_QUERY_RATE_PER_SECOND")) or 3.0
        query_burst = _float_or_zero(os.environ.get("AEGIS_ALPHA_JVQUANT_QUERY_BURST")) or 6.0
        query_timeout = _float_or_zero(os.environ.get("AEGIS_ALPHA_JVQUANT_QUERY_TIMEOUT_SECONDS")) or 10.0
        self._query_client = JvQuantQueryClient(
            cache_ttl_seconds=cache_ttl,
            query_rate_per_second=query_rate,
            query_burst=query_burst,
            timeout_seconds=query_timeout,
        )
        self._query_cache = self._query_client.cache
        self._query_limiter = self._query_client.limiter

    @classmethod
    def from_env(cls) -> "JvQuantMarketDataAdapter":
        return cls(token=os.environ.get("JVQUANT_TOKEN", ""))

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from jvQuant import sql_client

                self._client = sql_client.Construct(token=self.token, log_level=logging.ERROR)
            except Exception as exc:
                record_provider_failure(
                    provider="jvQuant",
                    component="semantic_client",
                    error=str(exc) or type(exc).__name__,
                )
                raise
        return self._client

    def get_market_snapshot(self) -> MarketSnapshot:
        limitup_pool = self.get_limitup_pool()
        break_pool = self.get_break_board_pool()
        limit_up_count = len(limitup_pool)
        break_board_count = len(break_pool)
        denominator = limit_up_count + break_board_count
        break_board_rate = round(break_board_count / denominator, 4) if denominator else 0.0
        themes = P._leading_themes(limitup_pool + break_pool)

        total_payload = self._query(
            "主板,非ST,股票代码,股票简称,涨跌幅,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        total_count = P._query_count(total_payload)

        return MarketSnapshot(
            market="A-share",
            trading_day=datetime.now(SH_TZ).date().isoformat(),
            timestamp=_now(),
            data_mode="live_provider",
            provider="jvQuant",
            limit_up_count=limit_up_count,
            break_board_count=break_board_count,
            break_board_rate=break_board_rate,
            leading_themes=themes,
            notes=[
                "Read-only jvQuant semantic query snapshot.",
                f"main_board_non_st_count={total_count}",
                "Limit-up pool includes jvQuant semantic-query first seal time and seal amount when available.",
            ],
        )

    def get_market_sentiment_gate(self) -> MarketSentimentGate:
        snapshot = self.get_market_snapshot()
        risk_flags: list[str] = []
        positive_signals: list[str] = []

        if snapshot.break_board_rate >= 0.45:
            risk_flags.append("Break-board rate is high; board-chasing should be defensive.")
        elif snapshot.break_board_rate >= 0.30:
            risk_flags.append("Break-board rate is elevated; only selective monitoring is reasonable.")
        else:
            positive_signals.append("Break-board rate is controlled by the current provider snapshot.")

        if snapshot.limit_up_count >= 60:
            positive_signals.append("Limit-up count is strong.")
        elif snapshot.limit_up_count >= 35:
            positive_signals.append("Limit-up count supports selective watchlist work.")
        else:
            risk_flags.append("Limit-up count is not strong enough for broad board-chasing.")

        if len(snapshot.leading_themes) >= 3:
            positive_signals.append("Leading themes are not overly narrow.")
        else:
            risk_flags.append("Theme breadth is narrow in the current snapshot.")

        if not risk_flags:
            risk_flags.append("Provider semantic-query data should still be cross-checked during active trading.")

        emotion = self.get_market_emotion(snapshot.trading_day)

        return MarketSentimentGate(
            trading_day=snapshot.trading_day,
            timestamp=snapshot.timestamp,
            data_mode="live_provider",
            provider="jvQuant",
            limit_up_count=snapshot.limit_up_count,
            break_board_rate=snapshot.break_board_rate,
            second_board_success_rate=0.0,
            hot_theme_count=len(snapshot.leading_themes),
            risk_flags=risk_flags,
            positive_signals=positive_signals,
            conclusion=(
                "jvQuant read-only market gate is usable for coarse filtering. "
                "Second-board success rate and intraday seal-time quality are not derived yet."
            ),
            yesterday_limitup_today_premium_pct=emotion.yesterday_limitup_today_premium_pct,
            consecutive_boards_alive_rate=emotion.yesterday_consecutive_boards_alive_rate,
            first_to_second_promotion_rate=emotion.first_to_second_promotion_rate,
            second_to_third_promotion_rate=emotion.second_to_third_promotion_rate,
            max_height_today=emotion.max_height_today,
        )

    def get_limitup_pool(self) -> list[LimitUpStock]:
        payload = self._query(
            "今日涨停,非ST,股票代码,股票简称,涨跌幅,首次涨停时间,封单金额,封单量,涨停封成比,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        rows = P._query_rows(payload)
        return [P._limitup_from_row(row) for row in rows]

    def get_break_board_pool(self) -> list[BreakBoardStock]:
        payload = self._query(
            "炸板,非ST,股票代码,股票简称,涨跌幅,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        rows = P._query_rows(payload)
        return [P._break_board_from_row(row) for row in rows]

    def get_stock_history_limitup_stats(self, symbol: str):
        return self._fallback.get_stock_history_limitup_stats(symbol)

    def get_history_stats(self, symbol: str) -> HistoryStats:
        from datetime import timedelta
        from aegis_alpha.feedback.history_stats import compute_history_stats

        normalized = normalize_symbol(symbol)
        end = datetime.now(SH_TZ).date()
        start = end - timedelta(days=365 * 3)
        return compute_history_stats(
            store=AegisAlphaStore(),
            symbol=normalized,
            start_day=start.isoformat(),
            end_day=end.isoformat(),
        )

    def get_theme_strength(self, symbol: str):
        return self._fallback.get_theme_strength(symbol)

    def get_theme_leaders(self, theme: str = "", trading_day: str = "") -> list[ThemeLeader]:
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        store = AegisAlphaStore()
        stored = store.latest_theme_leaders(theme=theme, trading_day=day, limit=20)
        if stored:
            return stored
        pool = self.get_limitup_pool()
        ladder = {
            stock.symbol: LadderEntry(
                symbol=stock.symbol,
                trading_day=day,
                consecutive_boards=1,
                height_label="first_board",
                last_limit_up_day=day,
            )
            for stock in pool
        }
        leaders = ThemeLeaderResolver().resolve(pool, ladder, trading_day=day)
        if theme:
            leaders = [leader for leader in leaders if leader.theme == theme]
        store.save_theme_leaders(leaders)
        return leaders

    def get_limit_up_ladder(self, symbol: str, trading_day: str = "") -> LadderEntry:
        normalized = normalize_symbol(symbol)
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        store = AegisAlphaStore()
        stored = store.get_ladder_entry(normalized, day)
        if stored is not None:
            return stored
        is_limit_up = any(normalize_symbol(item.symbol) == normalized for item in self.get_limitup_pool())
        consecutive = 1 if is_limit_up else 0
        entry = LadderEntry(
            symbol=normalized,
            trading_day=day,
            consecutive_boards=consecutive,
            height_label=classify_height(consecutive),
            last_limit_up_day=day if is_limit_up else "",
            notes=["Live jvQuant ladder uses current limit-up pool only until historical replay is connected."],
        )
        store.save_ladder_entries([entry])
        return entry

    def get_market_emotion(self, trading_day: str = "") -> MarketEmotion:
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        limitup_pool = self.get_limitup_pool()
        ladder_entries = [
            self.get_limit_up_ladder(stock.symbol, day) for stock in limitup_pool
        ]
        max_height = max(
            (entry.consecutive_boards for entry in ladder_entries),
            default=0,
        )
        return MarketEmotion(
            trading_day=day,
            yesterday_limitup_today_premium_pct=0.0,
            yesterday_consecutive_boards_alive_count=0,
            yesterday_consecutive_boards_total=0,
            yesterday_consecutive_boards_alive_rate=0.0,
            first_to_second_promotion_rate=0.0,
            second_to_third_promotion_rate=0.0,
            first_board_to_consecutive_ratio=0.0,
            max_height_today=max_height,
            notes=[
                "yesterday_limitup_today_premium_pct: requires yesterday limit-up cohort with today's change; not implemented yet.",
                "yesterday_consecutive_boards_*: requires yesterday session history; not implemented yet.",
                "first_to_second / second_to_third promotion rates: requires multi-day ladder history; not implemented yet.",
                "max_height_today computed from ladder of today's limit-up pool.",
            ],
        )

    def get_auction_analysis(self, symbol: str, trading_day: str = "") -> AuctionAnalysis:
        normalized = normalize_symbol(symbol)
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        for candidate in self.get_second_board_candidates():
            if normalize_symbol(candidate.symbol) == normalized:
                return AuctionAnalyzer().analyze(
                    symbol=normalized,
                    trading_day=day,
                    auction_change_pct=candidate.auction_change_pct,
                    auction_turnover_cny=candidate.auction_turnover_cny,
                    auction_turnover_rate=candidate.auction_turnover_rate,
                )
        return AuctionAnalyzer().analyze(symbol=normalized, trading_day=day)

    def get_event_scoring_config(self) -> EventScoringConfig:
        return load_event_scoring_config()

    def get_realtime_connection_status(self):
        return JvQuantRealtimeClient(token=self.token, market=os.environ.get("JVQUANT_MARKET", "ab")).status()

    def get_signal_snapshot(self, symbol: str) -> SignalSnapshot:
        normalized = normalize_symbol(symbol)
        store = AegisAlphaStore()
        stored = store.latest_signal_snapshot(normalized)
        if stored is not None:
            return stored

        for candidate in self.get_second_board_candidates():
            if candidate.symbol == normalized or normalize_symbol(candidate.symbol) == normalized:
                snapshot = self._signal_snapshot_from_candidate(candidate)
                store.save_signal_snapshot(snapshot)
                return snapshot

        realtime = self.get_stock_realtime_snapshot(symbol)
        snapshot = SignalSnapshot(
            symbol=normalized,
            name=realtime.name,
            provider=realtime.provider,
            data_mode=realtime.data_mode,
            price=realtime.last_price,
            change_pct=realtime.change_pct,
            big_order_net_inflow_cny=realtime.big_order_net_inflow_cny,
            orderbook_quality_score=realtime.bid_quality_score,
            data_timestamp=realtime.timestamp,
            provider_timestamp=realtime.timestamp,
            received_at=_now(),
            freshness_status="fresh",
            notes=[
                "Single-symbol signal snapshot built from realtime snapshot fallback.",
                "Speed windows are unavailable unless the symbol is in the second-board candidate pool or realtime buffer.",
            ],
        )
        store.save_signal_snapshot(snapshot)
        return snapshot

    def get_recent_market_events(self, limit: int = 20, event_type: str | None = None) -> list[MarketEvent]:
        store = AegisAlphaStore()
        stored = store.recent_market_events(limit, event_type)
        if stored:
            return stored

        snapshots = [self._signal_snapshot_from_candidate(candidate) for candidate in self.get_second_board_candidates()]
        detector = EventDetector(self.get_event_scoring_config())
        events: list[MarketEvent] = []
        for snapshot in snapshots:
            events.extend(detector.detect_from_snapshot(snapshot))
        events.extend(detector.detect_theme_cluster(snapshots))
        from aegis_alpha.seal_timeline.divergence import detect_theme_divergence
        from aegis_alpha.seal_timeline.tracker import SealTimelineTracker
        tracker = SealTimelineTracker(store)
        trading_day = datetime.now(SH_TZ).date().isoformat()
        leaders = self.get_theme_leaders(trading_day=trading_day)
        divergence_events = detect_theme_divergence(leaders, tracker, trading_day=trading_day)
        store.save_market_events(divergence_events)
        events.extend(divergence_events)
        store.save_market_events(events)
        recent = store.recent_market_events(limit, event_type)
        if not recent:
            recent = events[: max(1, min(int(limit or 20), 100))]
        return recent

    def explain_market_event(self, event_id: str) -> dict:
        events = {event.event_id: event for event in self.get_recent_market_events(limit=100)}
        event = events.get(event_id)
        if event is None:
            return {
                "event_id": event_id,
                "data_mode": "unavailable",
                "error": "Market event not found in recent event store.",
                "disclaimer": "Data source unavailable. Do not infer missing market data.",
            }
        return {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "theme": event.theme,
            "score": event.score,
            "confidence": event.confidence,
            "freshness_status": event.freshness_status,
            "reason": (
                f"{event.event_type} scored {event.score:.1f}. "
                "The event is generated by Aegis Alpha signal rules before agent interpretation."
            ),
            "evidence": event.evidence,
            "suggested_agent_action": event.suggested_agent_action,
            "provider_timestamp": event.provider_timestamp,
            "received_at": event.received_at,
            "disclaimer": "Research and watchlist output only. This is not investment advice or an order instruction.",
        }

    def review_candidate_outcome(self, symbol: str, trading_day: str) -> CandidateOutcomeReview:
        return AegisAlphaStore().get_review_outcome(normalize_symbol(symbol), trading_day)

    def record_candidate_outcome(self, review: CandidateOutcomeReview) -> CandidateOutcomeReview:
        review.symbol = normalize_symbol(review.symbol)
        return AegisAlphaStore().save_review_outcome(review)

    def get_seal_timeline(self, symbol: str, trading_day: str = "") -> SealTimeline:
        from aegis_alpha.seal_timeline.tracker import SealTimelineTracker
        normalized = normalize_symbol(symbol)
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        return SealTimelineTracker(AegisAlphaStore()).get_timeline(normalized, day)

    def record_seal_timeline_event(self, event: SealTimelineEvent) -> SealTimelineEvent:
        from aegis_alpha.seal_timeline.tracker import SealTimelineTracker
        return SealTimelineTracker(AegisAlphaStore()).record(event)

    def get_second_board_candidates(self):
        payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,5分钟涨幅,资金流向,主力资金,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        seal_payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,首次涨停时间,封单量,封单金额,涨停封成比,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        speed_1m_payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,1分钟涨幅,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        speed_3m_payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,3分钟涨幅,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        speed_10m_payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,10分钟涨幅,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        auction_payload = self._query(
            "昨日涨停,非ST,股票代码,股票简称,竞价涨幅,竞价成交额,竞价换手率,开盘价,价格,成交额,行业",
            sort_key="竞价涨幅",
        )
        theme_payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,所属概念,概念,题材,行业,价格,成交额",
            sort_key="涨跌幅",
        )
        break_reseal_payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,炸板次数,首次炸板时间,回封次数,最后封板时间,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        max_seal_payload = self._query(
            "昨日涨停,今日涨幅大于5,非ST,股票代码,股票简称,涨跌幅,最大封单金额,最大封单量,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        rows = P._query_rows(payload)
        seal_rows = P._rows_by_symbol(P._query_rows(seal_payload))
        speed_1m_rows = P._rows_by_symbol(P._query_rows(speed_1m_payload))
        speed_3m_rows = P._rows_by_symbol(P._query_rows(speed_3m_payload))
        speed_10m_rows = P._rows_by_symbol(P._query_rows(speed_10m_payload))
        auction_rows = P._rows_by_symbol(P._query_rows(auction_payload))
        theme_rows = P._rows_by_symbol(P._query_rows(theme_payload))
        break_reseal_rows = P._rows_by_symbol(P._query_rows(break_reseal_payload))
        max_seal_rows = P._rows_by_symbol(P._query_rows(max_seal_payload))
        query_timestamp = _now()
        max_candidates = _int_or_zero(os.environ.get("AEGIS_ALPHA_SECOND_BOARD_MAX_CANDIDATES")) or 12
        orderbook_limit = _int_or_zero(os.environ.get("AEGIS_ALPHA_SECOND_BOARD_ORDERBOOK_LIMIT")) or 5
        minute_replay_enabled = os.environ.get("AEGIS_ALPHA_ENABLE_MINUTE_REPLAY", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        minute_replay_limit = _int_or_zero(os.environ.get("AEGIS_ALPHA_SECOND_BOARD_MINUTE_REPLAY_LIMIT")) or 12
        theme_counts = Counter(P._theme_from_row(row) for row in rows)
        trading_day = query_timestamp[:10]
        try:
            _suspended_today = self.get_suspended_stocks(trading_day)
        except Exception:
            _suspended_today = []
        ladder_entries: dict[str, LadderEntry] = {}
        for row in rows[:max_candidates]:
            row_symbol = P._symbol_from_row(row)
            if not row_symbol:
                continue
            ladder_entries[row_symbol] = self.get_limit_up_ladder(row_symbol, trading_day)

        theme_leaders_list = self.get_theme_leaders(trading_day=trading_day)
        theme_leaders_by_theme: dict[str, ThemeLeader] = {
            leader.theme: leader for leader in theme_leaders_list
        }

        history_stats_by_symbol: dict[str, HistoryStats] = {}
        for row in rows[:max_candidates]:
            row_symbol = P._symbol_from_row(row)
            if not row_symbol:
                continue
            history_stats_by_symbol[row_symbol] = self.get_history_stats(row_symbol)

        candidates: list[SecondBoardCandidate] = []
        for index, row in enumerate(rows[:max_candidates]):
            row_symbol = P._symbol_from_row(row)
            if row_symbol and is_symbol_suspended(
                row_symbol, trading_day=trading_day, suspended=_suspended_today,
            ):
                continue
            try:
                weekly_pos = self.get_weekly_position(row_symbol)
                weekly_score = compute_weekly_health_score(weekly_pos)
            except Exception:
                weekly_score = 50.0
            candidate = build_one_candidate(
                index=index,
                row=row,
                seal_rows=seal_rows,
                speed_1m_rows=speed_1m_rows,
                speed_3m_rows=speed_3m_rows,
                speed_10m_rows=speed_10m_rows,
                auction_rows=auction_rows,
                theme_rows=theme_rows,
                break_reseal_rows=break_reseal_rows,
                max_seal_rows=max_seal_rows,
                query_timestamp=query_timestamp,
                theme_counts=theme_counts,
                orderbook_limit=orderbook_limit,
                minute_replay_enabled=minute_replay_enabled,
                minute_replay_limit=minute_replay_limit,
                get_minute_replay=self.get_stock_minute_replay_snapshot,
                get_orderbook=self.get_stock_orderbook_snapshot,
                ladder_entries=ladder_entries,
                theme_leaders_by_theme=theme_leaders_by_theme,
                history_stats_by_symbol=history_stats_by_symbol,
                weekly_health_score=weekly_score,
            )
            candidates.append(candidate)

        return candidates

    def get_historical_second_board_candidates(self, trading_day: str, limit: int = 50) -> list[dict[str, Any]]:
        safe_day = trading_day.strip()
        safe_limit = max(1, min(int(limit or 50), 200))
        calendar = HSB.resolve_adjacent_trading_days(self.client, safe_day, require_next=False)
        if not calendar.get("ok"):
            return [
                {
                    "trading_day": safe_day,
                    "provider": "jvQuant",
                    "data_mode": "unavailable",
                    "error": calendar.get("error", "Unable to resolve adjacent trading days."),
                }
            ]

        prev_day = str(calendar["prev_day"])
        next_day = str(calendar["next_day"])
        query = HSB.historical_candidate_query(safe_day, prev_day)
        payload = self._query(query, sort_key="涨跌幅")
        fields = HSB.payload_fields(payload)
        if not HSB.has_target_day_candidate_facts(fields, safe_day):
            return [
                {
                    "trading_day": safe_day,
                    "prev_day": prev_day,
                    "next_day": next_day,
                    "provider": "jvQuant",
                    "data_mode": "unavailable",
                    "error": "jvQuant did not return target-day dated candidate facts.",
                    "query": query,
                    "source_fields": fields[:40],
                }
            ]

        rows = P._query_rows(payload)
        candidates: list[dict[str, Any]] = []
        for row in rows[:safe_limit]:
            if not P._symbol_from_row(row):
                continue
            candidates.append(
                HSB.build_historical_candidate(
                    row,
                    trading_day=safe_day,
                    prev_day=prev_day,
                    next_day=next_day,
                    query=query,
                )
            )
        return candidates

    def get_historical_first_board_watchlist(self, as_of_day: str, limit: int = 50) -> list[dict[str, Any]]:
        safe_day = as_of_day.strip()
        safe_limit = max(1, min(int(limit or 50), 200))
        calendar = HSB.resolve_adjacent_trading_days(self.client, safe_day, require_next=False)
        if not calendar.get("ok"):
            return [
                {
                    "as_of_day": safe_day,
                    "provider": "jvQuant",
                    "data_mode": "unavailable",
                    "error": calendar.get("error", "Unable to resolve adjacent trading days."),
                }
            ]

        prev_day = str(calendar["prev_day"])
        target_day = str(calendar.get("next_day") or "")
        query = HSB.historical_first_board_watchlist_query(safe_day, prev_day)
        payload = self._query(query, sort_key="涨停封单额")
        prev_payload = self._query(HSB.historical_limit_up_symbols_query(prev_day))
        prev_limit_up_symbols = {
            normalize_symbol(P._symbol_from_row(row))
            for row in P._query_rows(prev_payload)
            if P._symbol_from_row(row)
        }
        fields = HSB.payload_fields(payload)
        if not HSB.has_as_of_watchlist_facts(fields, safe_day):
            return [
                {
                    "as_of_day": safe_day,
                    "prev_day": prev_day,
                    "target_second_board_day": target_day,
                    "provider": "jvQuant",
                    "data_mode": "unavailable",
                    "error": "jvQuant did not return as-of dated first-board watchlist facts.",
                    "query": query,
                    "source_fields": fields[:40],
                }
            ]

        rows = P._query_rows(payload)
        watchlist: list[dict[str, Any]] = []
        for row in rows:
            symbol = normalize_symbol(P._symbol_from_row(row))
            if not symbol:
                continue
            previous_day_limit_up = symbol in prev_limit_up_symbols
            if previous_day_limit_up:
                continue
            item = HSB.build_historical_first_board_watchlist_item(
                row,
                as_of_day=safe_day,
                prev_day=prev_day,
                target_second_board_day=target_day,
                query=query,
                previous_day_limit_up=previous_day_limit_up,
            )
            watchlist.append(item)
            if len(watchlist) >= safe_limit:
                break
        return watchlist

    def get_strategy_watchlist(self, as_of_day: str, limit: int = 50) -> list[dict[str, Any]]:
        safe_day = as_of_day.strip()
        safe_limit = max(1, min(int(limit or 50), 100))
        calendar = HSB.resolve_adjacent_trading_days(self.client, safe_day, require_next=False)
        if not calendar.get("ok"):
            return [
                {
                    "as_of_day": safe_day,
                    "provider": "jvQuant",
                    "data_mode": "unavailable",
                    "error": calendar.get("error", "Unable to resolve adjacent trading days."),
                }
            ]

        prev_day = str(calendar["prev_day"])
        target_day = str(calendar.get("next_day") or "")
        seed_items_by_symbol: dict[str, dict[str, Any]] = {}

        for item in self.get_historical_first_board_watchlist(safe_day, 100):
            symbol = normalize_symbol(str(item.get("symbol") or ""))
            if not symbol:
                continue
            seed_items_by_symbol[symbol] = {
                **item,
                "candidate_sources": ["first_board_watchlist"],
                "strategy_seed_reasons": ["as_of_day_first_board"],
                "strategy_seed_queries": [item.get("query", "")],
            }

        trend_queries = HSB.historical_large_turnover_strategy_queries(safe_day)
        if not calendar.get("next_day_known"):
            trend_queries = [*trend_queries, *HSB.current_large_turnover_strategy_queries()]
        trend_source_fields: list[str] = []
        for trend_query in trend_queries:
            trend_payload = self._query(trend_query, sort_key="成交额")
            trend_fields = HSB.payload_fields(trend_payload)
            if not trend_source_fields:
                trend_source_fields = trend_fields
            if not HSB.has_large_turnover_strategy_facts(trend_fields, safe_day):
                continue
            for row in P._query_rows(trend_payload):
                symbol = normalize_symbol(P._symbol_from_row(row))
                if not symbol:
                    continue
                trend_item = HSB.build_large_turnover_strategy_item(
                    row,
                    as_of_day=safe_day,
                    prev_day=prev_day,
                    target_day=target_day,
                    query=trend_query,
                )
                if "@" not in trend_query:
                    trend_item["data_mode"] = "current_provider_as_of"
                    trend_item.setdefault("notes", []).append(
                        "Current semantic query seed; date is inferred from provider field names and kline facts."
                    )
                if symbol in seed_items_by_symbol:
                    existing = seed_items_by_symbol[symbol]
                    existing["candidate_sources"] = sorted(
                        set(existing.get("candidate_sources", [])) | {"large_turnover_trend_seed"}
                    )
                    existing["strategy_seed_reasons"] = sorted(
                        set(existing.get("strategy_seed_reasons", [])) | {"as_of_day_turnover_seed"}
                    )
                    existing["strategy_seed_queries"] = list(
                        dict.fromkeys(
                            query
                            for query in [*existing.get("strategy_seed_queries", []), trend_query]
                            if query
                        )
                    )
                    existing.setdefault("trend_turnover_cny", trend_item.get("turnover_cny", 0.0))
                    existing.setdefault("trend_change_pct", trend_item.get("change_pct", 0.0))
                else:
                    seed_items_by_symbol[symbol] = {
                        **trend_item,
                        "candidate_sources": ["large_turnover_trend_seed"],
                        "strategy_seed_reasons": ["as_of_day_turnover_seed"],
                        "strategy_seed_queries": [trend_query],
                    }

        base_items = list(seed_items_by_symbol.values())
        if not base_items:
            return [
                {
                    "as_of_day": safe_day,
                    "target_second_board_day": target_day,
                    "provider": "jvQuant",
                    "data_mode": "unavailable",
                    "error": "No strategy seed candidates returned by first-board or large-turnover universe queries.",
                    "trend_queries": trend_queries,
                    "trend_source_fields": trend_source_fields[:40],
                }
            ]

        theme_counts = Counter(str(item.get("theme") or "unknown") for item in base_items)
        first_board_theme_counts = Counter(
            str(item.get("theme") or "unknown")
            for item in base_items
            if "first_board_watchlist" in item.get("candidate_sources", [])
        )
        continuity_map = HSB.build_theme_continuity_map(
            self.client,
            self._query,
            themes=list(theme_counts.keys()),
            as_of_day=safe_day,
            lookback_days=14,
        )
        enriched: list[dict[str, Any]] = []
        for item in base_items:
            symbol = str(item.get("symbol") or "")
            if not symbol:
                continue
            strategy_facts = HSB.strategy_facts_from_kline(self.client, symbol, as_of_day=safe_day)
            theme = str(item.get("theme") or "unknown")
            continuity = continuity_map.get(
                theme,
                HSB.summarize_theme_continuity(
                    theme=theme,
                    as_of_day=safe_day,
                    days=[],
                    daily_counts={},
                ),
            )
            enriched_item = {
                **item,
                **strategy_facts,
                "strategy_filter_pass": bool(strategy_facts.get("avg_turnover_10d_pass")),
                "same_theme_strategy_seed_count": theme_counts.get(theme, 0),
                "same_theme_first_board_count": first_board_theme_counts.get(theme, 0),
                "theme_continuity": {
                    **continuity,
                    "same_theme_strategy_seed_count": theme_counts.get(theme, 0),
                    "same_theme_first_board_count": first_board_theme_counts.get(theme, 0),
                },
                "strategy_coverage": {
                    "avg_turnover_10d": strategy_facts.get("strategy_data_mode") == "historical_provider",
                    "prev_day_shrink": strategy_facts.get("strategy_data_mode") == "historical_provider",
                    "previous_high_break": strategy_facts.get("strategy_data_mode") == "historical_provider",
                    "theme_two_week_continuity": continuity.get("data_mode") == "historical_provider",
                    "intraday_big_order_ratio": False,
                    "cls_news_alignment": False,
                },
            }
            if enriched_item["strategy_filter_pass"]:
                enriched.append(enriched_item)
        return enriched[:safe_limit]

    def get_strategy_items_for_symbols(self, as_of_day: str, symbols: list[str]) -> list[dict[str, Any]]:
        safe_day = as_of_day.strip()
        calendar = HSB.resolve_adjacent_trading_days(self.client, safe_day)
        if not calendar.get("ok"):
            return [
                {
                    "as_of_day": safe_day,
                    "provider": "jvQuant",
                    "data_mode": "unavailable",
                    "error": calendar.get("error", "Unable to resolve adjacent trading days."),
                }
            ]
        prev_day = str(calendar["prev_day"])
        target_day = str(calendar["next_day"])
        output: list[dict[str, Any]] = []
        for raw_symbol in symbols:
            symbol = normalize_symbol(raw_symbol)
            if not symbol:
                continue
            payload = self._query(
                (
                    f"{symbol},股票代码,股票简称,涨跌幅@{safe_day},"
                    f"收盘价@{safe_day},最高价@{safe_day},成交额@{safe_day},行业"
                ),
                sort_key="成交额",
            )
            row = next(
                (
                    item for item in P._query_rows(payload)
                    if normalize_symbol(P._symbol_from_row(item)) == symbol
                ),
                {},
            )
            strategy_facts = HSB.strategy_facts_from_kline(self.client, symbol, as_of_day=safe_day)
            theme = P._theme_from_row(row) if row else "unknown"
            continuity = (
                self.get_theme_continuity(theme, safe_day, 14)
                if theme != "unknown"
                else HSB.summarize_theme_continuity(theme=theme, as_of_day=safe_day, days=[], daily_counts={})
            )
            output.append(
                {
                    "symbol": symbol,
                    "name": P._name_from_row(row) if row else symbol,
                    "as_of_day": safe_day,
                    "prev_day": prev_day,
                    "target_second_board_day": target_day,
                    "provider": "jvQuant",
                    "data_mode": "historical_provider" if row else "partial_historical_provider",
                    "candidate_sources": ["requested_symbol_fast_path"],
                    "strategy_seed_reasons": ["user_requested_symbol"],
                    "change_pct": P.float_or_zero(P._field_value(row, "涨跌幅")) if row else 0.0,
                    "close_price": P.float_or_zero(P._field_value(row, "收盘价", "价格", "最新价")) if row else 0.0,
                    "as_of_high_price": P.float_or_zero(P._field_value(row, "最高价", "最高")) if row else 0.0,
                    "turnover_cny": P._parse_cny_amount(P._field_value(row, "成交额")) if row else 0.0,
                    "theme": theme,
                    **strategy_facts,
                    "strategy_filter_pass": bool(strategy_facts.get("avg_turnover_10d_pass")),
                    "same_theme_strategy_seed_count": 0,
                    "same_theme_first_board_count": 0,
                    "theme_continuity": {
                        **continuity,
                        "same_theme_strategy_seed_count": 0,
                        "same_theme_first_board_count": 0,
                    },
                    "strategy_coverage": {
                        "avg_turnover_10d": strategy_facts.get("strategy_data_mode") == "historical_provider",
                        "prev_day_shrink": strategy_facts.get("strategy_data_mode") == "historical_provider",
                        "previous_high_break": strategy_facts.get("strategy_data_mode") == "historical_provider",
                        "theme_two_week_continuity": continuity.get("data_mode") == "historical_provider",
                        "intraday_big_order_ratio": False,
                        "cls_news_alignment": False,
                    },
                    "notes": [
                        "Requested-symbol fast path; does not scan the full strategy universe.",
                        "No program grade or promotion probability is assigned.",
                    ],
                }
            )
        return output

    def get_theme_continuity(
        self,
        theme: str,
        as_of_day: str,
        lookback_days: int = 14,
    ) -> dict[str, Any]:
        safe_theme = theme.strip()
        safe_day = as_of_day.strip()
        if not safe_theme:
            return {
                "data_mode": "unavailable",
                "theme": safe_theme,
                "as_of_day": safe_day,
                "error": "theme is required",
            }
        continuity = HSB.build_theme_continuity_map(
            self.client,
            self._query,
            themes=[safe_theme],
            as_of_day=safe_day,
            lookback_days=lookback_days,
        )
        return continuity.get(
            safe_theme,
            HSB.summarize_theme_continuity(
                theme=safe_theme,
                as_of_day=safe_day,
                days=[],
                daily_counts={},
            ),
        )

    def run_historical_strategy_replay(
        self,
        as_of_day: str,
        target_day: str,
        symbols: list[str] | None = None,
        limit: int = 10,
        window_start: str = "",
        window_end: str = "",
    ) -> dict[str, Any]:
        from aegis_alpha.measurements.historical_strategy_replay import (
            run_historical_strategy_replay_from_items,
        )

        safe_as_of = as_of_day.strip()
        safe_target = target_day.strip()
        safe_limit = max(1, min(int(limit or 10), 50))
        selected_symbols = {normalize_symbol(symbol) for symbol in (symbols or []) if normalize_symbol(symbol)}
        lookup_limit = 100 if selected_symbols else safe_limit
        items = self.get_strategy_watchlist(safe_as_of, lookup_limit)
        if selected_symbols:
            items = [item for item in items if normalize_symbol(str(item.get("symbol") or "")) in selected_symbols]

        result = run_historical_strategy_replay_from_items(
            as_of_day=safe_as_of,
            target_day=safe_target,
            strategy_items=items,
            get_snapshot=lambda symbol, day: self.get_stock_minute_replay_snapshot(
                symbol,
                day,
                1,
                max_bars=240,
            ),
            window_start=window_start.strip(),
            window_end=window_end.strip(),
        )
        if selected_symbols:
            returned = {normalize_symbol(str(item.get("symbol") or "")) for item in result.get("results", [])}
            result["requested_symbols"] = sorted(selected_symbols)
            result["missing_requested_symbols"] = sorted(selected_symbols - returned)
        return result

    def run_historical_trigger_validation(
        self,
        end_day: str,
        lookback_days: int = 5,
        limit: int = 20,
        window_start: str = "09:31",
        window_end: str = "10:00",
    ) -> dict[str, Any]:
        from collections import Counter

        from aegis_alpha.measurements.historical_strategy_replay import post_signal_outcome

        safe_end = end_day.strip()
        safe_lookback = max(1, min(int(lookback_days or 5), 10))
        safe_limit = max(1, min(int(limit or 20), 50))
        days = HSB.recent_trading_days(self.client, safe_end, safe_lookback + 1)
        if len(days) < 2:
            return {
                "end_day": safe_end,
                "data_mode": "unavailable",
                "error": "not_enough_trading_days",
            }

        validations: list[dict[str, Any]] = []
        total_candidates = 0
        total_triggered = 0
        reason_counts: Counter[str] = Counter()
        post_outcomes: list[dict[str, Any]] = []

        for idx in range(1, len(days)):
            as_of = days[idx - 1]
            target = days[idx]
            replay = self.run_historical_strategy_replay(
                as_of,
                target,
                symbols=None,
                limit=safe_limit,
                window_start=window_start,
                window_end=window_end,
            )
            day_results = replay.get("results", [])
            triggered_rows: list[dict[str, Any]] = []
            day_reason_counts: Counter[str] = Counter()
            for item in day_results:
                total_candidates += 1
                diagnostics = item.get("pattern_diagnostics") or {}
                reason = str(diagnostics.get("no_signal_reason") or item.get("error") or "unknown")
                day_reason_counts[reason] += 1
                reason_counts[reason] += 1
                if int(item.get("signal_count") or 0) <= 0:
                    continue

                total_triggered += 1
                symbol = normalize_symbol(str(item.get("symbol") or ""))
                first_signal = (item.get("signals") or [{}])[0]
                triggered_at = str(first_signal.get("triggered_at") or "")
                snapshot = self.get_stock_minute_replay_snapshot(symbol, target, 1, max_bars=240)
                outcome = post_signal_outcome(snapshot, triggered_at=triggered_at)
                row = {
                    "symbol": symbol,
                    "name": item.get("name", ""),
                    "theme": item.get("theme", "unknown"),
                    "triggered_at": triggered_at,
                    "breakout_volume_ratio": first_signal.get("breakout_volume_ratio", 0.0),
                    "pullback_volume_shrink_ratio": first_signal.get("pullback_volume_shrink_ratio", 0.0),
                    "resurge_strength": first_signal.get("resurge_strength", 0.0),
                    "intraday_theme_copump": _intraday_theme_copump(
                        item,
                        day_results,
                        triggered_at=triggered_at,
                    ),
                    "post_signal_outcome": outcome,
                }
                triggered_rows.append(row)
                post_outcomes.append(row)

            validations.append(
                {
                    "as_of_day": as_of,
                    "target_day": target,
                    "candidate_count": len(day_results),
                    "triggered_count": len(triggered_rows),
                    "trigger_rate": round(len(triggered_rows) / len(day_results), 4) if day_results else 0.0,
                    "no_signal_reason_counts": dict(day_reason_counts),
                    "triggered": triggered_rows,
                }
            )

        closed_above = [
            row
            for row in post_outcomes
            if (row.get("post_signal_outcome") or {}).get("ok")
            and (row.get("post_signal_outcome") or {}).get("closed_above_trigger")
        ]
        return {
            "end_day": safe_end,
            "lookback_days": safe_lookback,
            "trading_days": days,
            "window": {
                "start": window_start,
                "end": window_end,
            },
            "limit_per_day": safe_limit,
            "data_mode": "historical_validation",
            "day_count": len(validations),
            "candidate_count": total_candidates,
            "triggered_count": total_triggered,
            "trigger_rate": round(total_triggered / total_candidates, 4) if total_candidates else 0.0,
            "closed_above_trigger_count": len(closed_above),
            "closed_above_trigger_rate": round(len(closed_above) / total_triggered, 4) if total_triggered else 0.0,
            "no_signal_reason_counts": dict(reason_counts),
            "validations": validations,
            "notes": [
                "Validation uses as_of_day watchlist facts, target-day window replay, and post-trigger outcomes for calibration.",
                "Post-trigger outcomes are future labels and must not be used as intraday decision inputs.",
                "Historical Level-2 big-order ratio, CLS popups, and off-platform news alignment are not connected.",
            ],
        }

    def get_intraday_theme_copump(
        self,
        symbol: str,
        as_of_day: str,
        target_day: str,
        trigger_time: str = "",
        window_start: str = "09:31",
        window_end: str = "10:00",
        peer_limit: int = 20,
    ) -> dict[str, Any]:
        safe_symbol = normalize_symbol(symbol)
        safe_as_of = as_of_day.strip()
        safe_target = target_day.strip()
        safe_peer_limit = max(1, min(int(peer_limit or 20), 50))
        universe = [
            item
            for item in self.get_strategy_watchlist(safe_as_of, 100)
            if normalize_symbol(str(item.get("symbol") or ""))
        ]
        target_item = next(
            (item for item in universe if normalize_symbol(str(item.get("symbol") or "")) == safe_symbol),
            None,
        )
        if not target_item:
            return {
                "symbol": safe_symbol,
                "as_of_day": safe_as_of,
                "target_day": safe_target,
                "data_mode": "unavailable",
                "error": "symbol_not_in_strategy_watchlist",
            }

        theme = str(target_item.get("theme") or "unknown")
        same_theme_items = [
            item
            for item in universe
            if str(item.get("theme") or "unknown") == theme
            and normalize_symbol(str(item.get("symbol") or "")) != safe_symbol
        ][:safe_peer_limit]
        replay_symbols = [safe_symbol] + [normalize_symbol(str(item.get("symbol") or "")) for item in same_theme_items]
        replay = self.run_historical_strategy_replay(
            safe_as_of,
            safe_target,
            symbols=replay_symbols,
            limit=100,
            window_start=window_start,
            window_end=window_end,
        )
        results = replay.get("results", [])
        target_result = next(
            (item for item in results if normalize_symbol(str(item.get("symbol") or "")) == safe_symbol),
            None,
        )
        if not target_result:
            return {
                "symbol": safe_symbol,
                "as_of_day": safe_as_of,
                "target_day": safe_target,
                "theme": theme,
                "data_mode": "unavailable",
                "error": "symbol_replay_missing",
            }

        safe_trigger_time = trigger_time.strip() or str(target_result.get("first_triggered_at") or "")
        if not safe_trigger_time:
            diagnostics = target_result.get("pattern_diagnostics") or {}
            safe_trigger_time = str(diagnostics.get("first_cross_time") or diagnostics.get("max_price_time") or "")
        copump = _intraday_theme_copump(target_result, results, triggered_at=safe_trigger_time)
        peer_details = []
        for peer in results:
            peer_symbol = normalize_symbol(str(peer.get("symbol") or ""))
            if peer_symbol == safe_symbol or str(peer.get("theme") or "unknown") != theme:
                continue
            diagnostics = peer.get("pattern_diagnostics") or {}
            peer_details.append(
                {
                    "symbol": peer_symbol,
                    "name": peer.get("name", ""),
                    "first_cross_time": diagnostics.get("first_cross_time", ""),
                    "opening_window_crossed_previous_high": diagnostics.get(
                        "opening_window_crossed_previous_high", False
                    ),
                    "signal_count": peer.get("signal_count", 0),
                    "first_triggered_at": peer.get("first_triggered_at", ""),
                    "no_signal_reason": diagnostics.get("no_signal_reason", ""),
                }
            )

        return {
            "symbol": safe_symbol,
            "name": target_result.get("name", ""),
            "as_of_day": safe_as_of,
            "target_day": safe_target,
            "window": {
                "start": window_start,
                "end": window_end,
            },
            "trigger_time": safe_trigger_time,
            "theme": theme,
            "data_mode": "historical_theme_copump",
            "universe": "same_theme_full_strategy_watchlist_candidates",
            "same_theme_candidate_count": len(same_theme_items),
            "copump": {
                **copump,
                "universe": "same_theme_full_strategy_watchlist_candidates",
            },
            "peer_details": peer_details,
            "notes": [
                "This checks same-theme names in the full strategy watchlist, not only the displayed validation sample.",
                "It is still not full-market sector breadth because direct jvQuant industry-member queries are not reliable yet.",
                "Historical Level-2 big-order ratio and CLS/news alignment are not connected.",
            ],
        }

    def get_intraday_orderflow_confirmation(
        self,
        symbol: str,
        trading_day: str,
        trigger_time: str = "",
        window_start: str = "09:31",
        window_end: str = "10:00",
    ) -> dict[str, Any]:
        safe_symbol = normalize_symbol(symbol)
        safe_day = trading_day.strip()
        payload = self._capital_flow_payload(safe_symbol, safe_day)
        slices = P.parse_daily_capital_flow_payload(payload, symbol=safe_symbol, trading_day=safe_day)
        rows = P._query_rows(payload)
        row = next(
            (
                item
                for item in rows
                if normalize_symbol(P._symbol_from_row(item)) == safe_symbol
            ),
            None,
        )
        turnover_cny = P._parse_cny_amount(P._field_value(row, "成交额")) if row is not None else 0.0
        daily = slices[0] if slices else None

        daily_flow: dict[str, Any] | None = None
        if daily is not None:
            big_ratio = round(daily.big_order_net_inflow_cny / turnover_cny, 4) if turnover_cny else None
            main_ratio = round(daily.main_capital_net_inflow_cny / turnover_cny, 4) if turnover_cny else None
            direction = "neutral"
            if daily.big_order_net_inflow_cny > 0 and daily.main_capital_net_inflow_cny > 0:
                direction = "positive"
            elif daily.big_order_net_inflow_cny < 0 and daily.main_capital_net_inflow_cny < 0:
                direction = "negative"
            daily_flow = {
                "window": daily.window,
                "big_order_net_inflow_cny": daily.big_order_net_inflow_cny,
                "main_capital_net_inflow_cny": daily.main_capital_net_inflow_cny,
                "retail_capital_net_inflow_cny": daily.retail_capital_net_inflow_cny,
                "turnover_cny": turnover_cny,
                "big_order_net_inflow_ratio": big_ratio,
                "main_capital_net_inflow_ratio": main_ratio,
                "direction": direction,
                "source_notes": daily.notes,
            }

        return {
            "symbol": safe_symbol,
            "trading_day": safe_day,
            "window": {"start": window_start, "end": window_end},
            "trigger_time": trigger_time.strip(),
            "data_mode": "historical_orderflow_proxy" if daily_flow else "unavailable",
            "provider": "jvQuant",
            "orderflow_available": False,
            "historical_big_order_buy_ratio_available": False,
            "can_compute_big_order_buy_ratio": False,
            "big_order_buy_ratio": None,
            "active_buy_strength": "unavailable",
            "realtime_orderflow_capability": {
                "lv2_large_trade_proxy_available": True,
                "active_trade_side_available": False,
                "can_compute_big_order_buy_ratio": False,
                "observed_lv2_fields": ["time", "trade_id", "price", "volume"],
                "closest_realtime_metric": "directionless_large_trade_amount_cny",
                "large_trade_threshold_cny": float(
                    os.environ.get("AEGIS_ALPHA_BIG_ORDER_THRESHOLD_CNY", "3000000")
                ),
                "latest_probe_note": (
                    "2026-06-18 lv2 probe for 002281 observed 4-field deal rows "
                    "without active buy/sell side."
                ),
            },
            "daily_capital_flow_available": daily_flow is not None,
            "daily_capital_flow": daily_flow,
            "data_gaps": [
                "historical_minute_level_active_big_order_buy_ratio",
                "historical_orderbook_trade_direction",
            ],
            "notes": [
                "Current jvQuant wiring provides daily semantic capital-flow fields only.",
                "Do not treat daily net inflow as trigger-window big-order buy ratio.",
                "Use this as weak context alongside replay and theme co-pump facts.",
            ],
        }

    def sample_realtime_large_trade_proxy(
        self,
        symbol: str,
        duration_seconds: float = 8.0,
        threshold_cny: float = 3_000_000.0,
        window_start: str = "",
        window_end: str = "",
    ) -> dict[str, Any]:
        safe_symbol = normalize_symbol(symbol)
        safe_duration = max(1.0, min(float(duration_seconds or 8.0), 30.0))
        safe_threshold = max(1.0, float(threshold_cny or 3_000_000.0))
        raw_buffer = SignalWindowBuffer()
        seen_trade_ids: set[str] = set()
        raw_message_count = 0

        def handle_raw(text: str) -> None:
            nonlocal raw_message_count
            raw_message_count += 1
            for record in raw_lv2_large_trade_records(text):
                record_symbol = normalize_symbol(str(record.get("symbol") or ""))
                if record_symbol != safe_symbol:
                    continue
                trade_id = str(record.get("trade_id") or "")
                key = f"{record_symbol}:{trade_id}" if trade_id else json.dumps(record, sort_keys=True)
                if key in seen_trade_ids:
                    continue
                seen_trade_ids.add(key)
                raw_buffer.add_large_trade_proxy(
                    record_symbol,
                    f"{datetime.now(SH_TZ).date().isoformat()}T{record.get('time')}+08:00",
                    float(record.get("price") or 0.0),
                    float(record.get("volume") or 0.0),
                    threshold_cny=safe_threshold,
                )

        client = JvQuantRealtimeClient(
            token=self.token,
            market=os.environ.get("JVQUANT_MARKET", "ab"),
            big_order_threshold_cny=safe_threshold,
            raw_data_handle=handle_raw,
        )
        initial_status = client.subscribe([safe_symbol], ["lv2"])
        time.sleep(safe_duration)
        stats = raw_buffer.large_trade_proxy_stats(
            safe_symbol,
            window_start=window_start.strip(),
            window_end=window_end.strip(),
            trading_day=datetime.now(SH_TZ).date().isoformat(),
        )
        final_status = client.disconnect()
        sample_available = raw_message_count > 0
        data_mode = "realtime_large_trade_proxy" if sample_available else "unavailable"
        return {
            "symbol": safe_symbol,
            "provider": "jvQuant",
            "data_mode": data_mode,
            "duration_seconds": safe_duration,
            "threshold_cny": safe_threshold,
            "window": {"start": window_start.strip(), "end": window_end.strip()},
            "sample_available": sample_available,
            "raw_message_count": raw_message_count,
            "active_trade_side_available": False,
            "can_compute_big_order_buy_ratio": False,
            "proxy_metric": "directionless_large_trade_amount_cny",
            "stats": stats,
            "initial_status": initial_status.model_dump(),
            "final_status": final_status.model_dump(),
            "notes": [
                "This samples realtime lv2 large trades and sums directionless trade amount only.",
                "Observed lv2 shape does not include active buy/sell side, so this is not a buy ratio.",
                "Use this as weak盘口活跃度 confirmation, not as 主动大单买入占比.",
                "If sample_available=false, the provider did not deliver lv2 raw messages during this sample window.",
            ],
        }

    def simulate_historical_orderflow_proxy(
        self,
        symbol: str,
        trading_day: str,
        window_start: str = "09:31",
        window_end: str = "10:00",
        volume_ratio_threshold: float = 1.5,
    ) -> dict[str, Any]:
        safe_symbol = normalize_symbol(symbol)
        safe_day = trading_day.strip()
        snapshot = self.get_stock_minute_replay_snapshot(
            safe_symbol,
            safe_day,
            1,
            max_bars=240,
        )
        return simulate_orderflow_proxy(
            snapshot,
            window_start=window_start.strip(),
            window_end=window_end.strip(),
            volume_ratio_threshold=float(volume_ratio_threshold or 1.5),
        )

    def get_second_board_next_day_outcomes(
        self,
        trading_day: str,
        symbols: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        safe_day = trading_day.strip()
        safe_limit = max(1, min(int(limit or 50), 200))
        calendar = HSB.resolve_adjacent_trading_days(self.client, safe_day)
        if not calendar.get("ok"):
            return {
                "trading_day": safe_day,
                "provider": "jvQuant",
                "data_mode": "unavailable",
                "error": calendar.get("error", "Unable to resolve adjacent trading days."),
                "outcomes": [],
            }

        next_day = str(calendar["next_day"])
        selected = [normalize_symbol(symbol) for symbol in (symbols or []) if normalize_symbol(symbol)]
        if not selected:
            selected = [
                str(item.get("symbol") or "")
                for item in self.get_historical_second_board_candidates(safe_day, safe_limit)
                if str(item.get("symbol") or "")
            ]
        selected = selected[:safe_limit]
        outcomes = [
            HSB.outcome_from_kline(self.client, symbol, trading_day=safe_day, next_day=next_day)
            for symbol in selected
        ]
        return {
            "trading_day": safe_day,
            "next_day": next_day,
            "provider": "jvQuant",
            "data_mode": "historical_provider",
            "symbols": selected,
            "outcomes": outcomes,
            "notes": [
                "Facts-only next-day labels for historical second-board replay.",
                "Aegis Alpha does not assign promotion probability or grade in this tool.",
            ],
        }

    def explain_candidate(self, symbol: str):
        return self._fallback.explain_candidate(symbol)

    def explain_second_board_candidate(self, symbol: str) -> CandidateExplanation:
        candidates = {candidate.symbol: candidate for candidate in self.get_second_board_candidates()}
        normalized = normalize_symbol(symbol)
        candidate = candidates.get(symbol) or candidates.get(normalized)
        if candidate is None:
            return CandidateExplanation(
                symbol=symbol,
                observations=[
                    "Symbol is not in the current jvQuant live-provider second-board candidate pool.",
                    "The current candidate pool only covers yesterday limit-up stocks with today's gain above 5% (non-ST).",
                ],
                risks=[
                    "Symbols outside the previous-day limit-up pool are silently absent from scoring output, so this is not a verdict on the symbol itself.",
                ],
                trigger_conditions=[
                    "Add the symbol to the valid previous-day limit-up and current strength pool before scoring.",
                ],
                avoid_conditions=[
                    "Avoid treating arbitrary symbols as second-board candidates.",
                ],
                data_timestamp=_now(),
                disclaimer="Research and watchlist output only. This is not investment advice or an order instruction.",
            )

        return CandidateExplanation(
            symbol=candidate.symbol,
            observations=[
                f"Current change is {candidate.current_change_pct:.2f}%.",
                f"Auction change is {candidate.auction_change_pct:.2f}%; auction turnover is {candidate.auction_turnover_cny:.0f} CNY.",
                f"Five-minute speed is {candidate.five_min_speed_pct:.2f}%.",
                f"Five-minute speed window is {candidate.five_min_speed_window}; timestamp is {candidate.five_min_speed_timestamp}.",
                f"Minute replay trading day is {candidate.minute_replay_trading_day or 'unknown'}; bar count is {candidate.minute_replay_bar_count}.",
                f"Multi-speed structure is 1m={candidate.one_min_speed_pct:.2f}%, 3m={candidate.three_min_speed_pct:.2f}%, 10m={candidate.ten_min_speed_pct:.2f}%.",
                f"Capital-flow net inflow ratio is {candidate.big_order_net_inflow_ratio:.2f}.",
                f"First limit-up time is {candidate.first_limit_up_time}.",
                f"Final seal time is {candidate.final_seal_time}; break-board count is {candidate.break_board_count}; reseal count is {candidate.reseal_count}.",
                f"Max seal amount is {candidate.max_seal_amount_cny:.0f} CNY; max seal volume is {candidate.max_seal_volume_shares:.0f} shares.",
                f"Seal amount is {candidate.seal_amount_cny:.0f} CNY; seal volume is {candidate.seal_volume_shares:.0f} shares.",
                f"Seal-to-turnover ratio is {candidate.seal_to_turnover_ratio:.2f}.",
                f"Queue position note: {candidate.queue_position_note}",
                f"Concept tags: {', '.join(candidate.concept_tags[:6]) or 'unknown'}.",
                f"Topic tags: {', '.join(candidate.topic_tags[:6]) or 'unknown'}.",
                f"Data quality keys: {', '.join(candidate.data_quality.keys())}.",
                f"Theme is {candidate.theme}; same-theme candidate count is {candidate.same_theme_rising_count}.",
                f"Orderbook quality score is {candidate.orderbook_quality_score:.2f}.",
                (
                    f"流通市值约 {candidate.free_float_market_cap_cny / 1e8:.1f} 亿元，"
                    f"近10日均成交额约 {candidate.avg_turnover_10d_cny / 1e8:.2f} 亿元，"
                    f"5日均线斜率 {candidate.ma5_slope_degrees:.1f}°，"
                    f"T-1量比 {candidate.prev_day_volume_shrink_ratio:.2f}，"
                    f"{'已' if candidate.broke_previous_high else '未'}突破前期高点 {candidate.previous_high_price:.2f}。"
                ),
                f"题材阶段（测量值）：{STAGE_LABELS_CN.get(candidate.theme_lifecycle_stage, candidate.theme_lifecycle_stage)}。",
            ],
            risks=[
                "Candidate pool is live-provider jvQuant; capital-flow fields are semantic-query values, not tick-by-tick order classification.",
                "Minute replay speed is minute-level historical/replay data, not tick-by-tick realtime Level-2.",
                "Auction, concept, topic, break/reseal, and max-seal fields are observed semantic-query values, not official field-level definitions.",
                "Historical three-year limit-up success and next-day premium are placeholders.",
                "True own-order queue position and cancellation rules require broker order/trade callbacks and are not implemented.",
            ],
            trigger_conditions=[
                "Market-wide break-board rate should stay controlled (low) before aggressive board-chasing.",
                "First seal time should be early and seal amount should remain strong versus turnover.",
                "Orderbook quality should remain strong during active trading hours.",
                "Same-theme candidates should expand or the theme leader should remain sealed.",
            ],
            avoid_conditions=[
                "Avoid if break-board rate remains high.",
                "Avoid if seal amount decays quickly or first seal time is late without same-theme support.",
                "Avoid if orderbook quality deteriorates or best ask expands sharply.",
                "Avoid if the candidate falls out of the yesterday-limit-up strength pool.",
            ],
            data_timestamp=_now(),
            disclaimer="Research and watchlist output only. This is not investment advice or an order instruction.",
        )

    def get_stock_realtime_snapshot(self, symbol: str) -> StockRealtimeSnapshot:
        code = normalize_symbol(symbol)
        kline_payload = self.client.kline(code, "stock", "前复权", "day", 2)
        orderbook = self.get_stock_orderbook_snapshot(symbol)

        data = kline_payload.get("data", {}) if isinstance(kline_payload, dict) else {}
        rows = data.get("list", []) if isinstance(data, dict) else []
        fields = data.get("fields", []) if isinstance(data, dict) else []
        latest = rows[0] if rows else []
        field_map = {field: latest[index] for index, field in enumerate(fields) if index < len(latest)}

        bid_volume = sum(level.volume_count for level in orderbook.bid_levels)
        ask_volume = sum(level.volume_count for level in orderbook.ask_levels)
        total_volume = bid_volume + ask_volume
        bid_quality = 50.0 if total_volume == 0 else min(100.0, round(100 * bid_volume / total_volume, 2))
        ask_pressure = 50.0 if total_volume == 0 else min(100.0, round(100 * ask_volume / total_volume, 2))

        return StockRealtimeSnapshot(
            symbol=symbol,
            name=str(data.get("name") or "unknown"),
            timestamp=_now(),
            data_mode="live_provider",
            provider="jvQuant",
            last_price=_float_or_zero(field_map.get("收盘")),
            change_pct=_float_or_zero(field_map.get("涨跌幅")),
            turnover_cny=_float_or_zero(field_map.get("成交额")),
            big_order_net_inflow_cny=0.0,
            bid_quality_score=bid_quality,
            ask_pressure_score=ask_pressure,
            orderbook_notes=[
                "Read-only jvQuant kline and level_queue data.",
                "big_order_net_inflow_cny is not derived yet; Level-2 trade classification is pending.",
                f"orderbook_level_count={orderbook.level_count}",
                f"best_bid_price={orderbook.best_bid_price}",
                f"best_ask_price={orderbook.best_ask_price}",
            ],
        )

    def get_stock_minute_replay_snapshot(
        self,
        symbol: str,
        end_day: str | None = None,
        limit_days: int = 1,
        max_bars: int = 30,
    ) -> MinuteReplaySnapshot:
        code = normalize_symbol(symbol)
        safe_limit = max(1, min(int(limit_days or 1), 30))
        safe_end_day = (end_day or datetime.now(SH_TZ).date().isoformat()).strip()
        payload = self.client.minute(code, safe_end_day, safe_limit)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        fields = data.get("fields", []) if isinstance(data, dict) else []
        days = data.get("list", []) if isinstance(data, dict) else []
        if not isinstance(fields, list):
            fields = []
        if not fields:
            fields = ["时间", "最新价", "均价", "成交量"]

        selected_day = P._latest_minute_day(days)
        trading_day = str(selected_day.get("date") or data.get("end") or safe_end_day)
        previous_close = _float_or_zero(selected_day.get("last_price"))
        raw_bars = selected_day.get("list", [])
        bars = P._minute_bars_from_rows(raw_bars, fields)
        safe_max_bars = max(0, int(max_bars or 0))
        selected_bars = bars[-safe_max_bars:] if safe_max_bars > 0 else bars
        last_bar = bars[-1] if bars else None
        timestamp = (
            P._iso_from_provider_datetime(f"{trading_day} {P._time_with_seconds(last_bar.time)}")
            if last_bar is not None
            else _now()
        )
        speed_pct_by_window, speed_window_by_window = P._minute_speed_windows(trading_day, bars)

        return MinuteReplaySnapshot(
            symbol=symbol,
            name=str(data.get("name") or selected_day.get("name") or "unknown") if isinstance(data, dict) else "unknown",
            timestamp=timestamp or _now(),
            data_mode="minute_replay",
            provider="jvQuant",
            trading_day=trading_day,
            previous_close=previous_close,
            last_price=last_bar.last_price if last_bar else 0.0,
            minute_count=len(bars),
            bars=selected_bars,
            speed_pct_by_window=speed_pct_by_window,
            speed_window_by_window=speed_window_by_window,
            notes=[
                "Read-only jvQuant minute replay data from client.minute(mode=minute).",
                "Speed windows are recalculated by Aegis Alpha from minute bars, not from semantic-query speed fields.",
                "Minute replay is minute-level historical/replay data; it is not tick-by-tick realtime Level-2.",
                f"requested_end_day={safe_end_day}",
                f"requested_limit_days={safe_limit}",
                f"returned_bar_count={len(selected_bars)}",
            ],
        )

    def get_stock_orderbook_snapshot(self, symbol: str) -> StockOrderbookSnapshot:
        code = normalize_symbol(symbol)
        payload = self.client.level_queue(code)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            rows = data.get("list", [])
            name = str(data.get("name") or "unknown")
            level_count = _int_or_zero(data.get("count") or len(rows))
        elif isinstance(data, list):
            rows = data
            name = "unknown"
            level_count = len(rows)
        else:
            rows = []
            name = "unknown"
            level_count = 0

        bid_levels: list[OrderbookQueueLevel] = []
        ask_levels: list[OrderbookQueueLevel] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            level = P._parse_level(row)
            if level.side == "bid":
                bid_levels.append(level)
            elif level.side == "ask":
                ask_levels.append(level)

        bid_levels = sorted(bid_levels, key=lambda item: item.price, reverse=True)[:10]
        ask_levels = sorted(ask_levels, key=lambda item: item.price)[:10]

        return StockOrderbookSnapshot(
            symbol=symbol,
            name=name,
            timestamp=_now(),
            data_mode="live_provider",
            provider="jvQuant",
            level_count=level_count,
            best_bid_price=bid_levels[0].price if bid_levels else None,
            best_ask_price=ask_levels[0].price if ask_levels else None,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            notes=[
                "Read-only jvQuant level_queue summary.",
                "Only top 10 bid and ask levels are returned to keep MCP output compact.",
                "Empty orderbook levels mean the provider returned no queue rows for this symbol at request time.",
                "Do not use this alone for automated trading; queue position and cancellation rules are not implemented.",
            ],
        )

    def _signal_snapshot_from_candidate(self, candidate: SecondBoardCandidate) -> SignalSnapshot:
        timestamp = candidate.five_min_speed_timestamp or candidate.minute_replay_timestamp or _now()
        received_at = _now()
        turnover_cny = self._candidate_note_float(candidate, "turnover_cny")
        big_order_ratio = candidate.big_order_net_inflow_ratio
        return SignalSnapshot(
            symbol=normalize_symbol(candidate.symbol),
            name=candidate.name,
            theme=candidate.theme,
            provider=candidate.provider,
            data_mode=candidate.data_mode,
            price=0.0,
            change_pct=candidate.current_change_pct,
            speed_1m_pct=candidate.one_min_speed_pct,
            speed_3m_pct=candidate.three_min_speed_pct,
            speed_5m_pct=candidate.five_min_speed_pct,
            speed_10m_pct=candidate.ten_min_speed_pct,
            big_order_net_inflow_cny=round(big_order_ratio * turnover_cny, 2) if turnover_cny else 0.0,
            big_order_net_inflow_ratio=big_order_ratio,
            orderbook_quality_score=candidate.orderbook_quality_score,
            seal_amount_cny=candidate.seal_amount_cny,
            data_timestamp=timestamp,
            provider_timestamp=timestamp,
            received_at=received_at,
            freshness_status=freshness_status(timestamp, received_at),
            notes=[
                "Signal snapshot derived from second-board candidate structured fields.",
                "Use data_quality on the candidate for source-level evidence before high-confidence analysis.",
            ],
        )

    def get_dragon_tiger(self, symbol: str, trading_day: str) -> DragonTigerRecord:
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        prefix = _day_query_prefix(day)
        payload = self._query(
            f"{prefix}龙虎榜,股票代码,股票简称,上榜原因,买入金额,卖出金额,净买入额",
            sort_key="净买入额",
        )
        return P.parse_jvquant_dragon_tiger_payload(payload, symbol=symbol, trading_day=day)

    def get_active_seats_today(self, trading_day: str) -> list[dict]:
        # P6/P7 starter: jvQuant 龙虎榜端点尚未对齐契约，返回带 placeholder 信号的
        # 单元素列表，让 Hermes 能区分「真没数据」和「端点未接入」。
        return [
            {
                "hot_money_alias": "",
                "symbol_count": 0,
                "total_net_buy_cny": 0.0,
                "symbols": [],
                "data_mode": "placeholder",
                "error": (
                    "placeholder: jvQuant active-seats endpoint not wired; "
                    "agents should not infer hot-money activity from this entry."
                ),
            }
        ]

    def get_limit_down_pool(self, trading_day: str = "") -> list[ContrarianPoolEntry]:
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        prefix = _day_query_prefix(day)
        payload = self._query(
            f"{prefix}跌停,股票代码,股票简称,涨跌幅,连续跌停天数,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        return P.parse_limit_down_pool_payload(payload, trading_day=day)

    def get_st_pool(self, trading_day: str = "") -> list[ContrarianPoolEntry]:
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        payload = self._query(
            "是否ST=是,股票代码,股票简称,涨跌幅,价格,成交额,行业",
            sort_key="涨跌幅",
        )
        return P.parse_st_pool_payload(payload, trading_day=day)

    def _capital_flow_payload(self, code: str, trading_day: str) -> dict[str, Any]:
        queries = [
            (
                f"{code},股票代码,股票简称,主力净额,超大单净额,"
                "大单净额,中单净额,小单净额,涨跌幅,成交额"
            )
        ]
        if trading_day:
            queries.append(
                (
                    f"{code},股票代码,股票简称,主力净额{trading_day},超大单净额{trading_day},"
                    f"大单净额{trading_day},中单净额{trading_day},小单净额{trading_day},"
                    f"涨跌幅{trading_day},成交额{trading_day}"
                )
            )
        last_payload: dict[str, Any] = {}
        for query in queries:
            last_payload = self._query(query, sort_key="主力净额")
            if P._query_rows(last_payload):
                return last_payload
        return last_payload

    def get_capital_flow_slices(
        self, symbol: str, trading_day: str
    ) -> list[CapitalFlowSlice]:
        code = normalize_symbol(symbol)
        day = trading_day or datetime.now(SH_TZ).date().isoformat()
        payload = self._capital_flow_payload(code, day)
        return P.parse_daily_capital_flow_payload(payload, symbol=code, trading_day=day)

    def get_weekly_position(self, symbol: str) -> WeeklyPosition:
        code = normalize_symbol(symbol)
        week_payload = self.client.kline(code, "stock", "前复权", "week", 12)
        day_payload = self.client.kline(code, "stock", "前复权", "day", 60)
        return P.parse_weekly_position_payload(week_payload, day_payload, symbol=code)

    def find_similar_setups(
        self,
        symbol: str,
        *,
        lookback_days: int = 90,
        similarity_threshold: float = 0.7,
    ) -> list[SimilarSetupResult]:
        # P6 starter: real search runs in MCP layer (combines adapter + store).
        # See mcp/server.py:find_similar_setups.
        return []

    def get_new_stock_candidates(self) -> list[NewStockCandidate]:
        today = datetime.now(SH_TZ).date().isoformat()
        payload = self._query(
            "上市天数小于180,股票代码,股票简称,上市日期,上市天数,流通市值,涨跌幅,行业",
            sort_key="涨跌幅",
        )
        return P.parse_new_stock_candidates_payload(payload, today=today)

    def get_suspended_stocks(self, trading_day: str = "") -> list[SuspendedStock]:
        # P6 starter: jvQuant 停牌字段映射尚未对齐。
        return []

    def _candidate_note_float(self, candidate: SecondBoardCandidate, key: str) -> float:
        prefix = f"{key}="
        for note in candidate.notes:
            if note.startswith(prefix):
                return _float_or_zero(note.removeprefix(prefix))
        return 0.0

    def _query(self, query: str, sort_key: str = "") -> dict[str, Any]:
        return self._query_client.query(self.client, query, sort_key)
