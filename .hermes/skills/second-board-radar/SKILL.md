---
name: second-board-radar
description: Use when Hermes is asked to analyze A-share second-board candidates, one-to-two board setups, board-chasing market conditions, yesterday limit-up pools, theme co-movement, or Aegis Alpha MCP watchlist outputs. Guides Hermes to use Aegis Alpha read-only MCP tools with strict safety boundaries and no deterministic buy/sell instructions. The agent MUST walk all 5 factors per candidate and output a bucketed promotion_likelihood (high/medium/low) plus an agent-assigned grade (A/B/C/REJECT).
license: Proprietary
metadata:
  hermes:
    tags: [Trading, A-share, Second Board, MCP, Risk]
    related_skills: []
    config:
      - key: aegis_alpha.workspace
        description: Absolute path to the Aegis Alpha repository (your local checkout).
        default: ""
        prompt: Aegis Alpha workspace path (absolute path to your local clone)
---

# Second-Board Radar

This skill is for research, watchlist, and review workflows only. Do not issue deterministic buy or sell instructions. Do not call or invent trading execution tools. Do not ask the user for broker credentials.

Market-data provider selection and secrets belong to the Aegis Alpha MCP server configuration, not to this skill. If live data is unavailable, report the unavailable state and continue only with mock or documented stale data.

## Operating Model

Aegis Alpha provides structured data and rule outputs through MCP. Hermes provides reasoning, explanation, memory, and review.

The correct division of responsibility is:

- Aegis Alpha MCP: data access, scoring inputs, timestamps, provider state, and deterministic signal contracts.
- Hermes: interpret the outputs, walk the 5 factors, assign `promotion_likelihood` and `grade`, explain tradeoffs, apply this skill, remember user preferences, and prepare review notes.
- Human user: final decision.
- Future risk engine: required before any paper or real order workflow.

## Required MCP Tools

Prefer Aegis Alpha MCP tools. Hermes may expose them with a server prefix such as `mcp_aegis_alpha_`.

Core tools:

- `get_market_sentiment_gate`
- `get_market_emotion`
- `get_theme_leaders`
- `get_theme_continuity`
- `get_top_themes_today`
- `get_limit_up_ladder`
- `get_auction_analysis`
- `get_seal_timeline`
- `get_second_board_candidates_compact`
- `get_second_board_candidates`
- `get_historical_second_board_candidates`
- `get_historical_first_board_watchlist`
- `get_strategy_watchlist`
- `get_daily_strategy_candidate_pool`
- `run_historical_strategy_replay`
- `run_historical_trigger_validation`
- `get_intraday_theme_copump`
- `get_realtime_symbol_context`
- `get_intraday_theme_context`
- `get_intraday_market_context`
- `record_agent_observation`
- `get_agent_observation`
- `list_agent_observations`
- `notify_agent_observation`
- `get_intraday_orderflow_confirmation`
- `sample_realtime_large_trade_proxy`
- `simulate_historical_orderflow_proxy`
- `get_strategy_decision_packet`
- `get_strategy_trend_outcomes`
- `get_second_board_next_day_outcomes`
- `get_second_board_candidate_data_quality`
- `explain_second_board_candidate`
- `get_stock_realtime_snapshot`
- `get_stock_minute_replay_snapshot`
- `get_recent_market_events`
- `get_signal_snapshot`
- `get_event_scoring_config`
- `get_active_strategy_prior`
- `get_realtime_connection_status`
- `get_runner_status`
- `explain_market_event`
- `get_theme_strength`
- `create_watchlist`
- `update_watchlist_state`
- `close_watchlist`
- `list_active_watchlists`
- `get_pending_alerts`
- `acknowledge_alert`
- `generate_daily_review`
- `generate_weekly_pattern_report`
- `attribute_outcome`
- `get_history_stats`
- `run_backtest`
- `get_recent_backtests`
- `get_dragon_tiger`
- `get_active_seats_today`
- `get_limit_down_pool`
- `get_st_pool`
- `get_capital_flow_slices`
- `backfill_candidates`
- `find_similar_setups`
- `get_new_stock_candidates`
- `get_suspended_stocks`
- `query_minute_bars`
- `simulate_outcome`
- `detect_intraday_buypoint`
- `get_promotion_dossier`
- `get_agent_judgment_scorecard`
- `record_selection_audit`
- `get_selection_audit`
- `get_selection_trigger_validation`
- `get_market_sector_breadth`
- `get_sector_breadth_continuity`
- `get_news_alignment`
- `get_tick_rule_orderflow_proxy`

Useful supporting tools:

- `get_market_snapshot`
- `get_limitup_pool`
- `get_break_board_pool`
- `get_stock_orderbook_snapshot`
- `get_stock_history_limitup_stats`
- `review_candidate_outcome`
- `record_candidate_outcome`

`get_market_sentiment_gate` returns market FACTS — not an action label. Fields include: `limit_up_count`, `break_board_rate`, `hot_theme_count`, `risk_flags`, `positive_signals`, `conclusion`, `yesterday_limitup_today_premium_pct`, `consecutive_boards_alive_rate`, `first_to_second_promotion_rate`, `second_to_third_promotion_rate`, `max_height_today`. Do NOT treat these as gate commands; the AGENT reads the facts and makes its own environmental judgment.

`get_historical_first_board_watchlist(as_of_day)` returns only facts that should be knowable at that historical close, for strict replay questions such as "站在 2026-06-16 收盘，选出明日最值得观察的二板 Top3". Use this before making the Top3 judgment. Do not call target-day second-board tools until after you have clearly separated a post-hoc validation step.

`get_daily_strategy_candidate_pool(as_of_day, limit)` is the preferred first step for the user's final workflow: close-day facts -> agent selects observation TopN -> target-day trigger facts. It returns a facts-only daily pool from first-board and large-turnover trend seeds, with 10-day turnover, T-1/as-of-day shrink, previous-high facts, market-internal theme continuity, source counts, coverage, and explicit data gaps. It does NOT rank by alpha, grade, score, or probability. Provider order is not an agent ranking.

TopN is an audit/explanation layer, not the full live monitoring universe. For live or simulated early-session scanning, the runner should monitor the broader strategy candidate pool exported by `mvp_pilot.py` (`subscription_mode=strategy_scan_pool_with_audit_priority`), with TopN picks only prioritized for review. If a user asks why a valid trigger was missed, check whether the symbol was outside the exported scan pool before changing the trigger rules.

