"""东方财富日频事件因子测试: 归一化口径 / T+1 生效 / 未上榜填 0 / 双路径一致。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.fundamentals import (
    FUNDAMENTAL_FACTOR_NAMES,
    attach_fundamental_factors,
    build_fundamental_matrices,
    fundamental_tables,
    load_fundamental_snapshots,
)
from app.backtest.matrix import build_market_data_matrix
from app.factors.registry import get_factor
from app.plugins.eastmoney import client as em
from app.services import eastmoney_sync as es


def _lhb_rows() -> list[dict]:
    def row(secucode: str, sec_type: str, net: float, amount: float, reason: str) -> dict:
        return {
            "SECUCODE": secucode,
            "SECURITY_TYPE_CODE": sec_type,
            "BILLBOARD_NET_AMT": net,
            "BILLBOARD_BUY_AMT": net * 2,
            "BILLBOARD_SELL_AMT": net,
            "ACCUM_AMOUNT": amount,
            "DEAL_NET_RATIO": net / amount * 100 if amount else None,
            "EXPLANATION": reason,
        }

    return [
        # 同一标的两条上榜原因: 取绝对值最大的一条, 原因为 2
        row("300324.SZ", "058001001", 2.8e8, 1e10, "连续三日涨幅偏离 20%"),
        row("300324.SZ", "058001001", -1.05e7, 5e9, "日涨幅 15% 前5"),
        row("600519.SH", "058001001", -3.0e7, 2e9, "日跌幅 7% 前5"),
        # 非 A 股: 可转债 / B 股必须被过滤
        row("123255.SZ", "060", 7.2e7, 2e9, "可转债"),
        row("200488.SZ", "058001002", 1.0e6, 1e7, "B 股"),
    ]


def _margin_rows() -> list[dict]:
    return [
        {
            "SECUCODE": "000001.SZ", "RZYE": 3.8e9, "RQYE": 2.4e8, "RZMRE": 1.4e8,
            "RZJME": -1.6e7, "RZJME5D": -1.8e8, "SZ": 2.4e11, "RZYEZB": 1.57,
        },
        {
            "SECUCODE": "600519.SH", "RZYE": 1.0e10, "RQYE": 0.0, "RZMRE": 5.0e7,
            "RZJME": 1.0e7, "RZJME5D": 3.0e7, "SZ": 2.0e12, "RZYEZB": 0.5,
        },
        # 缺流通市值 → 比例不可算, 必须为 None 而不是 0
        {
            "SECUCODE": "600000.SH", "RZYE": 1.0e9, "RQYE": 0.0, "RZMRE": 1.0e7,
            "RZJME": 1.0e6, "RZJME5D": 2.0e6, "SZ": None, "RZYEZB": None,
        },
        # ETF / 可转债 被过滤
        {"SECUCODE": "561980.SH", "RZYE": 1.0e8, "SZ": 1e9, "RZYEZB": 10.0},
        {"SECUCODE": "113677.SH", "RZYE": 1.0e7, "SZ": 1e9, "RZYEZB": 1.0},
    ]


def test_symbol_normalization_filters_non_equity():
    assert em.normalize_secucode("600519.SH") == "600519.SH"
    assert em.normalize_secucode("000001.sz") == "000001.SZ"
    assert em.normalize_secucode("830799.BJ") == "830799.BJ"
    assert em.normalize_secucode("123255.SZ") is None  # 可转债
    assert em.normalize_secucode("561980.SH") is None  # ETF
    assert em.normalize_secucode("200488.SZ") is None  # B 股
    assert em.normalize_secucode("600519.HK") is None


def test_parse_lhb_dedupes_by_abs_net_and_keeps_reason_count():
    frame = em.parse_lhb(_lhb_rows(), date(2026, 3, 2))
    assert set(frame["symbol"]) == {"300324.SZ", "600519.SH"}
    assert frame.height == 2

    target = frame.filter(pl.col("symbol") == "300324.SZ").row(0, named=True)
    assert target["lhb_net_amount"] == pytest.approx(2.8e8)  # 绝对值更大的一条
    assert target["lhb_reason_count"] == 2
    assert target["lhb_on_board"] == 1
    assert target["announce_date"] == date(2026, 3, 2)
    assert target["lhb_net_ratio"] == pytest.approx(2.8)


def test_parse_margin_ratios_use_float_mv():
    frame = em.parse_margin(_margin_rows(), date(2026, 3, 2))
    assert set(frame["symbol"]) == {"000001.SZ", "600519.SH", "600000.SH"}

    pingan = frame.filter(pl.col("symbol") == "000001.SZ").row(0, named=True)
    assert pingan["margin_balance_ratio"] == pytest.approx(1.57)  # 上游 RZYEZB
    assert pingan["margin_buy_ratio"] == pytest.approx(1.4e8 / 2.4e11 * 100)
    assert pingan["margin_net_5d_ratio"] == pytest.approx(-1.8e8 / 2.4e11 * 100)
    assert pingan["margin_short_ratio"] == pytest.approx(2.4e8 / 2.4e11 * 100)

    no_mv = frame.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert no_mv["margin_balance_ratio"] is None
    assert no_mv["margin_buy_ratio"] is None


def _daily_panel(start: date, days: int, symbols: tuple[str, ...]) -> pl.DataFrame:
    rows = []
    for offset in range(days):
        for symbol in symbols:
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=offset),
                "open": 10.0, "high": 10.5, "low": 9.5,
                "close": 10.0 + offset * 0.1, "volume": 1000.0,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _event_snapshot(rows: list[dict], columns: list[str]) -> pl.DataFrame:
    frame = pl.DataFrame(rows)
    return (
        frame.with_columns(
            pl.col("announce_date").str.slice(0, 10).str.to_date().alias("_announce")
        )
        .sort(["symbol", "_announce"])
    )


def test_daily_attach_shifts_one_trading_day_and_fills_zero():
    panel = _daily_panel(date(2026, 3, 2), 5, ("300324.SZ",))
    snapshot = {
        "lhb_metrics": _event_snapshot([
            {"symbol": "300324.SZ", "announce_date": "2026-03-03",
             "lhb_net_ratio": 2.5, "lhb_on_board": 1, "lhb_reason_count": 2},
        ], ["lhb_net_ratio", "lhb_on_board", "lhb_reason_count"]),
    }
    out = attach_fundamental_factors(panel, snapshot, ["lhb_on_board", "lhb_net_ratio"])
    by_date = {row["date"]: row for row in out.iter_rows(named=True)}

    # 数据覆盖起始日之前: null (不得凭空宣称"未上榜")
    assert by_date[date(2026, 3, 2)]["lhb_on_board"] is None
    # 披露当日仍不可用 (盘后发布)
    assert by_date[date(2026, 3, 3)]["lhb_on_board"] == 0.0
    # 次一交易日生效
    assert by_date[date(2026, 3, 4)]["lhb_on_board"] == 1.0
    assert by_date[date(2026, 3, 4)]["lhb_net_ratio"] == pytest.approx(2.5)
    # 事件因子不前向填充: 第三日起回到 0
    assert by_date[date(2026, 3, 5)]["lhb_on_board"] == 0.0


def test_daily_attach_leaves_null_when_factor_has_no_fill():
    """两融未覆盖的标的/日期保持 null, 不当作 0 杠杆。"""
    panel = _daily_panel(date(2026, 3, 2), 4, ("600519.SH",))
    snapshot = {
        "margin_metrics": _event_snapshot([
            {"symbol": "600519.SH", "announce_date": "2026-03-03",
             "margin_balance_ratio": 0.5, "margin_buy_ratio": 0.02,
             "margin_net_5d_ratio": 0.01, "margin_short_ratio": 0.001},
        ], ["margin_balance_ratio", "margin_buy_ratio",
            "margin_net_5d_ratio", "margin_short_ratio"]),
    }
    out = attach_fundamental_factors(panel, snapshot, ["margin_balance_ratio"])
    values = out.sort("date")["margin_balance_ratio"].to_list()
    assert values[0] is None  # 3-2 未覆盖
    assert values[1] is None  # 3-3 当日不可用
    assert values[2] == pytest.approx(0.5)
    assert values[3] is None  # 3-4 无当日数据 → 不前向填充


def test_matrix_path_matches_polars_for_daily_tables():
    panel = _daily_panel(date(2026, 3, 2), 6, ("300324.SZ", "600519.SH"))
    snapshot = {
        "lhb_metrics": _event_snapshot([
            {"symbol": "300324.SZ", "announce_date": "2026-03-04",
             "lhb_net_ratio": 2.5, "lhb_on_board": 1, "lhb_reason_count": 1},
            {"symbol": "600519.SH", "announce_date": "2026-03-05",
             "lhb_net_ratio": -1.5, "lhb_on_board": 1, "lhb_reason_count": 2},
        ], ["lhb_net_ratio", "lhb_on_board", "lhb_reason_count"]),
    }
    attached = attach_fundamental_factors(
        panel, snapshot, ["lhb_on_board", "lhb_net_ratio"],
    ).sort(["symbol", "date"])
    market = build_market_data_matrix(panel)
    matrices = build_fundamental_matrices(
        market, snapshot, ["lhb_on_board", "lhb_net_ratio"],
    )
    symbols = {symbol: index for index, symbol in enumerate(market.symbols)}
    dates = sorted({row["date"] for row in panel.iter_rows(named=True)})

    for name in ("lhb_on_board", "lhb_net_ratio"):
        for row in attached.iter_rows(named=True):
            expected = row[name]
            actual = matrices[name][dates.index(row["date"]), symbols[row["symbol"]]]
            if expected is None:
                assert np.isnan(actual), (name, row["date"], row["symbol"], actual)
            else:
                assert actual == pytest.approx(expected, rel=1e-6)


def test_snapshot_loader_keeps_daily_tables(tmp_path: Path):
    table_dir = tmp_path / "financials" / "lhb_metrics"
    table_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["300324.SZ", "300324.SZ"],
        "announce_date": ["2026-03-03", "2026-03-04"],
        "lhb_net_ratio": [1.0, 2.0],
        "lhb_on_board": [1, 1],
        "lhb_reason_count": [1, 1],
    }).write_parquet(table_dir / "part.parquet")

    snapshots = load_fundamental_snapshots(tmp_path, {"lhb_metrics", "margin_metrics"})
    assert set(snapshots) == {"lhb_metrics"}
    assert snapshots["lhb_metrics"].height == 2


def test_daily_merge_is_idempotent_and_keeps_other_dates():
    existing = em.parse_lhb(_lhb_rows(), date(2026, 3, 2))
    fetched = em.parse_lhb(_lhb_rows()[:1], date(2026, 3, 3))
    merged = es._merge_daily(existing, fetched)
    assert merged.height == 3
    again = es._merge_daily(merged, fetched)
    assert again.height == 3  # 重复同步不膨胀
    assert set(merged["announce_date"]) == {date(2026, 3, 2), date(2026, 3, 3)}


def test_factor_registry_exposes_event_groups():
    assert {"lhb_net_ratio", "lhb_on_board", "margin_balance_ratio"} <= FUNDAMENTAL_FACTOR_NAMES
    assert get_factor("lhb_net_ratio").group == "龙虎榜"
    assert get_factor("margin_net_5d_ratio").group == "融资融券"
    assert get_factor("lhb_on_board").pit is True
    assert "lhb_metrics" in fundamental_tables()
    assert "margin_metrics" in fundamental_tables()


def test_unknown_table_rejected_by_sync():
    with pytest.raises(ValueError):
        es.sync_table(Path("."), "not_a_table")
