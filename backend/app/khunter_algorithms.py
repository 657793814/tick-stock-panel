"""从 KHunter 移植的纯算法资产（零核心耦合、零网络、可单测）。

来源：/Users/liuzuodong/Documents/workspace/KHunter（只读分析后语义移植，非文件复制）。
移植原则：
  - 只搬可独立抽离的纯函数/状态机，不搬 KHunter 的 SQLite/DAO/回测引擎。
  - 去掉 KHunter 注释里已明确废弃的「降温机制 cooled / 峰值40 / fell_from_high」。
  - 保留算法口径原样（阈值、迟滞、两日同向、三级判定），阈值以常量暴露便于调参。
  - 资金流单位：KHunter 用「万元」，本模块沿用「万元」口径（东财元值需 /1e4 后传入）。

包含：
  1. ADX 三态状态机（band 迟滞 + dir 两日同向 + 反转日不吃缓冲 + 高位守门）
  2. 出货信号一票否决（占比优先→成交额回退→纯方向兜底）
  3. 主力净额一票否决（绝对额 + 相对占比联合判定）
  4. 5 日资金本地聚合（含 complete/missing 完整性标记，fail-closed）
  5. 连续净流入天数（逐日回溯，遇净流出/缺数据即止）
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------- 常量（KHunter 口径）

# ADX 分档
BAND_RANGE = "range"       # < 20 震荡
BAND_SPROUT = "sprout"     # 20~25 萌芽
BAND_TREND = "trend"       # >= 25 明确
BAND_THRESHOLDS = (20.0, 25.0)
BAND_BUFFER = 1.0          # 迟滞宽度
HIGH_ADX_GUARD = 40.0      # 高位守门：adx>=40 且方向非上升 → 震荡
DIR_UP, DIR_DOWN, DIR_UNKNOWN = "up", "down", ""

# 资金流否决阈值（KHunter moneyflow_scorer.py:51-67，单位万元 / %）
VETO_SCORE = -100.0
VETO_MAIN_NET_FLOW_THRESHOLD = -10000.0    # 5日主力净额 < -1亿元(万元口径) 触发粗筛
VETO_NET_FLOW_RATIO_THRESHOLD = -5.0       # 大单净流入占比 < -5% 确认否决
VETO_DISTRIBUTION_LG_THRESHOLD = -1.0      # 大单净流入占比 < -1%
VETO_DISTRIBUTION_SM_THRESHOLD = 1.0       # 小单净流入占比 > +1%
FLOW_WINDOW = 5


def _f(value) -> float:
    """安全转 float（None/空/非法/非有限 → 0.0）。"""
    if value is None or value == "":
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


# ================================================================ 1. ADX 三态状态机

@dataclass(frozen=True)
class AdxState:
    band: str = ""              # range/sprout/trend
    direction: str = ""         # up/down/空
    adx: Optional[float] = None
    prev_adx: Optional[float] = None

    @property
    def warmed_up(self) -> bool:
        return bool(self.band) and bool(self.direction)


def _band_of(adx: float) -> str:
    """纯阈值分档（无迟滞）。非法值 → ''。"""
    v = _f(adx)
    if not math.isfinite(v) or adx is None:
        return ""
    if v < BAND_THRESHOLDS[0]:
        return BAND_RANGE
    if v < BAND_THRESHOLDS[1]:
        return BAND_SPROUT
    return BAND_TREND


def _lower_bound(band: str) -> float:
    if band == BAND_RANGE:
        return 0.0
    if band == BAND_SPROUT:
        return BAND_THRESHOLDS[0]
    if band == BAND_TREND:
        return BAND_THRESHOLDS[1]
    return -1.0


def _apply_hysteresis(adx: float, current_band: str, buffer: float = BAND_BUFFER) -> str:
    """带迟滞分档：升档需 >= 下界+buffer；降档需 < 下界-buffer（严格）；否则保持。"""
    raw = _band_of(adx)
    if not raw:
        return ""
    cur = str(current_band or "")
    if not cur or cur == raw or cur not in (BAND_RANGE, BAND_SPROUT, BAND_TREND):
        return raw
    cur_low, raw_low = _lower_bound(cur), _lower_bound(raw)
    if raw_low > cur_low:
        return raw if _f(adx) >= raw_low + buffer else cur
    if raw_low < cur_low:
        return raw if _f(adx) < cur_low - buffer else cur
    return raw


def _is_mid_reversal(direction: str, current_band: str, adx: Optional[float]) -> bool:
    """回落中的中间反转（方向转升、迟滞正托住更低原值档）→ 当日按原值判档。

    实测只收紧不放宽：迟滞会把「回落前旧高档」继续托住，与 dir=up 组合成误放行，
    故反转日令 buffer=0。
    """
    if direction != DIR_UP or not current_band or adx is None:
        return False
    raw = _band_of(adx)
    if not raw or current_band == raw:
        return False
    return _lower_bound(raw) < _lower_bound(current_band)


def adx_step(state: Optional[AdxState], adx: Optional[float]) -> AdxState:
    """推进一个交易日（f(今日adx, 昨日状态)）。缺数据原样返回前态（不破坏状态）。

    dir 规则：连升两日(a>prev>grand)=up；连降两日(a<prev<grand)=down；其余沿用前日。
    band 规则：迟滞分档；反转日不吃缓冲。
    """
    st = state if isinstance(state, AdxState) else AdxState()
    if adx is None or not math.isfinite(_f(adx)):
        return st
    a = float(adx)
    prev, grand = st.adx, st.prev_adx

    d = st.direction
    if prev is not None and grand is not None:
        if prev > grand and a > prev:
            d = DIR_UP
        elif prev < grand and a < prev:
            d = DIR_DOWN
        # 其余沿用前一日

    mid_rev = _is_mid_reversal(d, st.band, a)
    band = _apply_hysteresis(a, st.band, buffer=0.0 if mid_rev else BAND_BUFFER)
    return AdxState(band=band, direction=d, adx=a, prev_adx=prev)


def adx_run(values: Iterable[Optional[float]]) -> list[AdxState]:
    """按升序回放序列，返回每日状态（调试/单测便利）。"""
    st = AdxState()
    out = []
    for v in values:
        st = adx_step(st, v)
        out.append(st)
    return out


def adx_regime(state: Optional[AdxState]) -> str:
    """唯一判定式：方向非上升时只降档不放宽（高位守门 + 明确降萌芽）。"""
    if state is None:
        return ""
    if state.direction != DIR_UP:
        if state.adx is not None and _f(state.adx) >= HIGH_ADX_GUARD:
            return BAND_RANGE
        if state.band == BAND_TREND:
            return BAND_SPROUT
    return state.band


def adx_allows_entry(state: Optional[AdxState], bands: Sequence[str] = (BAND_TREND,)) -> bool:
    """入场判定：硬条件 dir=up 且 band ∈ bands（默认只放 trend）。

    ⚠️ 不可用 adx_regime 的结果判（萌芽有两种来源会误放行），必须读原始两要素。
    """
    if state is None or state.direction != DIR_UP:
        return False
    return state.band in tuple(bands or (BAND_TREND,))


# ================================================================ 2/3. 资金流一票否决

def is_distribution_veto(large_net: float, small_net: float,
                         lg_rate: Optional[float] = None,
                         sm_rate: Optional[float] = None,
                         amount_5d: float = 0.0) -> tuple[bool, str]:
    """出货信号否决：大单净流出 + 小单净流入，按占比强度三级判定。

    优先级：① 接口占比字段(lg_rate<-1% 且 sm_rate>+1%) → ② 金额/成交额 → ③ 纯方向兜底。
    large_net/small_net 单位万元，正=净流入。
    """
    lg, sm = _f(large_net), _f(small_net)
    # ① 占比字段
    if lg_rate is not None and sm_rate is not None:
        lgr, smr = _f(lg_rate), _f(sm_rate)
        if lgr < VETO_DISTRIBUTION_LG_THRESHOLD and smr > VETO_DISTRIBUTION_SM_THRESHOLD:
            return True, f"出货：大单占比{lgr:.2f}%<-1% 且 小单占比{smr:.2f}%>1%"
        return False, ""   # 有占比字段即以其为准，不再走回退
    # ② 金额/成交额
    if not (lg < 0 and sm > 0):
        return False, ""
    amt = _f(amount_5d)
    if amt > 0:
        lg_ratio, sm_ratio = -lg / amt * 100, sm / amt * 100
        if (lg_ratio > VETO_DISTRIBUTION_SM_THRESHOLD
                and sm_ratio > VETO_DISTRIBUTION_SM_THRESHOLD):
            return True, f"出货：大单流出占额{lg_ratio:.2f}% 且 小单流入占额{sm_ratio:.2f}%"
        return False, ""
    # ③ 纯方向兜底
    return True, "出货：大单净流出且小单净流入"


def is_main_net_flow_veto(net_flow_5d: float,
                          lg_rate: Optional[float] = None,
                          amount_5d: float = 0.0) -> tuple[bool, str]:
    """主力净额否决：绝对额 + 相对占比联合判定（防大成交额股票被绝对额错杀）。

    net_flow_5d 单位万元。占比优先用 lg_rate(大单净流入占比%)，回退 净额/成交额。
    """
    nf = _f(net_flow_5d)
    if nf >= VETO_MAIN_NET_FLOW_THRESHOLD:
        return False, ""
    if lg_rate is not None:
        lgr = _f(lg_rate)
        if lgr < VETO_NET_FLOW_RATIO_THRESHOLD:
            return True, f"主力净额{nf:.0f}万元偏低且大单占比{lgr:.2f}%<-5%"
        return False, ""
    amt = _f(amount_5d)
    if amt > 0:
        ratio = nf / amt * 100
        if ratio >= VETO_NET_FLOW_RATIO_THRESHOLD:
            return False, ""
        return True, f"主力净额{nf:.0f}万元偏低且占成交额{ratio:.2f}%<-5%"
    return True, f"主力净额{nf:.0f}万元 < -1亿元"


# ================================================================ 4. 5日资金聚合

def compute_flow_metrics(rows: Optional[Iterable[dict]], window: int = FLOW_WINDOW) -> dict:
    """按本地口径计算窗口内资金流指标（fail-closed：窗口不齐 complete=False）。

    rows: 日粒度 dict，键 net_amount/large_net/small_net + *_rate（万元/%），顺序不限。
    源端聚合字段（如 net_d5_amount）不参与计算，只本地求和（KHunter 定稿口径）。
    """
    metrics = {
        "net_flow_5d": 0.0, "large_net": 0.0, "small_net": 0.0,
        "lg_rate_avg": 0.0, "sm_rate_avg": 0.0, "main_rate_avg": 0.0,
        "days": 0, "complete": False, "missing": window,
    }
    if not rows:
        return metrics
    ordered = sorted(rows, key=lambda r: str(r.get("date", r.get("trade_date", ""))))
    used = ordered[-window:] if window > 0 else ordered
    metrics["net_flow_5d"] = _f(sum(_f(r.get("net_amount")) for r in used))
    metrics["large_net"] = _f(sum(_f(r.get("large_net", r.get("buy_lg_amount"))) for r in used))
    metrics["small_net"] = _f(sum(_f(r.get("small_net", r.get("buy_sm_amount"))) for r in used))
    n = len(used)
    for key, src in (("lg_rate_avg", "lg_rate"), ("sm_rate_avg", "sm_rate"),
                     ("main_rate_avg", "main_rate")):
        metrics[key] = _f(sum(_f(r.get(src)) for r in used) / n) if n else 0.0
    metrics["days"] = n
    metrics["complete"] = n >= window
    metrics["missing"] = max(0, window - n)
    return metrics


# ================================================================ 5. 连续净流入天数

def continuous_inflow_days(flow_by_day: Sequence[tuple[date, float]],
                           as_of: Optional[date] = None) -> int:
    """从最新交易日向前回溯，统计连续净流入天数（遇净流出/<=0 即止）。

    flow_by_day: [(date, 主力净额), ...]，日期升序。as_of 默认取最后一日。
    ⚠️ 与「有数据但为 0」区分：0 视为净流出（中断）。
    """
    if not flow_by_day:
        return 0
    ordered = sorted(flow_by_day, key=lambda x: x[0])
    if as_of is not None:
        ordered = [x for x in ordered if x[0] <= as_of]
    if not ordered:
        return 0
    days = 0
    for _d, net in reversed(ordered):
        if _f(net) > 0:
            days += 1
        else:
            break
    return days