`get_strategy_watchlist(as_of_day)` is the lower-level strict replay entrypoint for the user's broad trend strategy. It returns `{result_count, candidates, data_gaps}` where candidates come from merged first-board and large-turnover trend seeds, then expose 10-day turnover baseline, T-1/as-of-day shrink, previous-high break, candidate source, and partial same-theme breadth. It still does not return program scores, probabilities, or grades. Prefer `get_daily_strategy_candidate_pool` for daily TopN selection unless you need the older compact shape. Do not treat MA5 slope as part of the user's current strategy; that rule is intentionally removed for now.

`get_theme_continuity(theme, as_of_day, lookback_days)` returns market-internal two-week continuity facts for a theme: active days, burst days, total limit-ups, max daily count, recent counts, and a descriptive label such as weak/emerging/persistent/fading. It does NOT check off-platform news or CLS popups, and it is not a buy/sell score.

`run_historical_strategy_replay(as_of_day, target_day, symbols, limit, window_start, window_end)` replays the user's intraday pattern over historical minute bars for the as-of strategy watchlist: opening-window break above previous high, volume-shrunken pullback, and resurge alert. Use `window_start="09:31", window_end="10:00"` when the user asks to stand at 9-10am. It returns research alert facts only. Each result includes `pattern_diagnostics` with crossed-previous-high facts, opening-window cross facts, volume-confirmed breakout status, and `no_signal_reason`; use this to explain why a stock did not trigger. It does not include future outcome labels and does not check historical Level-2 big-order ratio, CLS popups, or off-platform news. 监控窗口默认值已固化在 config/runner.yaml 与 runner DEFAULT_MONITOR_WINDOWS: open_drive 09:30–09:50、late_morning 11:10–11:30（策略第6点）。replay/live 工具未显式传 window_start/window_end 时，应使用这两个窗口。

`run_historical_trigger_validation(end_day, lookback_days, limit, window_start, window_end)` runs a compact historical validation table over recent trading days. For each target day it uses the previous trading day's strategy watchlist, replays the requested intraday window, counts trigger/no-trigger reasons, and appends post-trigger outcome labels. Treat post-trigger labels as calibration only; never use them as as-of decision inputs.

In validation output, `intraday_theme_copump` is the closest current proxy for the user's "同板块一起拉升" condition. It counts same-theme names inside the strategy watchlist sample that had crossed previous high or triggered before this signal time. Use it as a supportive co-pump fact, but say explicitly that it is not full-market realtime sector breadth.

`get_intraday_theme_copump(symbol, as_of_day, target_day, trigger_time, window_start, window_end, peer_limit)` checks same-theme peers from the full strategy watchlist, not just the displayed validation sample. Use it when the user asks whether a specific trigger had sector/theme co-pump. It is still a proxy: direct full-market industry-member breadth is not reliable from current jvQuant semantic queries.

`get_realtime_symbol_context(symbol, lookback_minutes)` returns the latest structured snapshot plus recent stored events for one symbol. Use it for event-triggered agent enrichment before writing an observation. It is facts-only and may report stale/missing/proxy data.

`get_intraday_theme_context(theme_or_symbol, lookback_minutes, peer_limit)` returns the current store's same-theme MarketEvent aggregation. Use it for live/near-live theme co-movement checks in the observer flow. This is a runner/store proxy, not full-market sector breadth; if it returns no events, record the gap rather than inventing sector action.

`get_intraday_market_context(lookback_minutes)` returns runner state, monitored universe size, recent event counts, strongest events, and freshness. Use it as the first call for periodic market-observer jobs and as the market backdrop for alert enrichment.

`record_agent_observation(trading_day, title, summary, source, observation_type, symbol, theme, stance, confidence, evidence_json, counter_evidence_json, data_gaps_json, linked_event_ids_json, linked_alert_ids_json, provider, model)` writes the agent's auditable interpretation. The agent must provide evidence, counter-evidence, data gaps, stance, and confidence; Aegis Alpha computes `notification_grade` deterministically. Never put buy/sell/order language in the observation. Use `get_agent_observation` and `list_agent_observations` to inspect prior observations and avoid repeated conclusions. If the returned `notification_grade` is `urgent` or `important`, call `notify_agent_observation(observation_id)` to let the deterministic WeClaw policy decide whether to push.

`get_intraday_orderflow_confirmation(symbol, trading_day, trigger_time, window_start, window_end)` checks whether order-flow confirmation is available around a replay/live trigger. Current jvQuant historical wiring does NOT provide verified minute-level active big-order buy ratio; the tool exposes that as `historical_big_order_buy_ratio_available=false` and may return only a weak daily capital-flow proxy (`主力净额` / `超大单净额` / `大单净额` divided by daily turnover). It also exposes `realtime_orderflow_capability`: current `lv2` can support a directionless large-trade amount proxy, but `active_trade_side_available=false`, so `can_compute_big_order_buy_ratio=false`. Do not convert this proxy into a trigger-window buy-ratio claim. Use it to name the missing盘口资金 condition clearly and to provide weak context when daily flow is available.

`sample_realtime_large_trade_proxy(symbol, duration_seconds, threshold_cny, window_start, window_end)` opens a short read-only lv2 sample and returns `directionless_large_trade_amount_cny` stats: trade count, total amount, max trade amount, and recent sample trades above the threshold. This is a weak盘口活跃度 proxy only. It cannot classify active buy vs active sell and must never be described as `big_order_buy_ratio`. If `sample_available=false` or `raw_message_count=0`, say the realtime provider did not deliver lv2 messages during the sample; do not interpret zero trade count as a true absence of large trades.

`simulate_historical_orderflow_proxy(symbol, trading_day, window_start, window_end, volume_ratio_threshold)` simulates weak盘口活跃度 from historical minute bars. It flags minutes whose volume is elevated versus the opening baseline. This is useful when the user asks to simulate without market hours, but it is NOT historical Level-2, NOT tick-level large trades, and NOT active buy/sell direction.

