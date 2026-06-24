# Aegis Alpha MVP Pilot Runbook

This runbook wires the current MVP through Hermes-native automation. It does not place orders and does not depend on WeChat.

## Architecture

```text
Hermes gateway
  -> Hermes cron: daily agent runs
  -> Hermes webhook: event-triggered agent runs

Aegis runner
  -> jvQuant WebSocket subscriptions
  -> SignalSnapshot / MarketEvent / AgentAlert
  -> optional POST to Hermes webhook
```

Hermes owns scheduling and event-triggered agent runs. Aegis Alpha owns market-data ingestion and structured facts. The runner does not call an LLM.

## Status And Plan

Completed in this MVP slice:

- Hermes cron runs the daily prepare and morning report jobs.
- Hermes cron runs periodic intraday agent market-observer jobs.
- Hermes webhook accepts signed Aegis runner alert events.
- Aegis runner can stay alive under launchd outside trading hours and report `WAITING`.
- Runner alerts can optionally POST to Hermes webhook without affecting runner liveness.
- Hermes can record `AgentObservation` rows and explicitly pass urgent/important observations through the deterministic WeClaw notification gate.
- MVP proxy/context fields are available for post-hoc explanation while staying outside strategy scoring.

Next work:

- Validate jvQuant WebSocket subscription during a live trading session.
- Confirm real `BUYPOINT_ALERT` and `SELECTION_VALIDATION` events flow from runner to Hermes webhook.
- `BUYPOINT_ALERT` is agent input, not a direct WeClaw message. WeClaw receives
  only agent observations graded `urgent` or `important`, plus low-frequency
  `SELECTION_VALIDATION` audit messages.
- Provider failures are a separate operational channel. Every five minutes, a
  no-agent health job checks Hermes cron failures, runner status, and recorded
  jvQuant query/WebSocket errors. It also incrementally reads Hermes Gateway
  provider errors, so a primary-model balance failure is reported even when a
  fallback model completes the task successfully. Balance, quota, entitlement,
  and rate-limit failures bypass market-signal grading and notify WeClaw
  directly, with a six-hour duplicate cooldown.
- Confirm useful `AgentObservation` quality over several live sessions and tune only the skill wording if the agent is too noisy or too timid.
- Add provider-paid data only if the local MVP shows useful signal quality.

## Install Hermes Automation

Install the thin context scripts only:

```bash
scripts/install_hermes_mvp_automation.sh
```

Install/start Hermes gateway and create cron/webhook jobs:

```bash
scripts/install_hermes_mvp_automation.sh --install-gateway --create-jobs
```

Check Hermes automation:

```bash
hermes gateway status
hermes cron list
hermes webhook list
```

## Model Modes

Default automation mode is DeepSeek direct:

```bash
scripts/switch_hermes_model.sh deepseek
```

DeepSeek mode keeps OpenRouter GPT-5.4 as the billing/rate-limit fallback.
Use OpenRouter as the primary model when a harder review needs it:

```bash
scripts/switch_hermes_model.sh openrouter
```

Do not mix `model.provider=deepseek` with `model.base_url=https://openrouter.ai/api/v1`; that sends DeepSeek requests through the wrong endpoint and fails authentication. The switch script updates provider, model, base URL, fallback chain, API mode, and restarts Hermes gateway.

## Daily Prepare Job

Hermes cron runs the prepare job after close:

```text
10 16 * * 1-5  aegis-alpha-daily-prepare
20 16 * * 1-5  aegis-alpha-export-subscription
```

The cron job injects dynamic context from:

```bash
~/.hermes/scripts/aegis_alpha_mvp_prepare_context.sh
```

Hermes then uses the `second-board-radar` skill and Aegis Alpha MCP tools to:

- call `get_daily_strategy_candidate_pool`
- choose Top3 as the agent audit/explanation layer
- call `record_selection_audit`
- output audit summary
- export runner subscription symbols from the broader strategy scan pool, with Top3 only prioritized for review

The context script does not choose stocks and does not call an agent.

Important boundary: Top3 is not the full live monitoring universe. The runner
should subscribe to the wider scan pool exported by `scripts/mvp_pilot.py`
(`subscription_mode=strategy_scan_pool_with_audit_priority`) so valid morning
triggers outside the audit Top3 can still alert.

`aegis-alpha-export-subscription` is a no-agent Hermes cron job. It runs:

```bash
scripts/mvp_pilot.py export-subscription --scan-limit 50 --allow-current-proxy-scan --update-env-local
```

