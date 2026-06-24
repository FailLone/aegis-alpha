from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from aegis_alpha.logging_setup import get_logger
from aegis_alpha.models import AgentAlert, AgentObservation


_LOGGER = get_logger(__name__)

DEFAULT_ALLOWED_PREFIXES = ("BUYPOINT_ALERT", "SELECTION_VALIDATION")
DEFAULT_OBSERVATION_GRADES = ("urgent", "important")


def weclaw_notification_enabled(config: dict[str, Any]) -> bool:
    cfg = config.get("weclaw_notification", {}) or {}
    raw = os.environ.get("AEGIS_ALPHA_WECLAW_ENABLED")
    if raw is None:
        raw = cfg.get("enabled", "false")
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def weclaw_api_url(config: dict[str, Any]) -> str:
    cfg = config.get("weclaw_notification", {}) or {}
    env_key = str(cfg.get("api_url_env") or "AEGIS_ALPHA_WECLAW_API_URL")
    return str(cfg.get("api_url") or os.environ.get(env_key, "http://127.0.0.1:18011/api/send")).strip()


def weclaw_target(config: dict[str, Any]) -> str:
    cfg = config.get("weclaw_notification", {}) or {}
    env_key = str(cfg.get("target_env") or "AEGIS_ALPHA_WECLAW_TO")
    return str(cfg.get("target") or os.environ.get(env_key, "")).strip()


def weclaw_timeout(config: dict[str, Any]) -> float:
    cfg = config.get("weclaw_notification", {}) or {}
    try:
        return max(0.2, float(cfg.get("timeout_seconds", 3)))
    except (TypeError, ValueError):
        return 3.0


def allowed_title_prefixes(config: dict[str, Any]) -> tuple[str, ...]:
    cfg = config.get("weclaw_notification", {}) or {}
    raw = cfg.get("allowed_title_prefixes")
    if raw is None:
        return DEFAULT_ALLOWED_PREFIXES
    if isinstance(raw, str):
        prefixes = [item.strip() for item in raw.split(",")]
    else:
        prefixes = [str(item).strip() for item in raw]
    return tuple(prefix for prefix in prefixes if prefix)


def should_post_alert_to_weclaw(alert: AgentAlert, config: dict[str, Any]) -> bool:
    if not weclaw_notification_enabled(config):
        return False
    prefixes = allowed_title_prefixes(config)
    if not prefixes:
        return True
    return alert.title.startswith(prefixes)


def render_weclaw_alert_text(alert: AgentAlert) -> str:
    lines = [f"[Aegis] {alert.title}"]
    if alert.symbol:
        lines.append(f"标的：{alert.symbol}")
    if alert.theme:
        lines.append(f"主题：{alert.theme}")
    lines.append(f"级别：{alert.severity}")
    if alert.body:
        lines.append(f"依据：{alert.body}")
    lines.append(f"时间：{alert.created_at}")
    return "\n".join(lines)