`get_strategy_decision_packet(as_of_day, target_day, symbols, limit, window_start, window_end, include_minute_volume_proxy, include_full_theme_copump, include_mvp_proxy_context)` is the preferred end-to-end fact bundle when the user wants a full strategy-style answer. It reduces tool-wandering by bundling as-of strategy facts, target-day replay, packet-local same-theme co-pump, and order-flow availability/proxy facts. It does NOT assign grades or scores; Hermes must still make the Top3/grade/promotion_likelihood judgment. Keep `include_minute_volume_proxy=false` unless the user explicitly wants offline simulation. Keep `include_full_theme_copump=false` by default; turn it on only when the user asks for broader same-theme replay because it is slower. For the user's MVP strategy, set `include_mvp_proxy_context=true` so each result includes exact/proxy/missing tiers, theme-continuity proxy, cninfo news-alignment proxy, and orderflow proxy context in one packet.

`get_strategy_trend_outcomes(as_of_day, target_day, symbols, limit, window_start, window_end)` is the preferred broad-strategy validation outcome after the agent has selected TopN. It is not second-board-only. It returns target-day window facts such as max_gain_pct, drawdown_after_high_pct, window_end_pct, gap_and_fade, morning_followthrough, buy_point_triggered, crossed_previous_high, and trigger_outcome_label. Use this before relying on second-board-specific `sealed_next_day` labels for the user's large-turnover trend strategy.

`post_hoc_attribution_label` appears only in validation output. It is a post-hoc explanation label, not a strategy input, not a buy-point condition, and not a scoring rule. Use it to avoid misattributing outcomes: `post_hoc_trend_breakout_path` means the original breakout/retest/resurge path explains the result; `post_hoc_strong_continuation_without_buy_point` means direction was strong but the main buy-point state machine did not trigger; `post_hoc_second_board_relay_path` means the later outcome came from first-to-second-board relay behavior rather than the user's trend-breakout buy point.

`get_historical_second_board_candidates(trading_day)` and `get_second_board_next_day_outcomes(trading_day, symbols)` return historical FACTS and objective T+1 labels. They do not return program scores, probabilities, or grades. Use them when the user asks whether the approach is usable, asks for replay/backtest-like evidence, asks why a judgment failed, or asks for historical comparison before trusting a live candidate. These are post-hoc tools for a known trading day; they are not valid as the initial candidate pool for an as-of-close replay.

`get_market_sector_breadth(trading_day, theme)` 与 `get_sector_breadth_continuity(theme, as_of_day, lookback_days)` 提供全市场板块宽度事实(同花顺 THS 概念体系,成分股×当日涨停池 join),升级了原先只看候选池的 packet-local 同题材代理。输出带 `concept_system="ths"` 与覆盖度;数据源(AkShare)不可用时返回 `data_mode="unavailable"`,不得脑补。THS 体系与东财体系归类存在差异,这是市场内板块事实,非交易所官方归类。

`get_news_alignment(symbol_or_theme, lookback_days)` 提供合规新闻/公告对齐事实(巨潮资讯公告)。这是合规替代,**明确不是财联社电报原文**(`source_is_caixin=false`);只作题材持续性的弱证据辅助,不作主信号。取数失败时降级,不得伪造消息面。

`get_tick_rule_orderflow_proxy(symbol, window_start, window_end, big_trade_threshold_cny, limit_up_price)` 用 tick-rule 从 lv2 逐笔价格序列推断大单主动买入占比。**这是推断代理,非交易所真值 BS flag**(`is_exchange_truth=false`、`method="tick_rule"`);A股实测精度约70-80%,且封板博弈时系统性虚高——当 `sealing_distortion_warning=true`(价格触及/接近涨停)时该占比不可信,不得当作主动买入真值。它是买点的资金确认弱证据层,买点主链(过前高→回踩缩量→重新上冲)不依赖此值。与 `sample_realtime_large_trade_proxy`(无方向金额)互补。

Hard data boundary: Aegis Alpha currently does not have exchange-verified intraday active buy/sell direction. Treat directionless lv2 large-trade amount, tick-rule inference, and daily semantic capital-flow fields as proxies only. Never describe any of them as true trigger-window active big-order buy ratio.

If these tools are unavailable, first ask Hermes to reload MCP with `/reload-mcp` or inspect the Hermes MCP configuration. Do not fabricate live data.

## Data Availability And Freshness

If Aegis Alpha MCP times out, returns an error, or provides empty data, explicitly state `Data source unavailable` and halt candidate analysis. Do not guess, interpolate, or backfill missing speed, orderbook, big-order, or theme metrics.

Before grading during active trading hours, verify the timestamp of speed, big-order, and orderbook data. Active trading hours are 09:30-11:30 and 13:00-15:00 Asia/Shanghai. If any required realtime field is delayed by more than 3 minutes, cap the maximum grade at `B`, cap `promotion_likelihood` away from `high` (i.e. only `medium` or `low`), warn the user, and do not describe the candidate as high-confidence. If `five_min_speed_window` starts with `provider_exact_window:`, report that exact provider window; if it is `provider_latest_rolling_5m`, explain that the provider did not expose the exact five-minute start/end time.

If `five_min_speed_window` starts with `minute_replay_exact_window:` or `minute_replay_partial_window:`, state that Aegis Alpha recalculated the speed from jvQuant minute replay bars. Minute replay is minute-level replay data, not tick-by-tick realtime Level-2. During active trading hours, use `five_min_speed_timestamp` or `minute_replay_timestamp` to check freshness before grading.

For event-driven reviews, consume `MarketEvent` and `SignalSnapshot` outputs. Do not ask for raw WebSocket messages and do not infer from individual ticks. If an event has stale or unknown `freshness_status`, explain the event as low-confidence context rather than a live trigger.
Use `get_runner_status` when the user asks whether realtime monitoring is active. If the runner state is not `RUNNING`, do not describe Aegis Alpha as actively monitoring the market.

## Standard Workflow

1. Check the market sentiment gate before analyzing individual candidates. Call `get_market_sentiment_gate` and read the FACTS it returns: `limit_up_count`, `break_board_rate`, `hot_theme_count`, `risk_flags`, `positive_signals`, `conclusion`, `yesterday_limitup_today_premium_pct`, `consecutive_boards_alive_rate`, `first_to_second_promotion_rate`, `second_to_third_promotion_rate`, `max_height_today`. If these emotion fields are all zero with a note explaining they are placeholder, treat them as unavailable rather than as a cold-market signal.

