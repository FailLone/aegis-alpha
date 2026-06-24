#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_SCRIPTS_DIR="${HERMES_SCRIPTS_DIR:-$HOME/.hermes/scripts}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
OBSERVER_PROFILE="${OBSERVER_PROFILE:-aegisobserver}"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$PROJECT_ROOT/.env.local}"
WEBHOOK_PORT="${WEBHOOK_PORT:-8644}"
WEBHOOK_ROUTE="${WEBHOOK_ROUTE:-aegis-alpha-alerts}"
WEBHOOK_URL="${WEBHOOK_URL:-http://127.0.0.1:${WEBHOOK_PORT}/webhooks/${WEBHOOK_ROUTE}}"
CREATE_JOBS=false
INSTALL_GATEWAY=false
CONFIGURE_MODEL=true

usage() {
  cat <<'USAGE'
Aegis Alpha Hermes-native automation installer

Installs thin context scripts for Hermes cron and optionally creates Hermes
cron/webhook jobs. Hermes remains the scheduler and event-triggered agent.
Aegis runner remains only the jvQuant WebSocket listener.

Usage:
  scripts/install_hermes_mvp_automation.sh [options]

Options:
  --create-jobs       Create Hermes cron jobs and webhook subscription.
  --install-gateway   Install/start Hermes gateway user service.
  --skip-model-config Do not set Hermes default model to DeepSeek direct.
  -h, --help          Show this help.

Environment:
  HERMES_SCRIPTS_DIR  Defaults to ~/.hermes/scripts.
  HERMES_ENV_FILE     Defaults to ~/.hermes/.env.
  OBSERVER_PROFILE    Defaults to aegisobserver.
  PROJECT_ENV_FILE    Defaults to <repo>/.env.local.
  WEBHOOK_PORT        Defaults to 8644.
  WEBHOOK_ROUTE       Defaults to aegis-alpha-alerts.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --create-jobs)
      CREATE_JOBS=true
      shift
      ;;
    --install-gateway)
      INSTALL_GATEWAY=true
      shift
      ;;
    --skip-model-config)
      CONFIGURE_MODEL=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

update_env_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  mkdir -p "$(dirname "$file")"
  touch "$file"
  if grep -q "^${key}=" "$file"; then
    perl -0pi -e "s|^${key}=.*$|${key}=${value}|m" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

secret_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

enable_hermes_webhook_platform() {
  local config_file="${HERMES_CONFIG:-$HOME/.hermes/config.yaml}"
  local port="$1"
  local secret="$2"
  "$PROJECT_ROOT/.venv/bin/python" - "$config_file" "$port" "$secret" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1]).expanduser()
port = int(sys.argv[2])
secret = sys.argv[3]
payload = yaml.safe_load(path.read_text()) if path.exists() else {}
payload = payload or {}
platforms = payload.setdefault("platforms", {})
platforms["webhook"] = {
    "enabled": True,
    "extra": {
        "host": "127.0.0.1",
        "port": port,
        "secret": secret,
    },
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
PY
}

remove_existing_cron_jobs_by_name() {
  local name="$1"
  local ids
  ids="$(hermes cron list 2>/dev/null | awk -v target="$name" '
    /^[[:space:]]+[[:alnum:]]+ \[active\]/ { id=$1 }
    /^[[:space:]]+Name:[[:space:]]+/ {
      job_name=$2
      if (job_name == target && id != "") print id
    }
  ')"
  for id in $ids; do
    hermes cron remove "$id" >/dev/null 2>&1 || true
  done
}

set_cron_job_profile() {
  local job_id="$1"
  local profile="$2"
  local hermes_agent_root="${HERMES_AGENT_ROOT:-$HOME/.hermes/hermes-agent}"
  HERMES_HOME="$HOME/.hermes" "$hermes_agent_root/venv/bin/python" - \
    "$hermes_agent_root" "$job_id" "$profile" <<'PY'
import json
import sys

sys.path.insert(0, sys.argv[1])
from tools.cronjob_tools import cronjob

result = json.loads(
    cronjob(action="update", job_id=sys.argv[2], skills=[], profile=sys.argv[3])
)
if not result.get("success"):
    raise SystemExit(result.get("error") or "failed to set cron profile")
PY
}

mkdir -p "$HERMES_SCRIPTS_DIR"

if [[ "$CONFIGURE_MODEL" == "true" ]]; then
  "$PROJECT_ROOT/scripts/switch_hermes_model.sh" deepseek
fi

cat > "$HERMES_SCRIPTS_DIR/aegis_alpha_mvp_prepare_context.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT_ROOT"
PYTHONPATH=src .venv/bin/python scripts/mvp_pilot.py cron-context prepare
EOF