def post_alert_to_weclaw(alert: AgentAlert, config: dict[str, Any]) -> bool:
    if not should_post_alert_to_weclaw(alert, config):
        return False

    url = weclaw_api_url(config)
    target = weclaw_target(config)
    if not url or not target:
        _LOGGER.warning("event=weclaw_notify_skip reason=missing_url_or_target")
        return False

    body = json.dumps(
        {
            "to": target,
            "text": render_weclaw_alert_text(alert),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=weclaw_timeout(config)) as response:
            ok = 200 <= response.status < 300
            if not ok:
                _LOGGER.warning("event=weclaw_notify_failed status=%s", response.status)
            return ok
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        _LOGGER.warning("event=weclaw_notify_failed status=%s body=%s", exc.code, detail)
        return False
    except (urllib.error.URLError, TimeoutError) as exc:
        _LOGGER.warning("event=weclaw_notify_failed error=%s", type(exc).__name__)
        return False
    except Exception as exc:
        _LOGGER.warning("event=weclaw_notify_failed error=%s", type(exc).__name__)
        return False


def post_system_message_to_weclaw(text: str, config: dict[str, Any] | None = None) -> bool:
    """Send an operational message without market-signal filtering.

    Billing, quota, authentication, and provider-health failures must not depend
    on an agent or an observation grade to reach the operator.
    """
    resolved_config = config or {}
    if not weclaw_notification_enabled(resolved_config):
        return False

    url = weclaw_api_url(resolved_config)
    target = weclaw_target(resolved_config)
    if not url or not target or not text.strip():
        _LOGGER.warning("event=weclaw_system_notify_skip reason=missing_url_target_or_text")
        return False

    body = json.dumps(
        {"to": target, "text": text.strip()},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=weclaw_timeout(resolved_config)) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        _LOGGER.warning(
            "event=weclaw_system_notify_failed status=%s body=%s",
            exc.code,
            detail,
        )
        return False
    except Exception as exc:
        _LOGGER.warning(
            "event=weclaw_system_notify_failed error=%s",
            type(exc).__name__,
        )
        return False


def allowed_observation_grades(config: dict[str, Any]) -> tuple[str, ...]:
    cfg = config.get("weclaw_notification", {}) or {}
    raw = cfg.get("observation_grades")
    if raw is None:
        return DEFAULT_OBSERVATION_GRADES
    if isinstance(raw, str):
        grades = [item.strip() for item in raw.split(",")]
    else:
        grades = [str(item).strip() for item in raw]
    return tuple(grade for grade in grades if grade)


def should_post_observation_to_weclaw(observation: AgentObservation, config: dict[str, Any]) -> bool:
    """Deterministic gate: push only configured notification grades.

    The grade is computed from the observation's structured fields, never
    authored by the agent. WeClaw stays conservative — default urgent +
    important only.
    """
    if not weclaw_notification_enabled(config):
        return False
    from aegis_alpha.feedback.agent_observation import observation_notification_grade

    grade = observation_notification_grade(observation)
    return grade in allowed_observation_grades(config)


def render_weclaw_observation_text(observation: AgentObservation) -> str:
    lines = [f"[Aegis] {observation.title or '市场观察'}"]
    if observation.symbol:
        lines.append(f"标的：{observation.symbol}")
    if observation.theme:
        lines.append(f"主题：{observation.theme}")
    if observation.summary:
        lines.append(f"结论：{observation.summary}")
    if observation.evidence:
        lines.append("依据：" + "；".join(observation.evidence[:3]))
    risk_parts: list[str] = []
    if observation.counter_evidence:
        risk_parts.extend(observation.counter_evidence[:2])
    if observation.data_gaps:
        risk_parts.extend(observation.data_gaps[:2])
    if risk_parts:
        lines.append("风险/缺口：" + "；".join(risk_parts))
    lines.append(f"置信：{observation.confidence}")
    if observation.created_at:
        lines.append(f"时间：{observation.created_at}")
    return "\n".join(lines)


def post_observation_to_weclaw(observation: AgentObservation, config: dict[str, Any]) -> bool:
    if not should_post_observation_to_weclaw(observation, config):
        return False

    url = weclaw_api_url(config)
    target = weclaw_target(config)
    if not url or not target:
        _LOGGER.warning("event=weclaw_observation_skip reason=missing_url_or_target")
        return False

    body = json.dumps(
        {"to": target, "text": render_weclaw_observation_text(observation)},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=weclaw_timeout(config)) as response:
            ok = 200 <= response.status < 300
            if not ok:
                _LOGGER.warning("event=weclaw_observation_failed status=%s", response.status)
            return ok
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        _LOGGER.warning("event=weclaw_observation_failed status=%s body=%s", exc.code, detail)
        return False
    except (urllib.error.URLError, TimeoutError) as exc:
        _LOGGER.warning("event=weclaw_observation_failed error=%s", type(exc).__name__)
        return False
    except Exception as exc:
        _LOGGER.warning("event=weclaw_observation_failed error=%s", type(exc).__name__)
        return False