2. From the market facts, make your own environmental judgment:
   - High `break_board_rate` (e.g. > 30%) and/or low `limit_up_count` and/or narrow `hot_theme_count` → environment is hostile; explain why, stay defensive, and do not pursue board-chasing candidates.
   - Moderate risk flags with some positive signals → cautious selective stance; highlight what would need to improve.
   - Low `break_board_rate`, healthy `limit_up_count`, broad `hot_theme_count`, positive `first_to_second_promotion_rate` or `consecutive_boards_alive_rate` → environment supports selective board-chasing.

3. If Aegis Alpha data is unavailable, stale beyond the freshness rule, or empty, follow the data availability rule before continuing.

4. If market facts support board-chasing (controlled break-board rate, sufficient limit-up breadth, at least one active hot theme), call `get_theme_leaders` and `get_market_emotion` for board-level context, then fetch second-board candidates with `get_second_board_candidates_compact`. Use `get_limit_up_ladder(symbol)` when you need to confirm a single stock's connect-board height; the candidate output already carries `previous_consecutive_boards`, `previous_height_label`, `theme_role`, and `theme_leader_symbol` so you usually do not need an extra call per candidate.

5. If Aegis Alpha returns recent market events, use them as context for re-scoring candidates, but do not treat event suggestions as order instructions.

6. **Prefer `get_promotion_dossier(symbol)` to fetch all five factors in one call** — it returns `market_emotion / theme_position / float_size / volume_energy / reseal_strength` bundled as facts (no score), so you cannot accidentally skip a factor. Fall back to the individual tools only if the dossier is unavailable.

   For EACH candidate, you MUST walk all 5 factors explicitly. Do not produce only a general summary — 不得只给综合总结而跳过任一因子的逐项说明. The 5 required factors are:

   **Factor 1 — 市场情绪 (market_emotion)**: Derived from the market gate facts: `break_board_rate`, `limit_up_count`, `yesterday_limitup_today_premium_pct`, `first_to_second_promotion_rate`, `consecutive_boards_alive_rate`, `hot_theme_count`. State in one Chinese sentence what the market environment implies for this candidate's odds. Example: "涨停42家，炸板率18%，连板存活率62%，市场情绪较好，对二板进攻有支撑。"

   **Factor 2 — 题材所在位置 (theme_position)**: Read `theme_lifecycle_stage` from the candidate. The lifecycle stages are: `launch`(启动) → `fermenting`(发酵) → `climax`(高潮) → `divergence`(分歧) → `ebb`(退潮). CRITICAL RULE: if `theme_lifecycle_stage` is `divergence` or `ebb`, you MUST downweight the candidate even if recent hotness or limit-up rate looks superficially strong. This is because late-stage themes carry high reverse risk — the electric-power theme failure pattern (高位分歧题材仍强推二板) is a known failure mode that this rule is designed to catch. Use `theme_role`, `theme_leader_symbol`, and `get_top_themes_today` for corroboration.

   具体降权后果（不得例外，即使其他因子都强）：
   - `theme_lifecycle_stage=divergence` → grade 最高只能给 B，promotion_likelihood 最高只能 medium。
   - `theme_lifecycle_stage=ebb` → grade 必须 REJECT，promotion_likelihood 必须 low。
   - `theme_lifecycle_stage=climax`(高潮)：promotion_likelihood 最高只能 medium，除非量能与回封力度同时很强，才可给 high。climax 阶段是分歧前的最后一档，高潮期兑现风险高，必须在因子说明里点出。

   **Factor 3 — 股本大小 (float_size)**: Use `free_float_market_cap_cny`. Large float reduces the probability of sustained sealing; small float with strong theme is favorable. State the float size and its implication in one Chinese sentence.

   **Factor 4 — 量能与资金 (volume_energy)**: Use `avg_turnover_10d_cny` (10-day average turnover baseline), `prev_day_volume_shrink_ratio` (whether T-1/as-of-day shrank vs. the prior baseline), and `broke_previous_high` (whether price cleared the prior swing high). ALSO cover `big_order_net_inflow_ratio` (net big-order inflow as a proportion of turnover — positive means institutional accumulation, negative means distribution) and `orderbook_quality_score` (queue depth and composition quality). An A-grade requires positive big-order inflow AND strong orderbook quality; if either is missing or negative, cap the assessment at B. State the overall volume-and-capital picture in one or two Chinese sentences. Note: the JSON output field key MUST remain `volume_energy` (the validator checks that exact key).

   **Factor 5 — 回封力度 (reseal_strength)**: Use `break_board_count`, `reseal_count`, `max_seal_amount_cny`, `final_seal_time`, and `seal_to_turnover_ratio`. A high `break_board_count` with fast, large `reseal_count` and strong `max_seal_amount_cny` suggests genuine institutional intent to hold the board. A `final_seal_time` near market close with a high `seal_to_turnover_ratio` is a positive sign. State the reseal pattern in one Chinese sentence.

7. After walking all 5 factors, assign `promotion_likelihood` and `grade`:
   - `promotion_likelihood`: MUST be exactly one of `high` / `medium` / `low`. This represents the bucketed probability of this candidate progressing to the third board (三板). The program validates this field — do not use a decimal, percentage, or any other format.
   - `grade`: YOUR judgment as the analyst — exactly one of `A`, `B`, `C`, or `REJECT`. This is not produced by the program; the agent assigns it based on the full picture.
   - 一般对应关系：A→high、B→medium、C/REJECT→low。若 grade 与 promotion_likelihood 出现反差（如 grade=A 但 promotion_likelihood=low），必须在评级原因里明确解释反差原因，不得无声矛盾。

8. Produce a watchlist report. For each candidate, the agent assigns the grade and explains the reason. Always include structured trigger conditions and avoid conditions.

9. Always state both model identity and market-data identity. Keep `llm_provider` / `llm_model` separate from `market_data_mode` / `market_data_provider`.

10. After every candidate, explain the reasoning in natural Chinese, synthesized from the 5 factors. Do not rely on any program-emitted `grade_reason` field — the program no longer produces one. Always derive the reason yourself from the returned metrics.

