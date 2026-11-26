"""市场注册表工具测试: symbol 归属、过滤、缓存键、基准映射。"""
from __future__ import annotations

import polars as pl
import pytest

from app import markets


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", "cn"),
        ("000001.SZ", "cn"),
        ("920344.BJ", "cn"),
        ("00700.HK", "hk"),
        ("AAPL.US", "us"),
        (" 600000.sh ", "cn"),
        ("600000", "cn"),
        ("AAPL", "us"),
    ],
)
def test_market_of_matches_filter(symbol: str, expected: str) -> None:
    df = pl.DataFrame({"symbol": [symbol]})
    assert markets.market_of(symbol) == expected
    assert markets.filter_symbols(df, expected).height == 1


def test_filter_symbols_isolates_each_market() -> None:
    df = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ", "920344.BJ", "00700.HK", "AAPL.US"],
        "value": [1, 2, 3, 4, 5],
    })

    assert markets.filter_symbols(df, "cn")["value"].to_list() == [1, 2, 3]
    assert markets.filter_symbols(df, "hk")["value"].to_list() == [4]
    assert markets.filter_symbols(df, "us")["value"].to_list() == [5]


def test_filter_symbols_safe_on_empty_and_missing_column() -> None:
    empty = pl.DataFrame({"symbol": [], "value": []}, schema={"symbol": pl.Utf8, "value": pl.Int64})
    assert markets.filter_symbols(empty, "cn").is_empty()

    no_symbol = pl.DataFrame({"value": [1, 2]})
    assert markets.filter_symbols(no_symbol, "hk").equals(no_symbol)


def test_filter_symbols_rejects_unknown_market() -> None:
    with pytest.raises(ValueError, match="未知市场"):
        markets.filter_symbols(pl.DataFrame({"symbol": ["AAPL.US"]}), "jp")


def test_symbols_of_returns_market_symbols() -> None:
    df = pl.DataFrame({"symbol": ["600000.SH", "00700.HK", "600000.SH", "AAPL.US"]})
    assert markets.symbols_of(df, "cn") == ["600000.SH"]
    assert markets.symbols_of(df, "hk") == ["00700.HK"]
    assert markets.symbols_of(df, "us") == ["AAPL.US"]


def test_market_cache_key_includes_market() -> None:
    assert markets.market_cache_key("cn", "a", "b") == ("cn", "a", "b")
    assert markets.market_cache_key("hk", "a", "b") != markets.market_cache_key("cn", "a", "b")
    with pytest.raises(ValueError):
        markets.market_cache_key("jp")


def test_benchmark_symbol_mapping() -> None:
    assert markets.benchmark_symbol("cn") == "000300.SH"
    assert markets.benchmark_symbol("hk") == "HSI"
    assert markets.benchmark_symbol("us") == "SPX"
    with pytest.raises(ValueError):
        markets.benchmark_symbol("jp")