cat > "$HERMES_SCRIPTS_DIR/aegis_alpha_mvp_report_context.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT_ROOT"
PYTHONPATH=src .venv/bin/python scripts/mvp_pilot.py cron-context report
EOF

cat > "$HERMES_SCRIPTS_DIR/aegis_alpha_mvp_export_subscription.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT_ROOT"
PYTHONPATH=src .venv/bin/python scripts/mvp_pilot.py export-subscription \\
  --scan-limit "\${AEGIS_ALPHA_SCAN_LIMIT:-50}" \\
  --allow-current-proxy-scan \\
  --update-env-local
launchctl kickstart -k "gui/\$(id -u)/com.aegis-alpha.runner" >/dev/null 2>&1 || true
EOF

cat > "$HERMES_SCRIPTS_DIR/aegis_alpha_market_observer_context.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT_ROOT"
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from aegis_alpha.config import load_project_env
from aegis_alpha.mcp.server import get_intraday_market_context, list_agent_observations

load_project_env()
now = datetime.now(ZoneInfo("Asia/Shanghai"))
trading_day = now.date().isoformat()
print(json.dumps({
    "hermes_cron_context": {
        "run_type": "agent_market_observer",
        "trading_day": trading_day,
        "now": now.isoformat(timespec="seconds"),
        "lookback_minutes": 30,
        "required_first_tool": "get_intraday_market_context",
        "write_tool": "record_agent_observation",
        "notification_tool": "notify_agent_observation",
        "safety": "research observation only; no buy/sell/order instruction",
    },
    "market_context_snapshot": get_intraday_market_context(lookback_minutes=30),
    "recent_agent_observations": list_agent_observations(trading_day=trading_day, limit=20),
}, ensure_ascii=False))
PY
EOF

cat > "$HERMES_SCRIPTS_DIR/aegis_alpha_provider_health_watch.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT_ROOT"
PYTHONPATH=src .venv/bin/python scripts/provider_health_watch.py
EOF

chmod +x "$HERMES_SCRIPTS_DIR/aegis_alpha_mvp_prepare_context.sh"
chmod +x "$HERMES_SCRIPTS_DIR/aegis_alpha_mvp_report_context.sh"
chmod +x "$HERMES_SCRIPTS_DIR/aegis_alpha_mvp_export_subscription.sh"
chmod +x "$HERMES_SCRIPTS_DIR/aegis_alpha_market_observer_context.sh"
chmod +x "$HERMES_SCRIPTS_DIR/aegis_alpha_provider_health_watch.sh"

if ! hermes profile show "$OBSERVER_PROFILE" >/dev/null 2>&1; then
  hermes profile create "$OBSERVER_PROFILE" --clone \
    --description "Lightweight Aegis Alpha intraday market observer with a narrow MCP tool surface."
fi
OBSERVER_PROFILE_DIR="$HOME/.hermes/profiles/$OBSERVER_PROFILE"
mkdir -p "$OBSERVER_PROFILE_DIR"
sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
  "$PROJECT_ROOT/.hermes/config/aegis-observer-profile.yaml" \
  > "$OBSERVER_PROFILE_DIR/config.yaml"
mkdir -p "$OBSERVER_PROFILE_DIR/scripts"
for script_name in \
  aegis_alpha_mvp_prepare_context.sh \
  aegis_alpha_mvp_report_context.sh \
  aegis_alpha_mvp_export_subscription.sh \
  aegis_alpha_market_observer_context.sh \
  aegis_alpha_provider_health_watch.sh
do
  cp "$HERMES_SCRIPTS_DIR/$script_name" "$OBSERVER_PROFILE_DIR/scripts/$script_name"
  chmod +x "$OBSERVER_PROFILE_DIR/scripts/$script_name"
done

echo "Installed Hermes context scripts:"
echo "  $HERMES_SCRIPTS_DIR/aegis_alpha_mvp_prepare_context.sh"
echo "  $HERMES_SCRIPTS_DIR/aegis_alpha_mvp_report_context.sh"
echo "  $HERMES_SCRIPTS_DIR/aegis_alpha_mvp_export_subscription.sh"
echo "  $HERMES_SCRIPTS_DIR/aegis_alpha_market_observer_context.sh"
echo "  $HERMES_SCRIPTS_DIR/aegis_alpha_provider_health_watch.sh"

if [[ "$INSTALL_GATEWAY" == "true" ]]; then
  update_env_key "$HERMES_ENV_FILE" WEBHOOK_ENABLED true
  update_env_key "$HERMES_ENV_FILE" WEBHOOK_PORT "$WEBHOOK_PORT"
  update_env_key "$HERMES_ENV_FILE" WEBHOOK_SECRET "$(secret_hex)"
  export WEBHOOK_ENABLED=true
  export WEBHOOK_PORT="$WEBHOOK_PORT"
  export WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(grep '^WEBHOOK_SECRET=' "$HERMES_ENV_FILE" | tail -1 | cut -d= -f2-)}"
  enable_hermes_webhook_platform "$WEBHOOK_PORT" "$WEBHOOK_SECRET"
  hermes gateway install