11. Use the full `get_second_board_candidates` only when the compact output is insufficient. If evidence details are needed, prefer `get_second_board_candidate_data_quality(symbol)` over fetching the full candidate pool again, to avoid tool-output truncation.

12. For multi-hour monitoring, create a watchlist with `create_watchlist(owner=user, label=YYYY-MM-DD label, symbols=A|B|C)` early in the session. Use `update_watchlist_state(watchlist_id, symbol, new_grade, action, note)` whenever a candidate's grade changes during the day. Use `close_watchlist(watchlist_id, note)` at session end to seal the audit trail. List existing watchlists with `list_active_watchlists(owner)`.

13. Read `get_pending_alerts(limit)` whenever the user starts a new chat to surface anything the runner detected while away. After acting on an alert call `acknowledge_alert(alert_id, note)`. The runner persists alerts for `SEAL_ORDER_DECAY`, `BIG_ORDER_INFLOW_SPIKE`, and `THEME_DIVERGENCE` events; do not re-run the same analysis if the alert is still pending.

14. After 15:10, run `generate_daily_review(trading_day=today)` to produce the structured review item used by Phase 3 review-and-correction. For weekly pattern audits use `generate_weekly_pattern_report(start_day, end_day)` (max 14-day window recommended).

15. Use `get_top_themes_today(trading_day, limit)` to surface the leading themes ranked by member count and leader connect-board height, and `get_seal_timeline(symbol)` to inspect intraday seal/break events when a candidate's `theme_role` shows `co_leader` or `follower`.

16. After collecting at least 5 trading days of outcomes, run `attribute_outcome(symbol, trading_day)` for failed candidates to identify recurring failure patterns. Surface the top primary_tag from recent attributions as a Hermes memory candidate after 3+ similar tags accumulate.

17. Use `get_history_stats(symbol)` instead of relying on the placeholder three_year_* fields when available. If `confidence` is `insufficient_sample`, treat the historical signal as unavailable and do not narrate a probability.

18. When the user asks "would tightening rule X improve hit rate?", call `run_backtest(rule_changes_json, start_day, end_day)` and report the sealed_rate delta and advice. Never apply a threshold proposal automatically — they always require the human-confirmation flow defined by `record_correction_action_decision`. Note: the threshold-backtest tool is currently being re-homed (Phase 7) and returns unavailable for certain rule change types; do not rely on it until it is re-enabled.

19. P5 数据维度可选用：
    - `get_dragon_tiger(symbol, trading_day)` 在收盘后查看候选股的龙虎榜结构；如席位含 `hot_money_known` 且 `hot_money_alias` 为白名单游资（章盟主、孙哥等），在评级原因里点出资金主体。
    - `get_active_seats_today(trading_day)` 看当天哪几位游资同时进入多只股，用作板块共振辅助证据。
    - `get_limit_down_pool(trading_day)` / `get_st_pool(trading_day)` 在判断市场情绪时观察反向池规模；如 `MARKET_BOTTOM_REVERSAL` 事件出现，将其当作板块见底的辅助语境，不要由它直接推荐买点。
    - 候选契约里 `limitup_driver_type` 与 `intraday_pattern` 在 evidence 里给一句中文备注；`policy` / `earnings` 驱动通常比 `theme` 更稳，`one_word_board` / `platform_breakout` 比 `messy_board` / `false_breakout` 风险更低。
    - `get_capital_flow_slices(symbol, trading_day)` 在复盘失败案例时使用：`tail_30m` 主力净流出说明尾盘机构离场。

20. P4 盘中买点离线回放（Phase 4，按需使用）：
    - `detect_intraday_buypoint(symbol, end_day, previous_high)` 返回盘中买点形态的离线回放告警（过前高→回踩缩量→重新上冲）。这是研究告警，不是下单指令。当前为离线历史回放（实盘盘中监控是后续阶段）；阈值用默认值，策略 prior（后续阶段）会让均量 > 50 亿等阈值可切换。把返回的 `signals` 当作观察证据，结合五因子判断，不要据此直接下单。若未传 `previous_high`，工具会用开盘前 3 根 bar 的最高 `last_price` 作为保守替代；传入明确前高更准确。

21. P6 进阶能力（按需使用）：
    - `find_similar_setups(symbol, lookback_days, similarity_threshold)` 在复盘候选时找相似历史样本；当返回的 `similarity ≥ 0.85`，可作为「这个形态历史上确实经常打成功」的弱证据，但不要替代当下行情判断。注意：历史快照的 agent/human 评级（若有记录）为弱参考，若为 None 则完全忽略。
    - `get_new_stock_candidates()` 返回的 `tier_aged_out` 不应再按次新处理；`tier_a_smallcap_recent` 才是典型的次新打板候选。
    - `get_suspended_stocks(trading_day)` 在每次拉候选前检查；候选若出现在停牌列表中应直接 REJECT 并提示数据脏。
    - `query_minute_bars(symbol, start_day, end_day)` 仅在 history-store extras 安装后可用；返回 `data_mode=unavailable` 时直接告诉用户分钟级历史层未启用。
    - `simulate_outcome(symbol, trading_day, hypothesis_json)` 在用户问「如果当时封单是 X 亿，评级会变吗？」时调用；返回 `payload_diff` 是结构化对比，不是确定性结论。P8 起它会用真 `candidate_grade()` 重算：你可以传任意 `{"action": "avoid"}` / `{"orderbook_quality_score": 80.0}` 等覆盖来观察 `hypothetical_grade` 变化。
    - 候选契约里的 `weekly_health_score` ≥ 70 表示周线位置健康，可加分；< 30 应在评级原因里点出周线劣势。
    - 板块事件 `THEME_LEADER_BREAK_BOARD`（高度龙头炸板）与 `SECTOR_ROTATION`（板块轮动）：当 `get_recent_market_events` 返回这两类事件时，把它们当作板块级风险/机会语境而不是单股触发——前者意味着同题材 follower 应整体降级，后者意味着可以把注意力从 weakening_theme 转向 strengthening_theme 的 followers。P8 起，板块事件 `THEME_LEADER_BREAK_BOARD` / `SECTOR_ROTATION` / `MARKET_BOTTOM_REVERSAL` 会触发 runner macOS 告警；通过 `get_pending_alerts(limit)` 拉到的告警里会带这 3 类。
    - 停牌过滤已在 P7 自动接入候选拉取链路 — 候选列表中不会再出现停牌股；如人工手动评估某只票，仍可调 `get_suspended_stocks(trading_day)` 复核。
    - `simulate_outcome` 的 `hypothetical_grade` 现在来自完整 candidate_grade 重算，不再受限于 P7 的 `seal_amount_cny` / `five_min_speed_pct` 两字段启发。

