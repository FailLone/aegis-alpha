from __future__ import annotations

from types import SimpleNamespace

from aegis_alpha.realtime_discovery import discover_realtime_symbols, merge_symbols


def test_merge_symbols_normalizes_dedupes_and_caps() -> None:
    assert merge_symbols(["600000.SH", "000001"], ["600000", "300001"], cap=3) == [
        "600000",
        "000001",
        "300001",
    ]


def test_discover_realtime_symbols_merges_current_provider_facts() -> None:
    class FakeAdapter:
        def _query(self, query: str, sort_key: str = "") -> dict:
            return {
                "data": {
                    "fields": ["股票代码", "股票简称", "成交额"],
                    "list": [
                        ["600519", "贵州茅台", 10_000_000_000],
                        ["300001", "特锐德", 5_000_000_000],
                    ],
                }
            }

        def get_limitup_pool(self) -> list:
            return [
                SimpleNamespace(symbol="002281"),
                SimpleNamespace(symbol="600519"),
            ]

    result = discover_realtime_symbols(
        FakeAdapter(),
        base_symbols=["000001", "600519.SH"],
        max_symbols=5,
        seed_turnover_yi=30,
    )

    assert result.symbols == ["000001", "600519", "300001", "002281"]
    assert result.discovered_symbols == ["300001", "002281"]
    assert result.source_counts["base"] == 2
    assert result.source_counts["current_large_turnover"] == 2
    assert result.source_counts["current_limitup"] == 2
    assert result.symbol_metadata["600519"] == {"name": "贵州茅台", "theme": "unknown"}
    assert result.symbol_metadata["002281"] == {"name": "unknown", "theme": "unknown"}
    assert result.errors == []