fi

if [[ "$CREATE_JOBS" == "true" ]]; then
  WEBHOOK_SECRET_VALUE="$(secret_hex)"
  update_env_key "$PROJECT_ENV_FILE" AEGIS_ALPHA_HERMES_WEBHOOK_ENABLED true
  update_env_key "$PROJECT_ENV_FILE" HERMES_AEGIS_WEBHOOK_URL "$WEBHOOK_URL"
  update_env_key "$PROJECT_ENV_FILE" HERMES_AEGIS_WEBHOOK_SECRET "$WEBHOOK_SECRET_VALUE"
  export WEBHOOK_ENABLED=true
  export WEBHOOK_PORT="$WEBHOOK_PORT"
  export WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(grep '^WEBHOOK_SECRET=' "$HERMES_ENV_FILE" | tail -1 | cut -d= -f2-)}"
  enable_hermes_webhook_platform "$WEBHOOK_PORT" "$WEBHOOK_SECRET"

  hermes webhook remove "$WEBHOOK_ROUTE" >/dev/null 2>&1 || true
  hermes webhook subscribe "$WEBHOOK_ROUTE" \
    --events "aegis.buy_point_alert,aegis.candidate_investigation,aegis.selection_validation,aegis.seal_order_decay,aegis.big_order_inflow_spike,aegis.theme_divergence,aegis.theme_leader_break_board,aegis.sector_rotation,aegis.market_bottom_reversal" \
    --skills "second-board-radar" \
    --deliver "log" \
    --secret "$WEBHOOK_SECRET_VALUE" \
    --description "Aegis Alpha runner alerts trigger Hermes observation enrichment" \
    --prompt "Aegis Alpha runner alert: {summary.title}
severity={summary.severity}
symbol={summary.symbol}
theme={summary.theme}
body={summary.body}
event_id={summary.event_id}

请使用 second-board-radar skill 的 Agent 市场观察流程处理该 alert：
1. 调用 get_pending_alerts(limit=50)，按 event_id 找到当前告警的真实 alert_id；
2. 调用 get_intraday_market_context(lookback_minutes=30)；
3. 如果 symbol 非空，调用 get_realtime_symbol_context(symbol, lookback_minutes=30)；
4. 如果 theme 或 symbol 可用，调用 get_intraday_theme_context(theme_or_symbol, lookback_minutes=30)；
5. 判断它是 buy_point_quality、theme_rotation、market_regime_shift、strong_continuation_without_buy_point、watchlist_observation、data_gap 还是 noise_or_rejected_trigger。CANDIDATE_INVESTIGATION 若存在同题材联动或强势延续事实，应使用 strong_continuation_without_buy_point；只有事实支持持续跟踪时使用 stance=actionable_watch；
6. 只有事实足够时调用 record_agent_observation，source 必须使用 trigger_enrichment，并把真实 alert_id 写入 linked_alert_ids_json；必须写 evidence/counter_evidence/data_gaps，且不输出买卖指令；
7. 处理完成后调用 acknowledge_alert(alert_id, note=...)。如果 record_agent_observation 返回 notification_grade=urgent 或 important，调用 notify_agent_observation(observation_id)。
最后用中文输出 observation_id、notification_grade、是否推送、核心依据和缺口。"

  remove_existing_cron_jobs_by_name "aegis-alpha-daily-prepare"
  hermes cron create \
    --name "aegis-alpha-daily-prepare" \
    --workdir "$PROJECT_ROOT" \
    --skill "second-board-radar" \
    --script "aegis_alpha_mvp_prepare_context.sh" \
    --deliver "local" \
    "10 16 * * 1-5" \
    "根据脚本输出的 hermes_cron_context，站在 suggested_as_of_day 收盘，调用 get_daily_strategy_candidate_pool(candidate_limit)，选择 Top3 作为重点审计对象，并调用 record_selection_audit。Top3 不是 runner 的全部扫描范围；runner 应使用更大的 strategy scan pool。完成后输出 audit_id、Top3、主要缺口和 scan_limit。不要读取未来 target_day 结果，不要输出买卖指令。"

  remove_existing_cron_jobs_by_name "aegis-alpha-morning-report"
  hermes cron create \
    --name "aegis-alpha-morning-report" \
    --workdir "$PROJECT_ROOT" \
    --skill "second-board-radar" \
    --script "aegis_alpha_mvp_report_context.sh" \
    --deliver "local" \
    "30 10 * * 1-5" \
    "根据脚本输出的 hermes_cron_context，读取最新 selection audit、runner alerts、runner subscribed symbols、get_selection_trigger_validation，并解释今天早盘是否触发主策略。说明 Top3 审计结果和更大 scan pool alert 是两层；区分主策略触发、强势延续未触发、二板接力路径和未启动；不要输出买卖指令。"

  remove_existing_cron_jobs_by_name "aegis-alpha-export-subscription"
  hermes cron create \
    --name "aegis-alpha-export-subscription" \
    --workdir "$PROJECT_ROOT" \
    --script "aegis_alpha_mvp_export_subscription.sh" \
    --no-agent \
    --deliver "local" \
    "20 16 * * 1-5" \
    "Export latest Aegis Alpha scan-pool subscription and restart runner."

  remove_existing_cron_jobs_by_name "aegis-alpha-provider-health"
  hermes cron create \
    --name "aegis-alpha-provider-health" \
    --workdir "$PROJECT_ROOT" \
    --script "aegis_alpha_provider_health_watch.sh" \
    --no-agent \
    --deliver "local" \
    "*/5 * * * *" \
    "Check Agent and market-data provider balance, quota, entitlement, and rate-limit failures."

  read -r -d '' OBSERVER_PROMPT <<'PROMPT' || true
