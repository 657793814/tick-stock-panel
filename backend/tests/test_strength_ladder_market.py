"""港美股强度梯队（api/screener.py::_limit_ladder_market）回归测试。

复现并锁定以下 bug（改造前 HK/US 梯队端点 100% 崩溃或返回错误结果）：
  1. NameError: markets / pl 未在 _limit_ladder_market 内导入；
  2. 单日快照上 close.shift(20/60) 恒为 null，动量档位永远为空；
  3. select 丢弃 _new_high/_new_low 后仍引用，ColumnNotFoundError；
  4. get_market 非法值抛 ValueError，被当作 None 判断导致 500 而非 400；
  5. 共享 enriched 未按市场过滤，A 股标的会混入 HK/US 梯队。
"""
from __future__ import annotations

import types
from datetime import date

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.screener import _limit_ladder_market, router

AS_OF = date(2026, 11, 20)


def _svc_with(rows: list[dict]):
    df = pl.DataFrame(rows)
    svc = types.SimpleNamespace(_load_enriched_for_date=lambda _d: df)
    return svc


def _hk_rows() -> list[dict]:
    """含 A 股与港股的混合快照；港股动量分布在各档位，A 股应被过滤掉。"""
    return [
        # ---- 港股：应进入梯队 ----
        {"symbol": "00700.HK", "name": "腾讯控股", "close": 400.0, "change_pct": 0.05,
         "amount": 1e9, "momentum_20d": 0.30, "vol_ratio_5d": 2.0,
         "high_60d": 390.0, "low_60d": 300.0},   # boards 5, 新高
        {"symbol": "09988.HK", "name": "阿里巴巴", "close": 100.0, "change_pct": 0.03,
         "amount": 5e8, "momentum_20d": 0.10, "vol_ratio_5d": 1.0,
         "high_60d": 120.0, "low_60d": 80.0},    # boards 3
        {"symbol": "03690.HK", "name": "美团", "close": 200.0, "change_pct": 0.01,
         "amount": 3e8, "momentum_20d": 0.02, "vol_ratio_5d": 3.0,
         "high_60d": 260.0, "low_60d": 150.0},   # 动量不足，应被过滤
        # ---- A 股：必须被市场过滤剔除 ----
        {"symbol": "600519.SH", "name": "贵州茅台", "close": 1680.0, "change_pct": 0.06,
         "amount": 2e9, "momentum_20d": 0.40, "vol_ratio_5d": 4.0,
         "high_60d": 1600.0, "low_60d": 1400.0},
    ]


def _repo():
    return types.SimpleNamespace(get_name_map=lambda symbols: {s: s for s in symbols})


def test_strength_ladder_boards_from_momentum_and_market_filter():
    data = _limit_ladder_market(_repo(), _svc_with(_hk_rows()), AS_OF, "hk")

    assert data["market"] == "hk"
    assert data["as_of"] == "2026-11-20"
    # 只有 2 只港股动量 >= 3% 进入梯队；A 股茅台被过滤，美团动量 2% 被过滤
    all_stocks = [s for tier in data["tiers"] for s in tier["stocks"]]
    assert {s["symbol"] for s in all_stocks} == {"00700.HK", "09988.HK"}
    assert not any(s["symbol"].endswith((".SH", ".SZ", ".BJ")) for s in all_stocks)
    boards = {s["symbol"]: s["boards"] for s in all_stocks}
    assert boards == {"00700.HK": 5, "09988.HK": 3}
    # 腾讯 60 日新高 → status=high
    tencent = next(s for s in all_stocks if s["symbol"] == "00700.HK")
    assert tencent["status"] == "high"
    # 新高新低计数基于 high_60d/low_60d 后备口径，不含 A 股
    assert data["counts"]["up"] >= 1


def test_strength_ladder_empty_snapshot_is_safe():
    data = _limit_ladder_market(_repo(), _svc_with([]), AS_OF, "us")
    assert data["tiers"] == []
    assert data["counts"] == {"up": 0, "down": 0}


def test_strength_ladder_missing_indicator_columns_degrades():
    """缺 momentum_20d 等列时不能抛 ColumnNotFoundError，应降级为空梯队。"""
    rows = [{"symbol": "AAPL.US", "close": 100.0, "change_pct": 0.01}]
    data = _limit_ladder_market(_repo(), _svc_with(rows), AS_OF, "us")
    assert data["tiers"] == []


def test_limit_ladder_rejects_invalid_market_with_400():
    app = FastAPI()
    app.include_router(router)
    app.state.repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir="/nonexistent"),
    )
    client = TestClient(app)
    r = client.get("/api/screener/limit-ladder", params={"market": "xx", "as_of": str(AS_OF)})
    assert r.status_code == 400
