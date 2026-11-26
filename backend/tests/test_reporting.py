"""P1-C3：quantjourney-bt 风格标准报告包 —— 单元测试（仅标准库）。

覆盖：JSON 载荷构建（含 inf/nan 安全化、字段缺失兜底）、CSV 写入（表头/行）、
净值/交易/每标的摘要计算、以及整包落盘（含 dashboard.html）后的文件与内容校验。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backtest.reporting import (
    build_report_payload,
    _dashboard_html,
    _summary_txt,
    _trades_header,
    write_backtest_report,
)


def _make_result() -> dict:
    """构造与 BacktestResult 同结构的测试结果对象（不依赖 polars/pandas）。"""
    return {
        "run_id": "run-2026-11-23",
        "config": {"symbols": ["A", "B"], "start": "2026-01-01", "end": "2026-12-31"},
        "stats": {
            "sharpe": 1.5,
            "total_return": 0.25,
            "profit_factor": float("inf"),  # 验证非有限浮点安全化
        },
        "equity_curve": [
            {"date": "2026-01-01", "value": 1000000},
            {"date": "2026-02-01", "value": 1200000},
            {"date": "2026-12-31", "value": 1400000},
        ],
        "trades": [
            {"symbol": "A", "entry_date": "2026-01-01", "exit_date": "2026-06-01",
             "pnl_pct": 0.12, "entry_price": 10, "exit_price": 11},
            {"symbol": "B", "entry_date": "2026-02-01", "exit_date": "2026-07-01",
             "pnl_pct": -0.05},
        ],
        "per_symbol_stats": [
            {"symbol": "A", "total_return": 0.18},
            {"symbol": "B", "total_return": 0.22},
        ],
    }


def test_build_payload_counts_and_equity_summary():
    payload = build_report_payload(_make_result())
    assert payload["run_id"] == "run-2026-11-23"
    assert payload["counts"]["n_trades"] == 2
    assert payload["counts"]["n_symbols"] == 2
    assert payload["counts"]["n_equity_points"] == 3
    eq = payload["equity_summary"]
    assert eq["min"] == 1000000
    assert eq["max"] == 1400000
    assert eq["last"] == 1400000
    assert eq["average"] == pytest.approx((1000000 + 1200000 + 1400000) / 3)
    assert eq["cumulative_return"] == pytest.approx(0.4)
    # profit_factor = inf 应被安全化为 None
    assert payload["stats"]["profit_factor"] is None
    assert "sharpe" in payload["stats"]


def test_payload_handles_missing_fields():
    result = _make_result()
    del result["trades"]
    del result["per_symbol_stats"]
    payload = build_report_payload(result)
    assert payload["counts"]["n_trades"] == 0
    assert payload["counts"]["n_symbols"] == 0
    assert payload["trades_summary"] == []
    assert payload["per_symbol_metrics"] == []


def test_trades_header():
    trades = [
        {"symbol": "A", "pnl_pct": 0.1},
        {"symbol": "B", "pnl_pct": -0.05},
    ]
    keys = _trades_header(trades)
    assert keys == ["symbol", "pnl_pct"]


def test_summary_txt_nonempty_and_has_sections():
    payload = build_report_payload(_make_result())
    txt = _summary_txt(payload)
    assert "run-2026-11-23" in txt
    assert "## 统计" in txt
    assert "## 净值曲线" in txt
    assert "1000000" in txt


def test_write_report_creates_all_files(tmp_path: Path):
    paths = write_backtest_report(_make_result(), tmp_path)
    for key in ("run_metadata", "summary", "metrics", "equity_curve", "trades", "summary_txt"):
        assert key in paths
    for name in ("run_metadata.json", "summary.json", "metrics.csv", "equity_curve.csv",
                 "trades.csv", "summary.txt"):
        assert (tmp_path / name).exists(), name
    # JSON 可解析且关键字段齐全
    meta = json.loads((tmp_path / "run_metadata.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "run-2026-11-23"
    assert meta["counts"]["n_trades"] == 2
    # equity_curve.csv 含 date/value 两列
    csv_text = (tmp_path / "equity_curve.csv").read_text(encoding="utf-8")
    assert "date" in csv_text and "value" in csv_text
    assert "2026-02-01" in csv_text


def test_write_report_with_dashboard(tmp_path: Path):
    paths = write_backtest_report(_make_result(), tmp_path, with_dashboard=True)
    assert paths["dashboard"] and (tmp_path / "dashboard.html").exists()
    html_text = (tmp_path / "dashboard.html").read_text(encoding="utf-8")
    assert html_text.startswith("<!doctype html>")
    assert "run-2026-11-23" in html_text
    assert "A" in html_text  # 每标的 symbol 出现在仪表盘


def test_dashboard_html_marks_no_trades():
    payload = build_report_payload(_make_result())
    payload["per_symbol_metrics"] = []
    payload["trades_summary"] = []
    html_text = _dashboard_html(payload)
    assert "（无数据）" in html_text or "（无交易）" in html_text


def test_module_imports_without_polars_pandas():
    # 模块仅标准库导入，可在 polars/pandas 缺失的环境导入
    import app.backtest.reporting as r

    assert r._REPORT_VERSION == "1.0"
    assert hasattr(r, "write_backtest_report")
    assert hasattr(r, "build_report_payload")
