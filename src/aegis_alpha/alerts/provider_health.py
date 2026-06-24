from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aegis_alpha.clock import SH_TZ, now_iso


RESOURCE_MARKERS = (
    "insufficient balance",
    "insufficient funds",
    "payment required",
    "credit balance",
    "credits exhausted",
    "quota exceeded",
    "quota exhausted",
    "resource exhausted",
    "积分不足",
    "余额不足",
    "配额不足",
    "额度不足",
)
ACCESS_MARKERS = (
    "unauthorized",
    "forbidden",
    "permission denied",
    "not available in your region",
    "token expired",
    "token missing",
    "invalid token",
    "无权限",
    "权限不足",
    "地区不可用",
)
RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "请求过于频繁",
)


def classify_provider_error(error: str) -> str:
    text = str(error or "").strip().lower()
    if not text:
        return ""
    if "402" in text or any(marker in text for marker in RESOURCE_MARKERS):
        return "balance_or_quota"
    if "401" in text or "403" in text or any(marker in text for marker in ACCESS_MARKERS):
        return "access_or_entitlement"
    if "429" in text or any(marker in text for marker in RATE_LIMIT_MARKERS):
        return "rate_limit"
    return ""


def provider_health_event_path() -> Path:
    configured = os.environ.get("AEGIS_ALPHA_PROVIDER_HEALTH_EVENT_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    data_dir = os.environ.get("AEGIS_ALPHA_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "provider_health_events.jsonl"
    return Path(__file__).resolve().parents[3] / "data" / "provider_health_events.jsonl"


def record_provider_failure(
    *,
    provider: str,
    component: str,
    error: str,
    occurred_at: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    category = classify_provider_error(error)
    if not category:
        return False
    payload = {
        "provider": str(provider or "unknown"),
        "component": str(component or "unknown"),
        "category": category,
        "error": str(error or "").strip()[:500],
        "occurred_at": occurred_at or now_iso(),
        "metadata": metadata or {},
    }
    path = provider_health_event_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def load_provider_failures(*, limit: int = 200) -> list[dict[str, Any]]:
    path = provider_health_event_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, limit) :]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("category"):
            events.append(payload)
    return events


def failure_fingerprint(failure: dict[str, Any]) -> str:
    stable = "|".join(
        [
            str(failure.get("provider") or ""),
            str(failure.get("component") or ""),
            str(failure.get("category") or ""),
            str(failure.get("error") or "").lower(),
        ]
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]


def should_notify_failure(
    failure: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    cooldown_hours: float = 6.0,
) -> bool:
    fingerprint = failure_fingerprint(failure)
    previous = (state.get("sent") or {}).get(fingerprint) or {}
    occurrence = str(failure.get("occurred_at") or "")
    if previous.get("occurrence_at") == occurrence:
        return False
    sent_at_raw = str(previous.get("sent_at") or "")
    if not sent_at_raw:
        return True
    try:
        sent_at = datetime.fromisoformat(sent_at_raw)
    except ValueError:
        return True
    current = now or datetime.now(SH_TZ)
    return current - sent_at >= timedelta(hours=max(0.0, cooldown_hours))


def mark_failure_notified(
    failure: dict[str, Any],
    state: dict[str, Any],
    *,
    notified_at: str = "",
) -> None:
    sent = state.setdefault("sent", {})
    sent[failure_fingerprint(failure)] = {
        "sent_at": notified_at or now_iso(),
        "occurrence_at": str(failure.get("occurred_at") or ""),
    }