22. 历史 replay / 可用性验证（按需使用）：
    - 严格 as-of replay：当用户说「站在 YYYY-MM-DD 收盘，选明日最值得观察的二板 Top3」或「生成明日观察池」时，优先调用 `get_daily_strategy_candidate_pool(as_of_day=YYYY-MM-DD)`。如果该工具不可用，才退回 `get_strategy_watchlist(as_of_day=YYYY-MM-DD)`；再不可用才退回 `get_historical_first_board_watchlist(as_of_day=YYYY-MM-DD)`，并明确说明策略字段不足。在给出 Top3 之前，不要调用 `get_historical_second_board_candidates(target_day)`，不要读取 `target_day` 的涨跌幅、封板结果或 T+1 outcome。
    - Top3/TopN 是 agent 重点审计与解释输出，不是策略声明“只监控这些票”。实盘 runner 的订阅/扫描池应来自更大的 daily strategy candidate pool；不要把 MVP 的 Top3 审计边界误写成策略边界。
    - 严格 as-of replay 的初始 Top3 阶段只使用 `get_daily_strategy_candidate_pool` / `get_strategy_watchlist` / `get_historical_first_board_watchlist` 返回的事实池。跳过标准实时工作流里的 `get_market_sentiment_gate`、`get_active_strategy_prior`、`get_theme_leaders`、`get_promotion_dossier`、实时盘口/分钟回放等工具，除非用户在 Top3 之后明确要求补充验证。
    - 对每只入选标的，必须逐条说明：近10日均成交额是否大于50亿、T-1/as-of-day是否缩量、T日是否突破前高、板块持续性数据是否充分、候选来自 `first_board_watchlist` 还是 `large_turnover_trend_seed`。`theme_continuity.continuity_label` 只能作为市场内板块持续性事实；盘外新闻/财联社若未接入，必须列为缺口，不得脑补。当前策略暂不使用“5日均线斜率30°到60°”。
    - 不要机械地把“缩量”排在“放量”之前。T-1 缩量是锁仓/抛压证据，但强题材、超大成交额、突破结构、机构参与或板块内核心地位可以构成保留放量票的理由；反过来，缩量但方向弱、题材衰退、距前高过远，也不能因为缩量而入选。若保留放量票，必须说明放量是突破动能/机构参与/主线共振，还是散筹兑现风险。
    - 当用户问「这个方向能不能验证」「为什么 agent 这么判断」「今天二板环境与历史相似吗」时，先调用 `get_historical_second_board_candidates(trading_day)` 取历史候选事实，再调用 `get_second_board_next_day_outcomes(trading_day, symbols)` 取 T+1 结果。
    - 你的任务是用历史事实校准自己的判断，不要要求程序提供评分权重。程序只给数据；agent 给 `promotion_likelihood` 和 `grade`。
    - 对比至少三个朴素基准：封单额优先、封成比/封成比近似优先、首次封板时间优先。先列出这三个基准 Top3，再给你的 agent Top3。
    - 反机械排序规则：如果你的 Top3 与任一朴素基准 Top3 完全相同，必须重新评估。若重新评估后仍保持一致，必须明说「当前判断主要等同于某某基准排序，尚未体现额外 alpha」，并把整体置信度降到 exploratory/low-confidence。
    - 入选解释必须是相对解释：每只入选标的至少说明一个「它胜过某只高封单额/高封成比/更早封板但落选标的」的理由。如果说不出相对优势，不要把它列为 A。
    - 封成比/封单额只能作为封板质量证据，不得作为唯一或主排序理由。在 strict as-of replay 的 Top3 生成阶段，不要引用同一 replay 的未来失败/成功案例；这些只能在明确标注为 post-hoc validation 的段落里使用。
    - 输出历史 replay 结论时，区分「胜过候选池平均」和「胜过简单基准」。前者只能说明有基础筛选价值；后者才说明 agent 判断可能有额外价值。
    - 不要从单日 replay 推断稳定胜率。若样本少于 10 个交易日，结论必须标为 exploratory。
    - 盘中历史 replay：当用户要求“用已发生数据模拟当时事实”或“模拟买点触发”时，优先调用 `get_strategy_decision_packet(as_of_day, target_day, symbols, limit, window_start, window_end, include_minute_volume_proxy=false, include_full_theme_copump=false, include_mvp_proxy_context=true)`，除非用户只要求单只票的原始 replay。输出必须分清：收盘观察池事实、target_day 分钟级触发事实、packet-local 同题材共振事实、缺失数据（历史大单买入占比/财联社/盘外新闻）、以及是否出现 research alert。不要把 research alert 写成买入指令。
    - 复盘时若某票 `triggered=false` 但 `trend_outcome_label=morning_followthrough`、`trigger_outcome_label=strong_continuation_without_buy_point`、`continuation_pattern=gap_up_followthrough|instant_limit_or_strong_hold|strong_trend_continuation` 或窗口内 `max_gain_pct` 很强，必须把它标成“主买点状态机未覆盖的强势延续/跳空延续候选”，而不是简单判为失败。当前买点状态机仍以过前高→回踩缩量→重新上冲为主链；跳空高开后持续走强、秒板强封、未过前高但窗口持续抬升，是待验证的辅助形态。
    - 历史样本验证：当用户要求“验证可用性”“跑一段历史样本”“看看触发器命中率/误报”时，调用 `run_historical_trigger_validation(end_day, lookback_days, limit, window_start, window_end)`。必须区分 replay 触发事实和 post-trigger outcome；后者只能用于校准。

