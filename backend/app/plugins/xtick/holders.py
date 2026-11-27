"""XTick 股东数据 → 股东结构因子宽表 (纯函数, 无网络)。

输入为上游三个接口的原始行 (字段已实测确认):
  - /doc/topholder     十大股东:    reportDate, publicDate, name, num, ratio, reason, rank
  - /doc/topflowholder 十大流通股东: 同上
  - /doc/holdernum     股东户数:    reportDate, publicDate, holder, holderA, holderFlow

输出按 (symbol, reportDate) 一行的数值快照, 供
``services/holder_sync.py`` 落盘到 ``data/financials/holder_metrics/part.parquet``,
再经 ``backtest/fundamentals.py`` 以公告日点时 (PIT) 门控并入日频面板成为因子。

口径 (CONTRIBUTING §3):
- ``ratio`` 上游即百分数 (54.07 表示 54.07%), 直接透传, 不再 /100;
- ``hhi`` 用百分数份额平方和 /10000, 落在 0~1, 与份额单位无关;
- 股东户数环比按「相邻已披露报告期」计算, 缺上一期时为 null (不填 0);
- 任一来源缺失时对应列为 null, 绝不填 0 —— 填 0 会在截面排名里被当成
  「无集中度 / 无变化」而污染分组。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _period(row: dict) -> str | None:
    text = str(row.get("reportDate") or "")[:10]
    return text or None


def _announce(row: dict) -> str | None:
    text = str(row.get("publicDate") or "")[:10]
    return text or None


def _iter_rows(rows: Any) -> Iterable[dict]:
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            yield row


def aggregate_top_holders(rows: Any, *, prefix: str) -> dict[str, dict[str, float | None]]:
    """按报告期聚合十大股东 (或十大流通股东)。

    prefix="top10" → top10_ratio / top10_hhi / top10_new_count ...
    prefix="flow"  → flow_ratio / flow_hhi / flow_new_count ...
    """
    by_period: dict[str, list[dict]] = {}
    for row in _iter_rows(rows):
        period = _period(row)
        if period is None:
            continue
        by_period.setdefault(period, []).append(row)

    result: dict[str, dict[str, float | None]] = {}
    for period, items in by_period.items():
        ratios = [_f(item.get("ratio")) for item in items]
        valid = [value for value in ratios if value is not None]
        total = sum(valid) if valid else None
        top1 = None
        for item in items:
            if str(item.get("rank") or "") == "1":
                top1 = _f(item.get("ratio"))
                break
        if top1 is None and items:
            ranked = sorted(
                (item for item in items if _f(item.get("ratio")) is not None),
                key=lambda item: _f(item.get("ratio")) or 0.0,
                reverse=True,
            )
            top1 = _f(ranked[0].get("ratio")) if ranked else None
        hhi = (
            sum((value / 100.0) ** 2 for value in valid) if valid else None
        )
        reasons = [str(item.get("reason") or "") for item in items]
        result[period] = {
            f"{prefix}_ratio": round(total, 4) if total is not None else None,
            f"{prefix}_top1_ratio": top1,
            f"{prefix}_hhi": round(hhi, 6) if hhi is not None else None,
            f"{prefix}_new_count": float(reasons.count("新进")),
            f"{prefix}_increase_count": float(reasons.count("增持")),
            f"{prefix}_decrease_count": float(reasons.count("减持")),
        }
    return result


def aggregate_holder_num(rows: Any) -> dict[str, dict[str, float | None]]:
    """按报告期取股东户数, 并计算相邻期环比 (%)。"""
    by_period: dict[str, float | None] = {}
    for row in _iter_rows(rows):
        period = _period(row)
        if period is None:
            continue
        by_period[period] = _f(row.get("holder"))

    result: dict[str, dict[str, float | None]] = {}
    previous: float | None = None
    for period in sorted(by_period):
        current = by_period[period]
        change = None
        if current is not None and previous not in (None, 0):
            change = round((current / previous - 1.0) * 100.0, 4)
        result[period] = {"holder_num": current, "holder_num_chg": change}
        if current is not None:
            previous = current
    return result


def build_holder_rows(
    symbol: str,
    *,
    top_rows: Any,
    flow_rows: Any,
    num_rows: Any,
) -> list[dict]:
    """合并三路数据为 (symbol, 报告期) 宽表行, 附带公告日。"""
    top = aggregate_top_holders(top_rows, prefix="top10")
    flow = aggregate_top_holders(flow_rows, prefix="flow")
    nums = aggregate_holder_num(num_rows)

    announce_by_period: dict[str, str] = {}
    for rows in (top_rows, flow_rows, num_rows):
        for row in _iter_rows(rows):
            period = _period(row)
            announce = _announce(row)
            if period is None or announce is None:
                continue
            if announce > announce_by_period.get(period, ""):
                announce_by_period[period] = announce

    periods = sorted(set(top) | set(flow) | set(nums))
    records: list[dict] = []
    for period in periods:
        record: dict[str, Any] = {
            "symbol": symbol,
            "period_end": period,
            "announce_date": announce_by_period.get(period),
        }
        record.update(top.get(period) or {})
        record.update(flow.get(period) or {})
        record.update(nums.get(period) or {})
        if all(
            record.get(key) is None
            for key in (record.keys() - {"symbol", "period_end", "announce_date"})
        ):
            continue
        records.append(record)
    return records
