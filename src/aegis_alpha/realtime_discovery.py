from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis_alpha.adapters.jvquant import historical_second_board as HSB
from aegis_alpha.adapters.jvquant import parsers as P
from aegis_alpha.symbols import normalize_symbol


@dataclass(frozen=True)
class RealtimeDiscoveryResult:
    symbols: list[str]
    discovered_symbols: list[str]
    source_counts: dict[str, int] = field(default_factory=dict)
    symbol_metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def merge_symbols(*groups: list[str], cap: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            symbol = normalize_symbol(raw)
            if not symbol or symbol in seen:
                continue
            output.append(symbol)
            seen.add(symbol)
            if len(output) >= cap:
                return output
    return output


def _merge_symbol_metadata(
    existing: dict[str, str] | None,
    incoming: dict[str, str],
) -> dict[str, str]:
    current = existing or {}
    return {
        "name": (
            incoming.get("name")
            if incoming.get("name") not in {"", "unknown", None}
            else current.get("name", "unknown")
        ),
        "theme": (
            incoming.get("theme")
            if incoming.get("theme") not in {"", "unknown", None}
            else current.get("theme", "unknown")
        ),
    }


def discover_realtime_symbols(
    adapter: Any,
    *,
    base_symbols: list[str],
    max_symbols: int = 200,
    seed_turnover_yi: int = 30,
    include_current_limitup: bool = True,
    include_current_large_turnover: bool = True,
) -> RealtimeDiscoveryResult:
    """Discover a live observation universe from current provider facts.

    This is intentionally facts-only. It widens the realtime subscription universe
    but does not grade, rank, or create buy/sell instructions.
    """

    safe_cap = max(1, int(max_symbols or 200))
    base = merge_symbols(base_symbols, cap=safe_cap)
    source_counts: dict[str, int] = {"base": len(base)}
    errors: list[str] = []
    limitup_symbols: list[str] = []
    turnover_symbols: list[str] = []
    symbol_metadata: dict[str, dict[str, str]] = {}

    if include_current_large_turnover:
        try:
            query_fn = getattr(adapter, "_query")
            for query in HSB.current_large_turnover_strategy_queries(seed_turnover_yi):
                payload = query_fn(query, sort_key="成交额")
                for row in P._query_rows(payload):
                    symbol = normalize_symbol(P._symbol_from_row(row))
                    if symbol:
                        turnover_symbols.append(symbol)
                        symbol_metadata[symbol] = _merge_symbol_metadata(
                            symbol_metadata.get(symbol),
                            {
                                "name": P._name_from_row(row) or "unknown",
                                "theme": P._theme_from_row(row),
                            },
                        )
        except Exception as exc:  # noqa: BLE001 - discovery should not kill runner
            errors.append(f"current_large_turnover:{type(exc).__name__}")
    source_counts["current_large_turnover"] = len(set(turnover_symbols))

    if include_current_limitup:
        try:
            for item in adapter.get_limitup_pool():
                symbol = normalize_symbol(getattr(item, "symbol", ""))
                if symbol:
                    limitup_symbols.append(symbol)
                    symbol_metadata[symbol] = _merge_symbol_metadata(
                        symbol_metadata.get(symbol),
                        {
                            "name": str(getattr(item, "name", "") or "unknown"),
                            "theme": str(getattr(item, "theme", "") or "unknown"),
                        },
                    )
        except Exception as exc:  # noqa: BLE001 - discovery should not kill runner
            errors.append(f"current_limitup:{type(exc).__name__}")
    source_counts["current_limitup"] = len(set(limitup_symbols))

    merged = merge_symbols(base, turnover_symbols, limitup_symbols, cap=safe_cap)
    base_set = set(base)
    discovered = [symbol for symbol in merged if symbol not in base_set]
    return RealtimeDiscoveryResult(
        symbols=merged,
        discovered_symbols=discovered,
        source_counts=source_counts,
        symbol_metadata={
            symbol: metadata
            for symbol, metadata in symbol_metadata.items()
            if symbol in set(merged)
        },
        errors=errors,
        notes=[
            "Realtime discovery widens observation coverage using current provider facts.",
            "It does not change strategy scoring, ranking, or buy-point rules.",
        ],
    )
