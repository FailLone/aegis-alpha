from __future__ import annotations

import logging
import os
import re
from typing import Any

from aegis_alpha.alerts.provider_health import (
    classify_provider_error,
    record_provider_failure,
)
from aegis_alpha.events import SignalWindowBuffer, now_iso
from aegis_alpha.models import RealtimeConnectionStatus
from aegis_alpha.signals.orderbook import estimate_from_lv10_object


def normalize_realtime_symbol(symbol: str) -> str:
    return symbol.strip().upper().split(".", 1)[0]


def subscription_codes(symbols: list[str], levels: list[str]) -> list[str]:
    clean_levels = [level.lower() for level in levels if level.lower() in {"lv1", "lv2", "lv10"}]
    return [
        f"{level}_{normalize_realtime_symbol(symbol)}"
        for symbol in symbols
        for level in clean_levels
    ]


def summarize_raw_ab_payload(text: str, *, max_rows: int = 20, include_samples: bool = False) -> dict[str, Any]:
    rows = [row for row in text.splitlines() if row.strip()]
    summary: dict[str, Any] = {"row_count": len(rows), "levels": {}}
    samples: list[dict[str, Any]] = []
    for row in rows[:max_rows]:
        if "=" not in row:
            continue
        symbol, payload = row.split("=", 1)
        level = symbol.split("_", 1)[0]
        pieces = [piece for piece in payload.split("|") if piece]
        latest = pieces[-1] if pieces else ""
        fields = latest.split(",") if latest else []
        level_summary = summary["levels"].setdefault(
            level,
            {"row_count": 0, "max_piece_count": 0, "latest_field_counts": []},
        )
        level_summary["row_count"] += 1
        level_summary["max_piece_count"] = max(level_summary["max_piece_count"], len(pieces))
        level_summary["latest_field_counts"].append(len(fields))
        sample = {
            "level": level,
            "symbol": re.sub(r"^(lv1|lv2|lv10)_", "", symbol),
            "piece_count": len(pieces),
            "latest_field_count": len(fields),
        }
        if include_samples:
            sample["latest_fields"] = fields
        samples.append(sample)
    summary["samples"] = samples
    return summary


def raw_lv2_large_trade_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in [item for item in text.splitlines() if item.strip()]:
        if "=" not in row:
            continue
        symbol_key, payload = row.split("=", 1)
        if not symbol_key.startswith("lv2_"):
            continue
        symbol = re.sub(r"^lv2_", "", symbol_key)
        for piece in [item for item in payload.split("|") if item]:
            fields = piece.split(",")
            if len(fields) < 4:
                continue
            try:
                price = float(fields[2])
                volume = float(fields[3])
            except ValueError:
                continue
            records.append(
                {
                    "symbol": symbol,
                    "time": fields[0],
                    "trade_id": fields[1],
                    "price": price,
                    "volume": volume,
                }
            )
    return records


