"""HK/US 聚合能力测试（方案 §5.4/§5.5/§5.7）：市场总览、Regime、复盘门控。

通过替换 ScreenerService 的加载方法注入混合市场面板，验证：
- 港美股总览用 60日新高/新低替代涨跌停，概念/行业为空，A 股数据不混入；
- Regime 4 维评分输出与 A 股 RegimeRow 同构且带 market；
- 复盘 API 对 HK/US 返回明确门控，不输出 A 股叙事。
"""
from __future__ import annotations

import json
import types
from datetime import date

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.market_recap import router as recap_router
from app.services import market_overview_builder, regime_builder
from app.services.screener import ScreenerService

AS_OF = date(2026, 11, 20)
SUFFIX = {None: ".HK", "hk": ".HK", "us": ".US"}


def _panel(suffix: str) -> pl.DataFrame:
    n = 6
    return pl.DataFrame({
        "symbol": [f"0070{i}{suffix}" for i in range(n)],
        "name": [f"股票{i}" for i in range(n)],
        "date": [AS_OF] * n,
        "close": [100.0 + i for i in range(n)],
        "change_pct": [0.05, 0.03, 0.01, -0.02, -0.04, 0.0],
        "volume": [1000.0] * n,
        "amount": [1e6] * n,
        "turnover_rate": [0.02] * n,
        "vol_ratio_5d": [2.0, 1.6, 1.0, 0.8, 1.8, 1.0],
        "ma5": [99.0] * n,
        "ma20": [97.0] * n,
        "ma60": [95.0] * n,
        "high_60d": [101.0] * n,
        "low_60d": [90.0] * n,
        "momentum_20d": [0.30, 0.20, 0.10, -0.05, 0.0, 0.04],
        "signal_limit_up": [False] * n,
        "signal_limit_down": [False] * n,
    })


def _inject(monkeypatch, panel: pl.DataFrame):
    """让 ScreenerService 的当日加载按实例 market 返回对应后缀的行。"""
    def fake_load(self, d):
        suffix = ".HK" if self.market == "hk" else ".US" if self.market == "us" else ""
        if suffix:
            return panel.filter(pl.col("symbol").str.ends_with(suffix))
        return panel
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", fake_load)


def test_overview_hk_uses_new_high_and_excludes_cn(monkeypatch):
    mixed = pl.concat([_panel(".HK"), _panel(".SH")], how="diagonal_relaxed")
    _inject(monkeypatch, mixed)
    repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir="/nonexistent"))

    ov = market_overview_builder.build_market_overview_market(repo, "hk", as_of=AS_OF)

    assert ov["market"] == "hk"
    assert ov["breadth"]["total"] == 6           # 只统计港股，A 股被隔离
    assert ov["concept_rank"] == {"leading": [], "lagging": []}
    assert ov["industry_rank"] == {"leading": [], "lagging": []}
    assert ov["amount"] == {"total": 0, "avg": 0}
    assert ov["limit"]["limit_up"] >= 1          # 60日新高替代涨停数
    assert all(s["symbol"].endswith(".HK") for s in ov["top_gainers"] + ov["top_losers"])


def test_overview_market_invalid_rejected():
    repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir="/nonexistent"))
    with pytest.raises(ValueError):
        market_overview_builder.build_market_overview_market(repo, "xx", as_of=AS_OF)


def test_regime_subscores_market_uses_momentum():
    """港美股 4 维评分：momentum 用新高口径，不依赖涨停列，输出 score 0~100。"""
    metrics = {
        "up_pct": 60, "down_pct": 30, "avg_pct": 0.01, "median_pct": 0.005,
        "strong_up_pct": 10, "strong_down_pct": 4, "strong_diff_pct": 6,
        "new_high": 20, "new_low": 5, "new_high_pct": 15, "new_high_net": 15,
        "above_ma20_pct": 0.6,
    }
    sub = regime_builder._compute_subscores_market(metrics)
    assert {"profit", "momentum", "resilience", "trend", "score"} <= set(sub)
    assert 0 <= sub["score"] <= 100
    assert 0 <= sub["momentum"] <= 100


def test_regime_market_persistence_roundtrip(tmp_path):
    rows = pl.DataFrame({
        "date": [AS_OF], "state": ["strong"], "score": [72],
        "limit_up": [20], "limit_down": [5], "max_consecutive": [4],
        "market": ["hk"], "profit_score": [70], "speculation_score": [75],
        "resilience_score": [80], "trend_score": [65],
    })
    regime_builder.save_regime_market(tmp_path, "hk", rows)
    got = regime_builder.load_regime_market_history(tmp_path, "hk")
    assert not got.is_empty() and got["market"][0] == "hk"
    cov = regime_builder.get_regime_market_coverage(tmp_path, "hk")
    assert cov["rows"] == 1
    assert regime_builder.load_regime_market_history(tmp_path, "us").is_empty()


def test_recap_api_gates_hk_us():
    app = FastAPI()
    app.include_router(recap_router)
    app.state.repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir="/nonexistent"))
    client = TestClient(app)

    for market in ("hk", "us"):
        r = client.post("/api/market-recap/analyze", json={"market": market})
        assert r.status_code == 200
        chunks = [json.loads(line) for line in r.text.strip().splitlines()]
        gate = next(c for c in chunks if c.get("code") == "market_not_supported")
        assert market.upper() in gate["message"]
        assert "不适用" in gate["message"]
