"""东方财富公开 HTTP 接口 client。

取数纪律（CONTRIBUTING「不伪造数据」）：
  - 用 curl 子进程取数（urllib 会被东财服务端断连，curl 实测可达）；
  - 必带 UA + Referer（缺 header 会被风控拒绝）；
  - 失败指数退避 + 随机抖动；空响应/连接失败明确返回 None（不得转成空数据冒充成功）；
  - 东财对短时间高频请求会临时封 IP（实测 6+ 次/几秒 → HTTP 000），调用方必须节流。
"""
from __future__ import annotations

import json
import random
import subprocess
import time
from datetime import date

import polars as pl

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
FFLOW_DAYKLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

# fields2 顺序: f51 日期, f52 主力净额, f53 小单, f54 中单, f55 大单, f56 超大单,
#               f57 主力净占比, f58 小单占比, f59 中单占比, f60 大单占比, f61 超大单占比
FFLOW_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

# 接口单位与口径（CONTRIBUTING §3.1，已用加总恒等式实测验证，不可凭字段名推断）：
#   - 净额单位为元，正 = 净流入；
#   - 恒等式 f52 == f55 + f56（主力 = 大单 + 超大单），f53+f54+f55+f56 == 0；
#   - 占比为百分数数值（-1.5 表示 -1.5%），非小数制；
#   - 历史深度约 120 个交易日（lmt 传更大值无效），仅覆盖近半年。


def to_secid(symbol: str) -> str:
    """'600000.SH'/'000001.SZ'/'830799.BJ' → 东财 secid '1.600000'/'0.000001'/'0.830799'。"""
    code, _, exchange = symbol.strip().partition(".")
    code = code.strip()
    if exchange.upper() == "SH":
        return f"1.{code}"
    # 深市与北交所在东财均为 0 前缀
    return f"0.{code}"


def parse_klines(klines: list[str], symbol: str) -> pl.DataFrame:
    """把东财 klines 字符串列表解析为资金流帧（原始日期，未做 T-1 平移）。

    纯函数，不依赖网络，可单测。畸形行跳过（不抛错、不造数）。
    """
    rows = []
    for line in klines or []:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        try:
            d = date.fromisoformat(parts[0])
            f52, _f53, _f54, f55, f56, f57, _f58, _f59, f60, _f61 = map(float, parts[1:11])
        except (ValueError, TypeError):
            continue
        rows.append({
            "symbol": symbol,
            "date": d,
            "main_net": f52,        # 元，主力净额 = 大单 + 超大单
            "large_net": f55,       # 元，大单
            "super_large_net": f56,  # 元，超大单
            "small_net": _f53,      # 元，小单
            "main_rate": f57,       # %
            "large_rate": f60,      # %
            "small_rate": _f58,     # %
        })
    return pl.DataFrame(rows, schema={
        "symbol": pl.Utf8, "date": pl.Date,
        "main_net": pl.Float64, "large_net": pl.Float64,
        "super_large_net": pl.Float64, "small_net": pl.Float64,
        "main_rate": pl.Float64, "large_rate": pl.Float64, "small_rate": pl.Float64,
    })


def fetch_fflow_daykline(
    secid: str,
    *,
    lmt: int = 500,
    retries: int = 3,
    timeout: int = 15,
) -> list[str] | None:
    """拉单只标的的资金流日线 klines。

    返回：klines 字符串列表；无数据（退市/无记录）返回 []；
    请求失败（限流/封禁/网络）返回 None —— 调用方必须区分这两种空。
    """
    url = (
        f"{FFLOW_DAYKLINE_URL}?lmt={lmt}&klt=101&secid={secid}"
        f"&fields1=f1,f2,f3,f7&fields2={FFLOW_FIELDS2}"
    )
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            out = subprocess.run(
                ["curl", "-s", "--max-time", str(timeout), "-A", UA,
                 "-H", "Referer: https://quote.eastmoney.com/", url],
                capture_output=True, text=True, timeout=timeout + 5,
            ).stdout.strip()
            if not out:
                raise RuntimeError("空响应(可能限流/封禁)")
            data = json.loads(out).get("data")
            if not data or not data.get("klines"):
                return []
            return data["klines"]
        except Exception as exc:  # noqa: BLE001 — 统一退避重试
            last_err = exc
            if attempt < retries - 1:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    if last_err is not None:
        print(f"[eastmoney] fetch fail secid={secid}: {last_err}", flush=True)
    return None
