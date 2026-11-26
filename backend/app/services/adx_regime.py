"""ADX 择时因子计算（Wilder 平滑 + KHunter 三态状态机）。

为当前项目补上原本缺失的 ADX regime 能力：
  - 从 OHLC 用标准 Wilder 平滑算 ADX(14)：TR / +DM / -DM → Wilder RMA → +DI/-DI → DX → ADX。
  - 用 app.khunter_algorithms 的状态机（带迟滞/两日同向/反转日/高位守门）算 band/direction。
  - 输出 (symbol, date, adx, adx_band_code, adx_dir_code, adx_entry) 面板，落 ext_data
    timeseries 后成为标准因子列，可被任意策略的 scoring / filter 引用。

T-1 防前视：ADX 用当日收盘算，落盘时 date 平移到下一交易日（与题材/资金流因子一致）。

纯计算函数 compute_adx / build_adx_panel 不依赖网络与 IO，可单测。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.khunter_algorithms import DIR_DOWN, DIR_UP, adx_allows_entry, adx_step

ADX_PERIOD = 14


def _wilder_rma(values: list[float], period: int) -> list[float]:
    """Wilder 平滑移动平均（RMA）：首值 = SMA(period)，其后 rma = (prev*(n-1)+x)/n。"""
    out = [float("nan")] * len(values)
    valid = [i for i, v in enumerate(values) if v == v]  # 非 NaN 索引
    if len(valid) < period:
        return out
    s = sum(values[i] for i in valid[:period])
    seed = s / period
    prev = seed
    for k, i in enumerate(valid):
        if k < period - 1:
            continue
        if k == period - 1:
            prev = seed
        else:
            prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def compute_adx(df: pl.DataFrame, period: int = ADX_PERIOD) -> pl.DataFrame:
    """从单只股票的 OHLC（按 date 升序）计算 ADX/±DI。

    返回列：date, adx, plus_di, minus_di。预热不足处 adx 为 NaN。
    """
    d = df.sort("date")
    high, low, close = d["high"].to_list(), d["low"].to_list(), d["close"].to_list()
    n = len(d)
    if n == 0:
        return pl.DataFrame(schema={"date": pl.Date, "adx": pl.Float64,
                                    "plus_di": pl.Float64, "minus_di": pl.Float64})
    tr, pdm, mdm = [float("nan")] * n, [float("nan")] * n, [float("nan")] * n
    for i in range(n):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            up = high[i] - high[i - 1]
            dn = low[i - 1] - low[i]
            tr[i] = max(high[i] - low[i], abs(up), abs(dn))
            pdm[i] = up if (up > dn and up > 0) else 0.0
            mdm[i] = dn if (dn > up and dn > 0) else 0.0
    atr = _wilder_rma(tr, period)
    spdm = _wilder_rma(pdm, period)
    smdm = _wilder_rma(mdm, period)
    plus_di, minus_di, dx = [], [], []
    for i in range(n):
        if atr[i] != atr[i] or atr[i] <= 0:
            plus_di.append(float("nan")); minus_di.append(float("nan")); dx.append(float("nan"))
            continue
        pdi = 100 * spdm[i] / atr[i]
        mdi = 100 * smdm[i] / atr[i]
        denom = pdi + mdi
        plus_di.append(pdi); minus_di.append(mdi)
        dx.append(100 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    adx = _wilder_rma(dx, period)
    # NaN → null，便于 Polars 处理
    def _nn(x):
        return None if x != x else x
    return pl.DataFrame({
        "date": d["date"].to_list(),
        "adx": [_nn(x) for x in adx],
        "plus_di": [_nn(x) for x in plus_di],
        "minus_di": [_nn(x) for x in minus_di],
    })


def build_adx_panel(kline: pl.DataFrame, period: int = ADX_PERIOD) -> pl.DataFrame:
    """对多股票 K 线面板计算 ADX + 状态机列（原始日期，未 T-1 平移）。

    输出列：symbol, date, adx, adx_band_code(1=trend,2=sprout,3=range,0=未预热),
            adx_dir_code(1=up,-1=down,0=未定), adx_entry(0/1)。
    """
    frames = []
    for symbol, group in kline.group_by("symbol", maintain_order=True):
        sym = symbol[0] if isinstance(symbol, tuple) else symbol
        adx_df = compute_adx(group, period)
        states = []
        state = None
        for v in adx_df["adx"].to_list():
            state = adx_step(state, v)
            states.append(state)
        band_code = {"trend": 1, "sprout": 2, "range": 3}.get
        frames.append(adx_df.with_columns([
            pl.Series("symbol", [sym] * adx_df.height),
            pl.Series("adx_band_code", [band_code(s.band, 0) for s in states], dtype=pl.Float64),
            pl.Series("adx_dir_code", [
                1.0 if s.direction == DIR_UP else (-1.0 if s.direction == DIR_DOWN else 0.0)
                for s in states
            ], dtype=pl.Float64),
            pl.Series("adx_entry", [1.0 if adx_allows_entry(s) else 0.0 for s in states], dtype=pl.Float64),
        ]))
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "adx": pl.Float64,
                                    "adx_band_code": pl.Float64, "adx_dir_code": pl.Float64,
                                    "adx_entry": pl.Float64})
    out = pl.concat(frames)
    return out.select(["symbol", "date", "adx", "adx_band_code", "adx_dir_code", "adx_entry"])