23. Agent 市场观察 / 盘中 observer：
    - 每轮先调用 `get_pending_alerts(limit=50)`。`BUYPOINT_ALERT` 必须优先调查；`CANDIDATE_INVESTIGATION` 表示某只股票已达到高置信强势候选阈值，即使没有完整买点，也必须逐只调用 `get_realtime_symbol_context` 调查，不能只依赖 `strongest_events` TopN。
    - 当 Hermes webhook 收到 runner alert，或 cron 触发周期性 observer 时，先调用 `get_intraday_market_context(lookback_minutes=30)`。若有具体 symbol，再调用 `get_realtime_symbol_context(symbol, lookback_minutes=30)`；若能识别 theme，再调用 `get_intraday_theme_context(theme_or_symbol, lookback_minutes=30)`。
    - 你可以输出零条观察。只有当事实显示 strategy-adjacent 信息时，才调用 `record_agent_observation`。常见类型包括 `buy_point_quality`、`theme_rotation`、`market_regime_shift`、`strong_continuation_without_buy_point`、`watchlist_observation`、`data_gap`、`noise_or_rejected_trigger`。
    - webhook alert 增强必须使用 `source="trigger_enrichment"` 并填写 `linked_alert_ids_json`；cron 周期观察必须使用 `source="periodic_market_scan"`。不要自行创造 source 枚举值。
    - `stance` 只允许表达研究观察态度：`actionable_watch` / `monitor_only` / `insufficient_data` / `reject`。不要把 `actionable_watch` 写成买入建议；它只表示值得中断提醒的人类观察项。
    - `evidence_json`、`counter_evidence_json`、`data_gaps_json` 必须是 JSON 数组字符串。至少写一条证据和一条数据缺口；若你认为无缺口，也写明 `["未发现新增数据缺口"]`，避免不可审计的空观察。
    - 不要自填或口头承诺 notification grade。`record_agent_observation` 返回的 `notification_grade` 才是 WeClaw 推送门控依据。若 grade 为 `urgent` 或 `important`，调用 `notify_agent_observation`；若返回 `posted=false`，只汇报原因，不重试刷屏。
    - 如果 runner 非 `RUNNING`、facts stale、或事件不足，只能记录 `data_gap` / `insufficient_data` 或直接输出无观察；不得强行给出市场方向判断。

## Strategy Prior

Call `get_active_strategy_prior` once per session to load the active strategy prior (currently `client_10pt` — 客户口述的二板买点策略). The prior is **GUIDANCE, not a filter**. It carries soft ideal ranges and qualitative notes the client cares about. The program never accepts or rejects a candidate based on the prior; YOU weigh each candidate's measured facts against the prior and decide.

How to use it:

- **Soft ranges (`thresholds`)**: each threshold has `ideal_low`/`ideal_high`/`unit`/`rationale`. Compare the candidate's measured fact to the ideal range and report the comparison in your factor write-up. Current active range for this strategy: 近10日均成交额 ideal ≥ 50亿 (`avg_turnover_10d`). Ignore MA5 slope for now even if an older prior or candidate object contains it.
- **Override-with-reasoning (MANDATORY)**: a candidate OUTSIDE a prior's ideal range is NOT auto-rejected. If facts justify it, keep the candidate and state the override explicitly. Likewise, being INSIDE every active range does NOT guarantee a high grade — the 5-factor judgment still governs. Never treat a prior range as a pass/fail gate.
- **Guidance notes (`guidance_notes`)**: qualitative items to weave into your reasoning — 板块两周持续性（可能需你做盘外信息确认）、T-1 缩量、T 日带量过前高、回踩缩量后重新上冲=买入预警点、同板块共振加分、重点监控时段 9:30–9:50 与 11:10–11:30。
- **板块两周持续性** is an agent task GUIDED by the prior: when the prior flags it, you should seek out-of-platform corroboration (盘外抓取) of whether the sector has shown repeated multi-day strength over the past two weeks, and say so — do not fabricate it if you cannot confirm.
- **财联社消息**: `caixin_alignment` is a placeholder this cycle (本期暂不接入财联社消息源). Do not claim a 财联社 alignment signal exists; treat it as not-yet-available.

If `get_active_strategy_prior` returns `data_mode: unavailable`, proceed with the standard 5-factor workflow without the prior and note that no active prior was loaded.

## Candidate Interpretation Rules

Use these as agent grading heuristics unless the user's memory or the Aegis Alpha output provides specific guidance:

- `A`: market facts indicate a supportive environment (low break-board rate, sufficient limit-up breadth, positive promotion rates); same-theme co-movement is strong; `theme_lifecycle_stage` is `launch` or `fermenting` (climax 见下方天花板规则); orderbook quality is strong; big-order inflow is positive; volume energy shows expansion; reseal strength is convincing; historical stats are favorable.
- `B`: watch closely, but at least one important factor is not ideal — including but not limited to volume shrinkage, mixed market breadth, or climax-stage theme risk. `theme_lifecycle_stage=divergence` 总是把 grade 限制在 B（即使其他因子都强）。
- `C`: observation only; do not frame it as actionable.
- `REJECT`: not in yesterday's valid limit-up pool; market-wide break-board rate is high and breadth is collapsing (agent's judgment from facts); theme leader broke board; `theme_lifecycle_stage=ebb`（退潮阶段一律 REJECT，不论封单多大）; or data quality is insufficient.

If speed, big-order, or orderbook timestamps are delayed by more than 3 minutes during active trading hours, maximum grade is `B` and `promotion_likelihood` must not be `high`.

For second-board analysis, prefer fewer candidates with better explanation over broad lists.

## Output Format

Use this structure for user-facing answers. The `factor_analysis` block and `promotion_likelihood` are MANDATORY for every candidate. Omitting any factor or replacing the factor walk with a general summary is a violation of this skill's contract.

