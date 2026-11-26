"""自选/搜索的多市场回归测试.

覆盖三条曾经断掉的链路:
  1. 搜索只在 A 股 instruments 池里查 → 港股「小米」搜不到;
  2. 自选名单不按市场过滤 → 切到港股也能看到 A 股自选, 反之亦然;
  3. 港股自选「加入以来涨跌幅」的基准价只认加入日当天的分区 → 周末加入恒为 null.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.api import kline as kline_api
from app.api import watchlist as wl_api


def _req(repo) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


class _SearchRepo:
    """只实现搜索用到的方法; 访问 A 股维表即报错, 用来验证不会降级串市场。"""

    def __init__(self, hk_df: pl.DataFrame):
        self._hk = hk_df

    def get_instruments_market(self, market: str = "cn"):
        return self._hk if market == "hk" else pl.DataFrame()

    def get_instruments_asset(self, asset_type: str = "stock"):  # pragma: no cover - 不应被调用
        raise AssertionError("非 cn 搜索不得读 A 股维表")

    def get_market_name_map(self, market: str = "cn"):
        return {"01810.HK": "小米集团-W"}


_HK_INSTRUMENTS = pl.DataFrame(
    {
        "symbol": ["01810.HK", "00700.HK"],
        "name": ["小米集团-W", "腾讯控股"],
        "code": ["01810", "00700"],
    }
)


def test_search_hk_returns_hk_instrument_with_market():
    rows = kline_api.search_instruments(
        _req(_SearchRepo(_HK_INSTRUMENTS)), q="小米", limit=5,
        asset_types="stock", market="hk",
    )["results"]
    assert [r["symbol"] for r in rows] == ["01810.HK"]
    assert rows[0]["name"] == "小米集团-W"
    assert rows[0]["market"] == "hk"


def test_search_hk_code_prefix_hits():
    rows = kline_api.search_instruments(
        _req(_SearchRepo(_HK_INSTRUMENTS)), q="00700", limit=5,
        asset_types="stock", market="hk",
    )["results"]
    assert [r["symbol"] for r in rows] == ["00700.HK"]


def test_search_non_cn_without_instruments_returns_empty_not_cn_rows():
    """维表未同步时必须返回空, 不能把 A 股结果当作港股结果返回。"""
    out = kline_api.search_instruments(
        _req(_SearchRepo(pl.DataFrame())), q="茅台", limit=5,
        asset_types="stock", market="us",
    )
    assert out["results"] == []
    assert out["market"] == "us"


def test_watchlist_list_filters_by_market(monkeypatch):
    mixed = [
        {"symbol": "600519.SH", "added_at": "2026-11-20T01:00:00", "group_ids": []},
        {"symbol": "01810.HK", "added_at": "2026-11-21T01:00:00", "group_ids": []},
        {"symbol": "AAPL.US", "added_at": "2026-11-21T02:00:00", "group_ids": []},
    ]
    monkeypatch.setattr(wl_api.watchlist, "list_symbols", lambda: mixed)

    class _Repo(_SearchRepo):
        def get_market_name_map(self, market: str = "cn"):
            return {"01810.HK": "小米集团-W"}

    hk = wl_api.list_all(_req(_Repo(pl.DataFrame())), market="hk")
    assert [r["symbol"] for r in hk["symbols"]] == ["01810.HK"]
    assert hk["symbols"][0]["name"] == "小米集团-W"

    cn = wl_api.list_all(_req(_Repo(pl.DataFrame())), market="cn")
    assert [r["symbol"] for r in cn["symbols"]] == ["600519.SH"]

    everything = wl_api.list_all(_req(_Repo(pl.DataFrame())), market=None)
    assert len(everything["symbols"]) == 3


def test_market_base_closes_falls_back_to_previous_trading_day(tmp_path: Path):
    """周六加入 → 基准价取最近一个交易日的分区, 而不是 null。"""
    part = tmp_path / "kline_daily" / "date=2026-11-20"
    part.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["01810.HK", "600519.SH"],
            "date": [date(2026, 11, 20)] * 2,
            "close": [27.64, 1500.0],
        }
    ).write_parquet(part / "part.parquet")

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    out = wl_api._market_base_closes(
        repo, {"01810.HK": date(2026, 11, 21)}, "hk",
    )
    assert out.to_dicts() == [{"symbol": "01810.HK", "base_close": 27.64}]
