"""港美股补充链路回归测试。

覆盖本轮补齐的三处「A 股口径无处不往」缺口:
  1. /api/watchlist/prepare 对港股按 A 股 asset 路径取数 —— 要么 no_data,
     更糟的是把 HK 标的写进 A 股 kline_daily 的 stock 分区;
  2. 异动 / 涨幅轮动 / 板块轮动没有 market 门禁, 港股市场下静默返回 A 股内容;
  3. 指数同步把 instruments_index_{market}/instruments.parquet（个股池）覆盖成
     几行指数, 一次点击就把市场标的池清空。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import abnormal as abnormal_api
from app.api import rps as rps_api
from app.api import sector_rotation as sector_api
from app.services import daily_bootstrap, index_sync_market


class _FakeRepo:
    """最小仓库桩: 只实现港美股补齐路径会用到的方法。"""

    def __init__(self, inst: pl.DataFrame, local: pl.DataFrame | None = None):
        self._inst = inst
        self._local = local if local is not None else pl.DataFrame()
        self.appended: list[pl.DataFrame] = []
        self.refreshed = 0
        self.views = 0
        self.store = SimpleNamespace(data_dir=None, _register_views=self._reg)

    def _reg(self):
        self.views += 1

    def get_instruments_market(self, market: str) -> pl.DataFrame:
        return self._inst

    def get_daily(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        return self._local

    def append_daily(self, df: pl.DataFrame) -> None:
        self.appended.append(df)

    def refresh_cache(self) -> None:
        self.refreshed += 1

    def resolve_asset_type(self, symbol: str) -> str:
        raise AssertionError(f"A 股口径不应处理港美股标的: {symbol}")


def _inst(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"symbol": symbols, "name": [f"n-{s}" for s in symbols], "type": ["stock"] * len(symbols)})


def _daily(symbol: str) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol, symbol],
        "date": [date(2026, 11, 19), date(2026, 11, 20)],
        "open": [10.0, 10.2], "high": [10.4, 10.5], "low": [9.9, 10.1],
        "close": [10.2, 10.4], "volume": [1e6, 1.2e6], "amount": [0.0, 0.0],
    })


@pytest.fixture()
def patched_market_paths(monkeypatch):
    """把网络与指标计算都替换为可观测的桩。"""
    calls: dict[str, list] = {"fetch": [], "pipeline": []}

    def fake_batch(symbols, **kwargs):
        calls["fetch"].append(list(symbols))
        return _daily(symbols[0])

    def fake_pipeline(**kwargs):
        calls["pipeline"].append(kwargs)
        return 2

    monkeypatch.setattr(daily_bootstrap.kline_sync, "sync_daily_batch", fake_batch)
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", fake_pipeline)
    return calls


def test_prepare_routes_hk_symbol_to_market_path(patched_market_paths):
    repo = _FakeRepo(_inst(["01810.HK"]))
    out = daily_bootstrap.prepare(repo, capset=None, symbols=["01810.HK"], end=date(2026, 11, 21))

    assert out[0]["status"] == "ready"
    assert out[0]["market"] == "hk"
    assert out[0]["source"] == "remote"
    # 走前复权批量接口, 且指标按 hk 口径算
    assert patched_market_paths["fetch"] == [["01810.HK"]]
    assert patched_market_paths["pipeline"][0]["market"] == "hk"
    assert patched_market_paths["pipeline"][0]["symbols"] == ["01810.HK"]
    assert repo.appended and repo.appended[0]["symbol"].to_list() == ["01810.HK", "01810.HK"]
    assert repo.refreshed == 1 and repo.views == 1


def test_prepare_hk_uses_local_without_network(patched_market_paths):
    repo = _FakeRepo(_inst(["01810.HK"]), local=_daily("01810.HK"))
    out = daily_bootstrap.prepare(repo, capset=None, symbols=["01810.HK"], end=date(2026, 11, 21))

    assert out[0] == {"symbol": "01810.HK", "status": "ready", "message": "",
                      "market": "hk", "source": "local"}
    assert patched_market_paths["fetch"] == []
    assert repo.appended == []


def test_prepare_rejects_symbol_outside_market_instruments(patched_market_paths):
    repo = _FakeRepo(_inst(["00700.HK"]))
    out = daily_bootstrap.prepare(repo, capset=None, symbols=["01810.HK"], end=date(2026, 11, 21))

    assert out[0]["status"] == "unsupported"
    assert "01810.HK" in out[0]["message"]
    assert patched_market_paths["fetch"] == []  # 不发出注定失败的请求


def test_prepare_cn_symbol_still_uses_asset_path(monkeypatch):
    """A 股分支不能被市场化改造影响: 仍走 resolve_asset_type。"""
    seen: list[str] = []

    class _CnRepo(_FakeRepo):
        def resolve_asset_type(self, symbol: str) -> str:
            seen.append(symbol)
            return "stock"

    repo = _CnRepo(_inst(["600000.SH"]))
    monkeypatch.setattr(daily_bootstrap, "ensure_history",
                        lambda *a, **k: pl.DataFrame({"symbol": ["600000.SH"]}))
    out = daily_bootstrap.prepare(repo, capset=None, symbols=["600000.SH"], end=date(2026, 11, 21))

    assert seen == ["600000.SH"]
    assert out == [{"symbol": "600000.SH", "status": "ready", "message": "", "market": "cn"}]


@pytest.mark.parametrize("call", [
    lambda: abnormal_api.abnormal_intraday(SimpleNamespace(), limit=10, market="hk"),
    lambda: abnormal_api.abnormal_overview(SimpleNamespace(), market="us"),
    lambda: rps_api.get_rotation(SimpleNamespace(), market="hk"),
    lambda: sector_api.get_sector_rotation(SimpleNamespace(), market="us"),
])
def test_ashare_only_endpoints_fail_closed(call):
    """非 A 股必须明确拒绝, 而不是返回 A 股聚合结果。"""
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 409
    assert "不支持" in str(exc.value.detail)


def test_ashare_only_endpoint_rejects_cross_market_all():
    with pytest.raises(HTTPException) as exc:
        abnormal_api.abnormal_intraday(SimpleNamespace(), limit=10, market="all")
    assert exc.value.status_code == 409


def test_index_sync_does_not_overwrite_stock_instruments(tmp_path):
    """指数维表必须单独落盘: 个股池被覆盖会让市场同步「无标的」。"""
    inst_dir = tmp_path / "instruments_index_hk"
    inst_dir.mkdir(parents=True)
    stock_pool = pl.DataFrame({"symbol": ["00700.HK", "01810.HK"], "name": ["腾讯", "小米"]})
    stock_pool.write_parquet(inst_dir / "instruments.parquet")

    def fake_fetch(code, days):
        return pl.DataFrame({
            "symbol": [code], "date": [date(2026, 11, 20)],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
            "volume": [1.0], "amount": [0.0],
        })

    original = dict(index_sync_market._FETCHERS)
    index_sync_market._FETCHERS["tencent"] = fake_fetch
    try:
        rows = index_sync_market.sync_market_indices("hk", tmp_path)
    finally:
        index_sync_market._FETCHERS.clear()
        index_sync_market._FETCHERS.update(original)

    assert rows > 0
    after = pl.read_parquet(inst_dir / "instruments.parquet")
    assert after["symbol"].to_list() == ["00700.HK", "01810.HK"]
    assert (inst_dir / "indices.parquet").exists()


# ── enriched 差额补齐 ────────────────────────────────────────────────
def _write_part(base, day: str, table: str, symbols: list[str]) -> None:
    part = base / table / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols, "date": [date.fromisoformat(day)] * len(symbols),
                  "close": [1.0] * len(symbols)}).write_parquet(part / "part.parquet")


def test_find_missing_only_returns_symbols_without_enriched(tmp_path):
    from app.services import enriched_backfill

    _write_part(tmp_path, "2026-11-20", "kline_daily", ["00700.HK", "01810.HK", "600000.SH"])
    _write_part(tmp_path, "2026-11-20", "kline_daily_enriched", ["00700.HK", "600000.SH"])

    repo = _FakeRepo(pl.DataFrame())
    repo.store = SimpleNamespace(data_dir=tmp_path, _register_views=lambda: None)
    assert enriched_backfill.find_missing(repo, "hk") == ["01810.HK"]
    assert enriched_backfill.find_missing(repo, "cn") == []


def test_backfill_runs_pipeline_only_for_missing(tmp_path, monkeypatch):
    from app.services import enriched_backfill

    _write_part(tmp_path, "2026-11-20", "kline_daily", ["00700.HK", "01810.HK", "09988.HK"])
    _write_part(tmp_path, "2026-11-20", "kline_daily_enriched", ["00700.HK"])

    seen: dict[str, object] = {}

    def fake_pipeline(**kwargs):
        seen.update(kwargs)
        return 42

    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", fake_pipeline)
    repo = _FakeRepo(pl.DataFrame())
    repo.store = SimpleNamespace(data_dir=tmp_path, _register_views=lambda: None)

    out = enriched_backfill.backfill(repo, "hk", limit=0)
    assert out["candidates"] == 2
    assert out["computed"] == 2 and out["rows"] == 42 and out["remaining"] == 0
    assert seen["market"] == "hk"
    assert seen["symbols"] == ["01810.HK", "09988.HK"]
    assert repo.refreshed == 1

    # limit 只跑差额的一部分, 剩余量如实回报 (美股 9 千余只靠重复调用推进)
    seen.clear()
    out = enriched_backfill.backfill(repo, "hk", limit=1)
    assert seen["symbols"] == ["01810.HK"]
    assert out["computed"] == 1 and out["remaining"] == 1


# ── enriched 列类型漂移 ──────────────────────────────────────────────
def test_compute_all_market_rows_match_storage_schema():
    """港美股行必须按存储 schema 出列: 连板数宽度不一致会让整张表的扫描失败。"""
    from app.indicators.pipeline import compute_all
    from app.parquet import ENRICHED_STORAGE_SCHEMA

    raw = pl.DataFrame({
        "symbol": ["01810.HK"] * 5,
        "date": [date(2026, 11, 16 + i) for i in range(5)],
        "open": [10.0, 10.1, 10.2, 10.3, 10.4],
        "high": [10.5, 10.6, 10.7, 10.8, 10.9],
        "low": [9.8, 9.9, 10.0, 10.1, 10.2],
        "close": [10.2, 10.3, 10.4, 10.5, 10.6],
        "volume": [1e6] * 5,
        "amount": [0.0] * 5,
    })
    out = compute_all(raw, market="hk")
    for col in ("consecutive_limit_ups", "consecutive_limit_downs"):
        # 声明的是宽整型 Int64; 写入侧也必须用它, 否则跨市场共表时按 schema 扫描会失败
        assert ENRICHED_STORAGE_SCHEMA[col] == pl.Int64
        assert out[col].dtype == pl.Int64
        assert out[col].to_list() == [0] * 5


def test_repair_enriched_dtypes_casts_drifted_partitions(tmp_path):
    from app.services import market_schema_repair

    part = tmp_path / "kline_daily_enriched" / "date=2026-11-20"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "01810.HK"],
        "date": [date(2026, 11, 20)] * 2,
        "consecutive_limit_ups": pl.Series([2, 0], dtype=pl.Int32),
        "consecutive_limit_downs": pl.Series([0, 0], dtype=pl.Int32),
    }).write_parquet(part / "part.parquet")

    written: list[pl.DataFrame] = []

    def _atomic_write(df, out):
        written.append(df)
        df.write_parquet(out)

    repo = _FakeRepo(pl.DataFrame())
    repo.store = SimpleNamespace(data_dir=tmp_path, _register_views=lambda: None)
    repo._atomic_write_parquet = _atomic_write

    out = market_schema_repair.repair_enriched_dtypes(repo)
    assert out == {"partitions": 1, "repaired": 1, "failed": 0}
    fixed = written[0]
    assert fixed["consecutive_limit_ups"].dtype == pl.Int64
    assert fixed["consecutive_limit_downs"].dtype == pl.Int64
    assert fixed["consecutive_limit_ups"].to_list() == [2, 0]

    # 幂等: 已对齐的分区不再重写
    out = market_schema_repair.repair_enriched_dtypes(repo)
    assert out["repaired"] == 0 and len(written) == 1