```text
市场环境(事实): 涨停N家 炸板率X% 连板存活率Y% 热门题材M个 → 判断: 适合/谨慎/不宜进攻

候选:
1. 代码 名称
   晋级三板概率(分档): high / medium / low
   综合评级(agent): A / B / C / REJECT
   因子分析:
   - 市场情绪: <一句中文说明，基于涨停数/炸板率/溢价率等市场事实>
   - 题材所在位置: <说明 theme_lifecycle_stage 及其含义；若为 divergence/ebb 须明确点出降权原因>
   - 股本大小: <说明 free_float_market_cap_cny 及对封板持续性的影响>
   - 量能: <说明 avg_turnover_10d_cny / prev_day_volume_shrink_ratio / broke_previous_high / big_order_net_inflow_ratio / orderbook_quality_score>
   - 回封力度: <说明 break_board_count / reseal_count / max_seal_amount_cny / final_seal_time / seal_to_turnover_ratio>
   评级原因: <综合五个维度的自然语言总结，说明主要加分项和主要扣分项>
   竞价数据: auction_change_pct / auction_turnover_cny / auction_turnover_rate
   涨速数据: five_min_speed_pct / five_min_speed_window / five_min_speed_timestamp / data_quality.five_min_speed
   分时回放: minute_replay_trading_day / minute_replay_bar_count / minute_replay_timestamp
   题材数据: concept_tags / topic_tags
   证据层级: official_doc / observed_probe / internal_inference 中实际出现的 authority
   封板数据: 首次封板时间 / 最终封板时间 / 封单额 / 最大封单额 / 炸板次数 / 回封次数 / 封成比 / 排队位置说明
   观察:
   风险:
   触发条件:
   - 价格: ...
   - 量能/大单: ...
   - 板块动作: ...
   禁止条件:
   - 价格: ...
   - 量能/盘口: ...
   - 板块动作: ...

数据状态:
- llm_provider:
- llm_model:
- market_data_mode:
- market_data_provider:
非投资建议:
```

Examples of concrete avoid conditions include: high open below 3%, auction front-running, same-theme leader breaking the intraday VWAP, orderbook quality falling below threshold, big-order net inflow turning negative, or break-board rate expanding quickly.

Do not write "buy", "must buy", "sell", "full position", or "guaranteed". Use "观察", "候选", "触发条件", "禁止条件", and "风险".

## Safety Rules

- Never request or expose broker credentials, trading passwords, or real order tokens.
- Never propose real automated trading from this skill.
- Never treat mock data as real market data.
- Never ignore the market sentiment gate facts.
- Never recommend board-chasing when market-wide break-board rate is high or limit-up breadth is collapsing (agent's judgment from facts).
- Never analyze arbitrary symbols as second-board candidates unless they are in the valid previous-day limit-up pool or the user explicitly asks for a hypothetical review.
- If data is stale, missing, or inconsistent, downgrade confidence and say so.
- A strategy prior is guidance only: never apply a prior's range as a hard pass/fail filter, and never let a prior override measured facts — facts win, and any deviation from the prior must be stated with reasoning.

## Review And Memory

When the user corrects a judgment, summarize the reusable lesson and ask whether to remember it.
Use `record_candidate_outcome` for explicit review facts or user corrections; do not store rumors, credentials, or unverified tips as outcomes.

Good memory candidates:

- The user prefers second-board setups over first-board setups.
- The user dislikes fast boards without theme co-movement.
- The user wants less activity when break-board rate is high.
- The user cares about next-day open and third-day premium after sealed second boards.
- Late-stage themes (`divergence`/`ebb`) should be downweighted even if short-term hotness looks strong (电力题材高位失败教训).

Do not save raw stock tips, credentials, or one-off market rumors as memory.

## Judgment Self-Check (Scorecard)

Use `get_agent_judgment_scorecard(start_day, end_day)` to review how well your PAST calls matched reality over a date window. It returns objective calibration metrics — `brier_score` (lower = better-calibrated promotion_likelihood), `likelihood_calibration` (per high/medium/low bucket: predicted vs realized seal rate), and `grade_hit_rate` (realized seal rate per grade) — computed from your stored reviews vs recorded next-day outcomes.

This is a self-calibration mirror, not a program grade and not an order. Read it to spot systematic bias — e.g. if your `high` bucket's realized seal rate is far below 0.8, you are over-confident and should tighten what earns a `high`. When the window has no scored samples, `sample_size` is 0 and `brier_score` is null; do not over-interpret a tiny sample.

## 闭环验证（二期A，#3+#4）

闭环验证(二期A,#3+#4):收盘 agent 从候选池选完 TopN 后,调 `record_selection_audit(as_of_day, picks_json, rejected_json, candidate_pool_size)` 持久化选股决策——picks_json 每只含 symbol/rank/relative_reason(相对理由:为什么它胜过某只更高封单额/封成比的落选股)/caveats(缺失数据,如盘外新闻未确认);rejected_json 记录落选 near-miss 及 why_rejected/beat_by。程序自动用同日候选池/历史二板事实算三朴素基准 TopN(封单额/封成比/首封时间)并标记 `equals_baseline`:若为 true,说明你的 TopN 等同某朴素基准、未体现额外 alpha,返回里会带 anti_mechanical_warning,你必须重评或明确说明。若返回 `audit_quality=incomplete` 或 `audit_quality_warnings`,说明 rank/相对理由/落选解释不足,必须补全审计后重新调用 `record_selection_audit`,不要直接进入次日验证。`confidence_label` 在累计选股记录 <10 个交易日时强制 exploratory。次日(或任意目标日)调 `get_selection_trigger_validation(as_of_day, target_day, window_start, window_end)` 对照闭环:逐只 pick 分开给出 09:31-10:00 是否过前高(crossed_previous_high/cross_time)、买点状态机是否真正触发(triggered/trigger_time/no_signal_reason)、宽策略趋势 outcome(trend_outcome_label/trigger_outcome_label/max_gain_pct/window_end_pct/drawdown_after_high_pct)、事后归因(post_hoc_attribution_label)、次日触板/封板/开盘涨幅,汇总 trigger_rate;注意 trigger_rate 只统计真正买点触发,不把单纯过前高算作触发。`post_hoc_attribution_label` 只能用于复盘解释,不得反向写成策略筛选条件或买点。盘中/次日事实任一不可用时该字段 data_mode 标 unavailable,不脑补。runner 在交易日 10:00 后自动对最近一条昨收审计跑一次验证并发 SELECTION_VALIDATION 告警(advisory,只读审计+写告警,绝不下单),通过 get_pending_alerts 拉取。样本不足时所有结论标 exploratory,不得据单日/小样本下稳定胜率结论。

## Scheduled Use

For Hermes cron jobs, prompts must be self-contained. A useful schedule is:

- 09:20: prepare yesterday-limit-up pool and market precheck.
- 09:30-10:30: monitor the second-board candidate list.
- 15:10: review whether candidates touched limit-up, sealed, broke board, and produced expected follow-through.

Cron outputs should be concise and should not claim real-time monitoring unless the MCP data source is live.