class JvQuantRealtimeClient:
    """Thin read-only jvQuant WebSocket wrapper.

    This client updates local buffers from parsed SDK callbacks. It does not expose
    raw WebSocket payloads to MCP tools or agents.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        market: str | None = None,
        buffer: SignalWindowBuffer | None = None,
        raw_data_handle: Any | None = None,
        big_order_threshold_cny: float | None = None,
    ) -> None:
        self.token = token or os.environ.get("JVQUANT_TOKEN", "")
        self.market = market or os.environ.get("JVQUANT_MARKET", "ab")
        self.buffer = buffer or SignalWindowBuffer()
        self.raw_data_handle = raw_data_handle
        self.big_order_threshold_cny = (
            float(big_order_threshold_cny)
            if big_order_threshold_cny is not None
            else float(os.environ.get("AEGIS_ALPHA_BIG_ORDER_THRESHOLD_CNY", "3000000"))
        )
        self._client: Any | None = None
        self._connected = False
        self._subscribed: set[str] = set()
        self._last_message_at = ""
        self._last_error = ""

    def connect(self) -> RealtimeConnectionStatus:
        if not self.token:
            self._last_error = "JVQUANT_TOKEN missing"
            record_provider_failure(
                provider="jvQuant",
                component="websocket_connect",
                error=self._last_error,
            )
            return self.status()
        try:
            from jvQuant import websocket_client

            self._client = websocket_client.Construct(
                market=self.market,
                token=self.token,
                log_level=logging.ERROR,
                log_handle=self._on_log,
                data_handle=self._on_raw_data,
                ab_lv1_handle=self._on_ab_lv1,
                ab_lv2_handle=self._on_ab_lv2,
                ab_lv10_handle=self._on_ab_lv10,
            )
            self._connected = True
            self._last_error = ""
        except Exception as exc:
            self._connected = False
            error = str(exc) or type(exc).__name__
            self._last_error = error if classify_provider_error(error) else type(exc).__name__
            record_provider_failure(
                provider="jvQuant",
                component="websocket_connect",
                error=error,
            )
        return self.status()

    def subscribe(self, symbols: list[str], levels: list[str] | None = None) -> RealtimeConnectionStatus:
        if self._client is None:
            self.connect()
        if self._client is None:
            return self.status()
        clean_symbols = [normalize_realtime_symbol(symbol) for symbol in symbols if symbol.strip()]
        clean_levels = [level.lower() for level in (levels or ["lv1", "lv2", "lv10"])]
        try:
            if "lv1" in clean_levels:
                self._client.add_lv1(clean_symbols)
            if "lv2" in clean_levels:
                self._client.add_lv2(clean_symbols)
            if "lv10" in clean_levels:
                self._client.add_lv10(clean_symbols)
            self._subscribed.update(subscription_codes(clean_symbols, clean_levels))
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._last_error = error if classify_provider_error(error) else type(exc).__name__
            record_provider_failure(
                provider="jvQuant",
                component="websocket_subscribe",
                error=error,
            )
        return self.status()

    def unsubscribe(self, symbols: list[str], levels: list[str] | None = None) -> RealtimeConnectionStatus:
        if self._client is None:
            return self.status()
        clean_symbols = [normalize_realtime_symbol(symbol) for symbol in symbols if symbol.strip()]
        clean_levels = [level.lower() for level in (levels or ["lv1", "lv2", "lv10"])]
        try:
            if "lv1" in clean_levels:
                self._client.del_lv1(clean_symbols)
            if "lv2" in clean_levels:
                self._client.del_lv2(clean_symbols)
            if "lv10" in clean_levels:
                self._client.del_lv10(clean_symbols)
            self._subscribed.difference_update(subscription_codes(clean_symbols, clean_levels))
        except Exception as exc:
            self._last_error = type(exc).__name__
        return self.status()

    def disconnect(self) -> RealtimeConnectionStatus:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:
                self._last_error = type(exc).__name__
        self._connected = False
        self._subscribed.clear()
        self._last_message_at = ""
        return self.status()

    def status(self) -> RealtimeConnectionStatus:
        return RealtimeConnectionStatus(
            provider="jvQuant",
            market=self.market,
            connected=self._connected,
            subscribed=sorted(self._subscribed),
            last_message_at=self._last_message_at,
            last_error=self._last_error,
            notes=[
                "WebSocket feeds update the local signal engine; raw messages are not exposed to agents.",
                "MCP tools should consume SignalSnapshot and MarketEvent outputs instead.",
            ],
        )

    def _on_log(self, message: str) -> None:
        raw = str(message or "").strip()
        text = raw.lower()
        category = classify_provider_error(raw)
        if category:
            self._last_error = raw[:500]
            record_provider_failure(
                provider="jvQuant",
                component="websocket_log",
                error=raw,
            )
            return
        if any(marker in text for marker in ("lost", "goodbye", "closed", "disconnect")):
            self._connected = False
            self._last_error = "provider_connection_lost"
        elif "error" in text or "失败" in raw:
            self._last_error = "provider_log_error"

    def _on_raw_data(self, text: str) -> None:
        self._last_message_at = now_iso()
        if callable(self.raw_data_handle):
            self.raw_data_handle(text)

    def _on_ab_lv1(self, lv1: Any) -> None:
        self._last_message_at = now_iso()
        self.buffer.add_price(
            str(lv1.code),
            self._provider_time(lv1.time),
            float(lv1.price),
            float(getattr(lv1, "amount", 0.0)),
            change_pct=float(getattr(lv1, "ratio", 0.0)),
        )

    def _on_ab_lv2(self, lv2: Any) -> None:
        self._last_message_at = now_iso()
        for deal in getattr(lv2, "deal_list", []):
            self.buffer.add_large_trade_proxy(
                str(lv2.code),
                self._provider_time(getattr(deal, "time", getattr(lv2, "time", ""))),
                float(getattr(deal, "price", 0.0)),
                float(getattr(deal, "volume", 0.0)),
                threshold_cny=self.big_order_threshold_cny,
            )

    def _on_ab_lv10(self, lv10: Any) -> None:
        self._last_message_at = now_iso()
        self.buffer.add_price(
            str(lv10.code),
            self._provider_time(lv10.time),
            float(lv10.price),
            float(getattr(lv10, "amount", 0.0)),
            change_pct=float(getattr(lv10, "ratio", 0.0)),
        )
        symbol = str(lv10.code)
        metrics = estimate_from_lv10_object(
            lv10,
            previous_seal_amount_cny=self.buffer.previous_seal_amount(symbol),
        )
        self.buffer.set_orderbook_metrics(
            symbol,
            quality_score=metrics.orderbook_quality_score,
            ask_pressure_score=metrics.ask_pressure_score,
            seal_amount_cny=metrics.seal_amount_cny,
            seal_decay_pct=metrics.seal_decay_pct,
            sell_wall_amount_cny=metrics.sell_wall_amount_cny,
            notes=metrics.notes,
        )

    def _provider_time(self, value: str) -> str:
        text = str(value or "").strip()
        if len(text) == 8 and text.count(":") == 2:
            return f"{now_iso()[:10]}T{text}+08:00"
        if "T" in text:
            return text
        return now_iso()
