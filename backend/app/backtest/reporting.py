"""quantjourney-bt 报告包落地（tick-stock-panel · P1-C3）。

自包含、**仅依赖标准库**（不引入新依赖、不改动现有回测/服务主链路）。
把 ``BacktestResult`` 整理为 quantjourney-bt 风格的标准报告包：

- ``run_metadata.json`` / ``summary.json``：统一 JSON 载荷（run_id、config、stats、
  计数、equity 汇总、per-symbol 指标、trades 摘要）。
- ``metrics.csv``：每标的统计。
- ``equity_curve.csv``：净值曲线。
- ``trades.csv``：交易明细。
- ``summary.txt``：人类可读摘要。
- 可选 ``dashboard.html``：自包含静态仪表盘（无外部依赖）。

PNG 图表（``equity_curve.png`` / ``plots/``）依赖绘图库，按最小改动原则暂不实现，
见 ``docs/quantjourney-bt-capability-extraction.md`` 的 P1-C3。
"""
from __future__ import annotations

import csv
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 运行时不需要 import app.services（其依赖 polars/pandas 环境依赖）
    from app.services.backtest import BacktestResult

_REPORT_VERSION = "1.0"


# ── 安全化 / 取值工具 ────────────────────────────────────────────────────


def _json_safe(value: Any) -> Any:
    """转为 JSON 安全形式：非有限浮点 → None，其余原样保留（list/tuple 递归）。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _require(result: Any, name: str) -> Any:
    """按字段名从结果对象取值（duck-typed）。

    同时支持带属性对象（``BacktestResult``）与 dict 结构视图。
    """
    if isinstance(result, dict):
        return result.get(name)
    return getattr(result, name, None)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value)


def _to_float(value: Any) -> float | None:
    if value is None or value == "" or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── 摘要计算 ──────────────────────────────────────────────────────────────


def _equity_summary(equity_curve: list[dict]) -> dict[str, Any]:
    """净值曲线摘要：点数、极值、末值、均值、累计收益、日期序列。"""
    pts: list[dict] = []
    for p in _as_list(equity_curve):
        if not isinstance(p, dict):
            continue
        pts.append(p)

    values: list[float] = []
    dates: list[Any] = []
    for p in pts:
        v = p.get("value", p.get("equity", p.get("nav")))
        if v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
        dates.append(p.get("date", ""))

    if not values:
        return {"n_points": 0, "min": None, "max": None, "last": None,
                "average": None, "cumulative_return": None, "dates": []}

    return {
        "n_points": len(values),
        "min": min(values),
        "max": max(values),
        "last": values[-1],
        "average": sum(values) / len(values),
        "cumulative_return": (values[-1] / values[0] - 1.0) if values[0] else None,
        "dates": dates,
    }


def _per_symbol_metrics(per_symbol: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in per_symbol:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["total_return"] = _to_float(item.get("total_return"))
        out.append(item)
    return out


def _trades_summary(trades: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        row = dict(t)
        for key in ("pnl_pct", "entry_price", "exit_price"):
            row.setdefault(key, _to_float(row.get(key)))
        out.append(row)
    return out


def build_report_payload(result: Any) -> dict[str, Any]:
    """构造报告包的统一 JSON 载荷（run_metadata.json 与 summary.json 共用）。"""
    trades = _trades_summary(_as_list(_require(result, "trades")))
    per_symbol = _per_symbol_metrics(_as_list(_require(result, "per_symbol_stats")))
    equity = _require(result, "equity_curve")
    return {
        "report_version": _REPORT_VERSION,
        "run_id": _require(result, "run_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": _json_safe(_require(result, "config"))
        if isinstance(_require(result, "config"), dict)
        else {"config": _json_safe(_require(result, "config"))},
        "stats": _json_safe(_require(result, "stats"))
        if isinstance(_require(result, "stats"), dict)
        else {"stats": _json_safe(_require(result, "stats"))},
        "counts": {
            "n_trades": len(trades),
            "n_symbols": len(per_symbol),
            "n_equity_points": _equity_summary(equity)["n_points"],
        },
        "equity_summary": _equity_summary(equity),
        "per_symbol_metrics": per_symbol,
        "trades_summary": trades,
    }


# ── 文件写入 ──────────────────────────────────────────────────────────────


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_csv(path: Path, header: list[str], rows: list[dict[str, Any]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})
    return path


def _union_keys(rows: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    return keys


def _equity_rows(equity_curve: list[dict]) -> list[dict[str, Any]]:
    return [
        {"date": str(p.get("date", "")), "value": p.get("value")}
        for p in _as_list(equity_curve)
        if isinstance(p, dict)
    ]


def _trades_header(trades: list[dict]) -> list[str]:
    return _union_keys([t for t in _as_list(trades) if isinstance(t, dict)])


def _metrics_header(per_symbol: list[dict]) -> list[str]:
    return _union_keys(per_symbol)


# ── 文本与仪表盘 ──────────────────────────────────────────────────────────


def _summary_txt(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    eq = payload.get("equity_summary", {})
    stats = _json_safe(payload.get("stats", {}))
    if not isinstance(stats, dict):
        stats = {}
    run_id = payload.get("run_id")
    lines = [
        f"# {run_id} 回测报告（quantjourney-bt 风格 · P1-C3）",
        f"report_version: {payload.get('report_version')}",
        f"generated_at: {payload.get('generated_at')}",
        "",
        "## 统计",
    ]
    lines += [f"- {k}: {v}" for k, v in stats.items()]
    lines += [
        "",
        "## 计数",
        f"- trades: {counts.get('n_trades')}",
        f"- symbols: {counts.get('n_symbols')}",
        f"- equity points: {counts.get('n_equity_points')}",
        "",
        "## 净值曲线",
        f"- min: {eq.get('min')}",
        f"- max: {eq.get('max')}",
        f"- last: {eq.get('last')}",
        f"- average: {eq.get('average')}",
        f"- cumulative_return: {eq.get('cumulative_return')}",
    ]
    if eq.get("dates"):
        lines.append(f"- equity points: {eq['dates'][-5:]}…（完整见 equity_curve.csv）")
    lines.append("")
    return "\n".join(lines)


_STYLE = (
    "<style>"
    "body{font-family:-apple-system,system-ui,sans-serif;margin:24px;color:#222}"
    "h1{font-size:18px}"
    "table{border-collapse:collapse;margin:8px 0;font-size:13px;max-width:90vw}"
    "th,td{border:1px solid #ccc;padding:4px 8px;text-align:right}"
    "th{background:#f5f5f5}"
    ".k{color:#666;font-size:12px}"
    "</style>"
)


def _dashboard_html(payload: dict[str, Any]) -> str:
    """自包含静态仪表盘：指标 + 净值曲线 + 每标的统计 + 交易摘要（无外部依赖）。"""
    eq = payload.get("equity_summary", {})
    counts = payload.get("counts", {})
    stats = payload.get("stats", {})
    run_id = payload.get("run_id")
    per_symbol = payload.get("per_symbol_metrics", [])
    trades = payload.get("trades_summary", [])

    def table(headers: list[str], rows: list[list[Any]]) -> str:
        body = "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
            for row in rows
        ) or "<tr><td class='k'>（无数据）</td></tr>"
        return (
            "<table><tr>"
            + "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
            + "</tr>"
            + body
            + "</table>"
        )

    stats_safe = _json_safe(stats)
    if not isinstance(stats_safe, dict):
        stats_safe = {}
    symbols = table(["symbol", "total_return"],
                    [[html.escape(str(r.get("symbol", ""))), html.escape(str(r.get("total_return", "")))]
                     for r in per_symbol])
    trades_rows = table(
        _trades_header(_as_list(trades)),
        [[html.escape(str(t.get(k, ""))) for k in _trades_header(_as_list(trades))] for t in trades if isinstance(t, dict)],
    )

    run_id = payload.get("run_id")
    ver = payload.get("report_version")
    gen = payload.get("generated_at", "")
    body = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{html.escape(str(run_id))} 回测报告</title><style>{_STYLE}</style></head>"
        f"<body><h1>回测报告 {html.escape(str(run_id))}</h1>"
        f"<div class='k'>report_version: {html.escape(str(ver))} · generated_at: {html.escape(str(gen))}</div>"
        f"<p>stats: {html.escape(str(stats_safe))}</p>"
        f"<p>counts: {html.escape(str(counts))}</p>"
        f"<p>equity min/max/last/average/cumulative_return: "
        f"{html.escape(str(eq.get('min')))} / {html.escape(str(eq.get('max')))} / "
        f"{html.escape(str(eq.get('last')))} / {html.escape(str(eq.get('average')))} / "
        f"{html.escape(str(eq.get('cumulative_return')))}</p>"
        f"{html.escape(str(symbols))}"
        f"<h2>交易摘要</h2>{html.escape(str(trades_rows))}"
        "</body></html>"
    )
    return body


# ── 对外入口 ──────────────────────────────────────────────────────────────


def write_backtest_report(result: Any, out_dir: str | Path, *, with_dashboard: bool = False) -> dict[str, str]:
    """把 ``BacktestResult`` 写成 quantjourney-bt 风格标准报告包。

    参数:
        result: 含 ``run_id/config/stats/equity_curve/trades/per_symbol_stats`` 的结果对象
                （``app.services.backtest.BacktestResult`` 或其同结构视图）。
        out_dir: 报告输出目录（不存在则创建）。
        with_dashboard: 是否额外生成自包含 ``dashboard.html``。

    返回: 文件路径字典。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    equity_curve = _require(result, "equity_curve")
    trades = _as_list(_require(result, "trades"))
    per_symbol = _as_list(_require(result, "per_symbol_stats"))
    payload = build_report_payload(result)

    paths: dict[str, str] = {}
    paths["run_metadata"] = str(_write_json(out / "run_metadata.json", payload))
    paths["summary"] = str(_write_json(out / "summary.json", payload))
    paths["metrics"] = str(_write_csv(out / "metrics.csv", _metrics_header(per_symbol), per_symbol))
    paths["equity_curve"] = str(_write_csv(out / "equity_curve.csv", ["date", "value"], _equity_rows(equity_curve)))
    paths["trades"] = str(_write_csv(out / "trades.csv", _trades_header(trades), trades))
    paths["summary_txt"] = str(Path(out) / "summary.txt")
    (Path(out) / "summary.txt").write_text(_summary_txt(payload), encoding="utf-8")

    if with_dashboard:
        paths["dashboard"] = str(_write_dashboard(out / "dashboard.html", payload))

    return paths


def _write_dashboard(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(_dashboard_html(payload), encoding="utf-8")
    return path
