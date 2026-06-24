from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aegis_alpha.alerts.provider_health import (
    classify_provider_error,
    load_provider_failures,
    mark_failure_notified,
    should_notify_failure,
)
from aegis_alpha.alerts.weclaw_notifier import post_system_message_to_weclaw
from aegis_alpha.clock import now_iso
from aegis_alpha.config import load_project_env
from aegis_alpha.runner import load_runner_config, status_payload


def _hermes_jobs_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return home / "cron" / "jobs.json"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _agent_failures() -> list[dict[str, Any]]:
    raw = _load_json(_hermes_jobs_path(), [])
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
    failures: list[dict[str, Any]] = []
    for job in jobs if isinstance(jobs, list) else []:
        name = str(job.get("name") or "")
        error = str(job.get("last_error") or "")
        category = classify_provider_error(error)
        if (
            not name.startswith("aegis-alpha-")
            or name == "aegis-alpha-provider-health"
            or job.get("last_status") != "error"
            or not category
        ):
            continue
        failures.append(
            {
                "provider": "agent_model",
                "component": name,
                "category": category,
                "error": error[:500],
                "occurred_at": str(job.get("last_run_at") or ""),
                "metadata": {"job_id": job.get("id")},
            }
        )
    return failures


def _runner_failure() -> list[dict[str, Any]]:
    status = status_payload("config/runner.yaml")
    error = str(status.get("last_error") or "")
    category = classify_provider_error(error)
    if not category:
        return []
    return [
        {
            "provider": str(status.get("provider") or "jvQuant"),
            "component": "realtime_runner",
            "category": category,
            "error": error[:500],
            "occurred_at": str(status.get("updated_at") or ""),
            "metadata": {"state": status.get("state")},
        }
    ]


def _gateway_log_failures(state: dict[str, Any]) -> list[dict[str, Any]]:
    log_path = Path(
        os.environ.get(
            "HERMES_GATEWAY_LOG_PATH",
            str(Path.home() / ".hermes" / "logs" / "gateway.log"),
        )
    ).expanduser()
    if not log_path.exists():
        return []

    size = log_path.stat().st_size
    previous_offset = int(state.get("gateway_log_offset") or 0)
    if previous_offset < 0 or previous_offset > size:
        previous_offset = 0
    with log_path.open("rb") as handle:
        handle.seek(previous_offset)
        raw = handle.read()
    state["gateway_log_offset"] = size
    if not raw:
        return []

    text = raw.decode("utf-8", errors="replace")
    blocks = re.split(r"(?=⚠️\s+API call failed)", text)
    failures: list[dict[str, Any]] = []
    for block in blocks:
        category = classify_provider_error(block)
        if not category or "API call failed" not in block:
            continue
        provider_match = re.search(r"Provider:\s*([^\s]+)", block)
        error_match = re.search(r"(?:Error|Details):\s*(.+)", block)
        failures.append(
            {
                "provider": provider_match.group(1) if provider_match else "agent_model",
                "component": "gateway_inference",
                "category": category,
                "error": (
                    error_match.group(1).strip()[:500]
                    if error_match
                    else block.strip()[:500]
                ),
                "occurred_at": now_iso(),
                "metadata": {"source": str(log_path)},
            }
        )
    return failures


def collect_failures(state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    resolved_state = state if state is not None else {}
    combined = [
        *load_provider_failures(),
        *_agent_failures(),
        *_runner_failure(),
        *_gateway_log_failures(resolved_state),
    ]
    latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for failure in combined:
        key = (
            str(failure.get("provider") or ""),
            str(failure.get("component") or ""),
            str(failure.get("category") or ""),
            str(failure.get("error") or ""),
        )
        current = latest.get(key)
        if current is None or str(failure.get("occurred_at") or "") > str(
            current.get("occurred_at") or ""
        ):
            latest[key] = failure
    return sorted(latest.values(), key=lambda item: str(item.get("occurred_at") or ""))


def render_failure_message(failures: list[dict[str, Any]]) -> str:
    labels = {
        "balance_or_quota": "余额或配额不足",
        "access_or_entitlement": "权限或地区限制",
        "rate_limit": "调用频率受限",
    }
    lines = ["[Aegis 系统告警] 数据或 Agent 服务不可用"]
    for failure in failures[:5]:
        lines.append(
            f"- {failure.get('provider')}/{failure.get('component')}: "
            f"{labels.get(str(failure.get('category')), failure.get('category'))}"
        )
        lines.append(f"  错误：{str(failure.get('error') or '')[:180]}")
        if failure.get("occurred_at"):
            lines.append(f"  时间：{failure['occurred_at']}")
    if len(failures) > 5:
        lines.append(f"- 另有 {len(failures) - 5} 条同类故障，请检查运行日志。")
    lines.append("影响：相关自动判断可能未执行；这不代表市场没有信号。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Notify operator about provider resource failures.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cooldown-hours", type=float, default=None)
    args = parser.parse_args()

    load_project_env()
    runner_config = load_runner_config("config/runner.yaml")
    health_config = runner_config.get("provider_health_notification", {}) or {}
    if not health_config.get("enabled", True):
        print(json.dumps({"checked_at": now_iso(), "disabled": True}, ensure_ascii=False))
        return 0
    cooldown_hours = (
        args.cooldown_hours
        if args.cooldown_hours is not None
        else float(health_config.get("cooldown_hours") or 6.0)
    )
    state_path = Path(
        os.environ.get(
            "AEGIS_ALPHA_PROVIDER_HEALTH_STATE_PATH",
            str(ROOT / "data" / "provider_health_notification_state.json"),
        )
    ).expanduser()
    state = _load_json(state_path, {"sent": {}})
    failures = collect_failures(state)
    pending = [
        failure
        for failure in failures
        if should_notify_failure(
            failure,
            state,
            cooldown_hours=cooldown_hours,
        )
    ]

    posted = False
    if pending and not args.dry_run:
        posted = post_system_message_to_weclaw(render_failure_message(pending))
        if posted:
            for failure in pending:
                mark_failure_notified(failure, state)
    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "checked_at": now_iso(),
                "failure_count": len(failures),
                "pending_count": len(pending),
                "posted": posted,
                "dry_run": args.dry_run,
                "failures": pending,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