Then it restarts the launchd runner so the next trading session reads the new
`JVQUANT_SUBSCRIBE_SYMBOLS`. If the strict daily strategy pool is unavailable,
the export can use a clearly labelled current-provider proxy pool
(`data_mode=proxy_current_provider`) rather than silently falling back to Top3.

## Runner

The runner is still required because jvQuant currently exposes realtime data through a client-held WebSocket, not provider-initiated HTTP webhooks.

Start/check the runner:

```bash
scripts/install_launchd_runner.sh
scripts/check_runner_status.sh
```

Runner stdout/stderr logs are installed under:

```bash
~/Library/Logs/AegisAlpha/runner.out.log
~/Library/Logs/AegisAlpha/runner.err.log
```

When `AEGIS_ALPHA_HERMES_WEBHOOK_ENABLED=true`, runner alerts are posted to the Hermes webhook configured by:

```bash
HERMES_AEGIS_WEBHOOK_URL=http://127.0.0.1:8644/webhooks/aegis-alpha-alerts
HERMES_AEGIS_WEBHOOK_SECRET=...
```

## Morning Report Job

Hermes cron runs the morning report after the opening window:

```text
30 10 * * 1-5  aegis-alpha-morning-report
```

The cron job injects context from:

```bash
~/.hermes/scripts/aegis_alpha_mvp_report_context.sh
```

Hermes should read the latest audit, runner status, pending alerts, and `get_selection_trigger_validation`, then explain:

- whether the main strategy triggered
- whether the trigger came from the Top3 audit set or the wider scan pool
- whether a move was only `strong_continuation_without_buy_point`
- whether the later move was a second-board relay path
- which facts are exact, proxy, or missing

## Intraday Agent Observer Jobs

Hermes cron runs periodic observer jobs during the trading session:

```text
42 9  * * 1-5  aegis-alpha-market-observer-0942
15 10 * * 1-5  aegis-alpha-market-observer-1015
18 11 * * 1-5  aegis-alpha-market-observer-1118
35 13 * * 1-5  aegis-alpha-market-observer-1335
20 14 * * 1-5  aegis-alpha-market-observer-1420
55 14 * * 1-5  aegis-alpha-market-observer-1455
```

The cron job injects context from:

```bash
~/.hermes/scripts/aegis_alpha_market_observer_context.sh
```

Observer jobs run with the `aegisobserver` Hermes profile. The installer copies
the context scripts into that profile and applies a shorter agent budget. This
keeps observer prompts lighter while preserving the same MCP facts and
deterministic notification gate.

Hermes should then:

- call `get_intraday_market_context`
- investigate notable symbols with `get_realtime_symbol_context`
- investigate same-theme movement with `get_intraday_theme_context`
- write useful observations with `record_agent_observation`
- call `notify_agent_observation` only when the returned grade is `urgent` or `important`

It may output zero observations. A forced observation is worse than no observation.

## Agent Observations And WeClaw

Agent observations are stored in SQLite as `agent_observations`. Query them through Hermes MCP:

```text
list_agent_observations(trading_day=YYYY-MM-DD)
get_agent_observation(observation_id=...)
```

WeClaw pushes remain opt-in through `config/runner.yaml` and env:

```bash
AEGIS_ALPHA_WECLAW_ENABLED=true
AEGIS_ALPHA_WECLAW_API_URL=http://127.0.0.1:18011/api/send
AEGIS_ALPHA_WECLAW_TO=...
```

`notify_agent_observation` computes the notification grade deterministically and returns whether a push was attempted/succeeded. Hermes must not invent a push result.

## Manual Debug Commands

Generate context without running Hermes:

```bash
PYTHONPATH=src .venv/bin/python scripts/mvp_pilot.py cron-context prepare
PYTHONPATH=src .venv/bin/python scripts/mvp_pilot.py cron-context report
~/.hermes/scripts/aegis_alpha_market_observer_context.sh
```

Manually run the old local report helper when needed:

```bash
PYTHONPATH=src .venv/bin/python scripts/mvp_pilot.py report --as-of-day YYYY-MM-DD --target-day YYYY-MM-DD --hermes-explain
```

## Boundaries

- `triggered=true` means the buy-point state machine fired.
- `post_hoc_attribution_label` is only after-the-fact explanation. It must not be used as a strategy filter or buy point.
- `strong_continuation_without_buy_point` means direction was strong but the main buy-point state machine did not trigger.
- jvQuant WebSocket events are converted to structured facts before Hermes sees them.
- Hermes cron/webhook should trigger agent reasoning; Aegis runner should not call LLMs.