根据脚本输出的 hermes_cron_context 与 market_context_snapshot，执行轻量 Agent 市场观察流程：
1. 先调用 get_pending_alerts(limit=50)。BUYPOINT_ALERT 优先调查；CANDIDATE_INVESTIGATION 表示高置信强势候选，即使没有标准买点也必须逐只调查，不能被 strongest_events TopN 挤掉。
2. 检查 runner_state/freshness/data_gaps；如果 runner 未运行或数据不足，可以记录 data_gap/insufficient_data。
3. 对每个待调查 symbol 调用 get_realtime_symbol_context(symbol, lookback_minutes=30)；对已识别 theme 调用 get_intraday_theme_context(theme_or_symbol, lookback_minutes=30)。同时检查 strongest_events、APPROACHING_LIMIT_UP、BIG_ORDER_INFLOW_SPIKE、SEAL_ORDER_DECAY 和板块事件。
4. 如果发现买点质量、题材轮动、市场状态变化、强势延续但未触发买点、或重要数据缺口，调用 record_agent_observation，source 必须使用 periodic_market_scan。高置信强势但没有完整买点时，使用 observation_type=strong_continuation_without_buy_point；只有事实支持值得持续跟踪时才使用 stance=actionable_watch。
5. 处理完待办后调用 acknowledge_alert(alert_id, note=...)。若 observation 的 notification_grade 为 urgent 或 important，调用 notify_agent_observation(observation_id)。
6. 输出 concise 中文摘要：本轮是否有观察、observation_id、notification_grade、是否推送 WeClaw、主要依据、主要缺口。不要为了凑数强行产出观察。
PROMPT

  observer_jobs=(
    "aegis-alpha-market-observer-0942|42 9 * * 1-5"
    "aegis-alpha-market-observer-1015|15 10 * * 1-5"
    "aegis-alpha-market-observer-1118|18 11 * * 1-5"
    "aegis-alpha-market-observer-1335|35 13 * * 1-5"
    "aegis-alpha-market-observer-1420|20 14 * * 1-5"
    "aegis-alpha-market-observer-1455|55 14 * * 1-5"
  )
  for spec in "${observer_jobs[@]}"; do
    name="${spec%%|*}"
    schedule="${spec#*|}"
    remove_existing_cron_jobs_by_name "$name"
    create_output="$(hermes cron create \
      --name "$name" \
      --workdir "$PROJECT_ROOT" \
      --script "aegis_alpha_market_observer_context.sh" \
      --deliver "local" \
      "$schedule" \
      "$OBSERVER_PROMPT")"
    echo "$create_output"
    observer_job_id="$(printf '%s\n' "$create_output" | awk '/Created job:/ {print $3; exit}')"
    if [[ -z "$observer_job_id" ]]; then
      echo "Unable to determine observer cron job id for $name" >&2
      exit 1
    fi
    # Hermes CLI pre-parses --profile as a process profile override. Update the
    # root scheduler job through Hermes' own cron API so it stores a per-job profile.
    set_cron_job_profile "$observer_job_id" "$OBSERVER_PROFILE"
  done

  echo "Hermes cron jobs and webhook subscription created."
  echo "Runner webhook env written to: $PROJECT_ENV_FILE"
fi

echo "Next checks:"
echo "  hermes gateway status"
echo "  hermes cron list"
echo "  hermes webhook list"
