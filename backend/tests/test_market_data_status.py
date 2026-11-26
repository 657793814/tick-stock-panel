"""市场数据画像 + 港美股同步入口的回归测试。

覆盖本次修复的三处「从未被调用 / 未接线」问题:
  1. instrument_sync.sync_market_instruments 里的客户端未初始化 (NameError 被
     自身 except 吞掉, 恒返回 0 只标的);
  2. repository.get_instruments_market 使用未导入的 MARKET_CN;
  3. market_data_status 读取维表路径与写入路径不一致。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.markets import MARKET_CN, MARKET_HK
from app.services import instrument_sync
from app.services.market_data_status import market_data_status


def _write_daily(base, day: str, symbols: list[str]) -> None:
    part = base / "kline_daily" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": symbols,
        "date": [date.fromisoformat(day)] * len(symbols),
        "close": [10.0] * len(symbols),
    }).write_parquet(part / "part.parquet")


def test_market_data_status_splits_shared_storage(tmp_path):
    _write_daily(tmp_path, "2026-11-19", ["600000.SH", "000001.SZ"])
    _write_daily(tmp_path, "2026-11-20", ["600000.SH", "000001.SZ", "00700.HK"])

    cn = market_data_status(tmp_path, MARKET_CN)
    assert cn["symbols"] == 2
    assert cn["latest_date"] == "2026-11-20"
    assert cn["has_data"] is True

    hk = market_data_status(tmp_path, MARKET_HK)
    assert hk["symbols"] == 1
    assert hk["latest_date"] == "2026-11-20"
    assert hk["instruments"] == 0


def test_market_data_status_reads_market_instruments_path(tmp_path):
    """维表写 instruments_index_hk/instruments.parquet, 画象必须读同一路径。"""
    inst = tmp_path / "instruments_index_hk" / "instruments.parquet"
    inst.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["00700.HK", "09988.HK"]}).write_parquet(inst)

    hk = market_data_status(tmp_path, MARKET_HK)
    assert hk["instruments"] == 2
    assert hk["has_data"] is False  # 有维表但还没日K


def test_market_data_status_rejects_unknown_market(tmp_path):
    with pytest.raises(ValueError):
        market_data_status(tmp_path, "jp")


def test_sync_market_instruments_uses_initialized_client(tmp_path, monkeypatch):
    """客户端必须在函数内取到; 否则异常被吞成 0 行且无任何日志。"""
    calls: list[str] = []

    class _Exchanges:
        def get_instruments(self, ex, instrument_type="stock"):  # noqa: ARG002
            calls.append(ex)
            return [{
                "symbol": "00700.HK", "name": "腾讯控股", "code": "00700",
                "exchange": "HK", "region": "HK", "type": "stock",
                "ext": {"float_shares": 9.09e9},
            }]

    class _Client:
        exchanges = _Exchanges()

    monkeypatch.setattr(instrument_sync, "get_client", lambda: _Client())

    rows = instrument_sync.sync_market_instruments(tmp_path, MARKET_HK)
    assert calls == ["HK"]
    assert rows == 1
    out = tmp_path / "instruments_index_hk" / "instruments.parquet"
    assert out.exists()
    assert pl.read_parquet(out)["symbol"].to_list() == ["00700.HK"]
