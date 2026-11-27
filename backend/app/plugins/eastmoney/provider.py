"""东方财富(eastmoney)内置数据源 provider。

数据集:
  - moneyflow  个股资金流日线(东财 fflow/daykline, 免 token, 约 120 个交易日历史)。
                归一化列: symbol/date/main_net/super_large_net/large_net/small_net/
                main_rate/large_rate/small_rate。净额单位元(正=净流入), 占比为百分数数值。

设计边界(见 docs/khunter-integration-plan.md §6.3/§7):
  - moneyflow 是「逐标的」维度, provider 方法 get_moneyflow() 是唯一数据集级入口,
    消费方经 custom.loader.get_provider("eastmoney") 直取; 落因子由
    scripts/ingest_moneyflow_factors.py 负责, 走 ext_data 因子通道
    (timeseries + T-1 防前视), 不进 kline 同步主链路。
  - moneyflow/lhb/margin 已注册为能力路由槽位 (tf_provides=False, 默认路由
    eastmoney): plugin.yaml datasets 与本 config.datasets 均声明三项, 使
    provider_has_dataset 与能力矩阵一致。标准行情数据集(daily/realtime 等)
    不实现不声明 → 主流程自动回退 tickflow。

龙虎榜 / 融资融券 (日频事件) 不走 provider 方法: 它们是"按日全市场"维度, 由
``services/eastmoney_sync.py`` 直接调用 ``client.fetch_lhb_day/fetch_margin_day``
并落 ``data/financials/{lhb_metrics,margin_metrics}``; 之所以不做成 provider 方法,
是因为 provider 的契约是按 symbols 批量取数, 与"全市场一天一次"的取数形态不匹配。
datasets 里声明 lhb/margin 仅为接入能力路由矩阵 (展示与门控), 不改变取数形态。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

import polars as pl

from app.data_providers.base import ProviderCapabilities, ProviderRequestError
from app.plugins.eastmoney import client as em_client

# 逐股节流: 东财对高频请求临时封 IP(实测 6+ 连发 → HTTP 000), 保守取 1.2s/股
_THROTTLE_S = 1.2


@dataclass(frozen=True)
class _EastmoneyConfig:
    """对齐 custom 源约定: provider.config.datasets 列可路由数据集。

    moneyflow/lhb/margin 均已注册为能力路由槽位 (tf_provides=False), 与
    plugin.yaml datasets: [moneyflow, lhb, margin] 保持一致 —— lhb/margin 无
    provider 方法 (按日全市场形态, 见模块 docstring), 声明仅为能力矩阵与
    provider_has_dataset 口径统一。
    """

    datasets: dict = field(
        default_factory=lambda: dict.fromkeys(("moneyflow", "lhb", "margin"))
    )


class EastmoneyProvider:
    """东方财富公开接口 provider。moneyflow = 个股资金流日线(免 token)。"""

    name = "eastmoney"
    builtin = True

    def __init__(self) -> None:
        self.config = _EastmoneyConfig()
        self.capabilities = ProviderCapabilities()  # 全 False: 不参与行情能力路由

    def close(self) -> None:  # loader 重建注册表时对每个 provider 调用
        return None

    # ---- moneyflow ----

    def get_moneyflow(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pl.DataFrame:
        """批量拉个股资金流日线, 返回归一化帧(原始日期, 未做 T-1 平移)。

        契约:
          - 单只失败(限流/封禁)记日志并跳过, 不中断整批;
          - 全部失败 → ProviderRequestError(fail-closed), 调用方不得当作空数据处理;
          - start_time/end_time 为可选过滤(含端点), 传 None 返回全部可得历史;
          - 逐股节流 _THROTTLE_S, 调用方无需再自行 sleep。
        """
        frames: list[pl.DataFrame] = []
        failed: list[str] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_THROTTLE_S)
            klines = em_client.fetch_fflow_daykline(em_client.to_secid(symbol))
            if klines is None:
                failed.append(symbol)
                continue
            df = em_client.parse_klines(klines, symbol)
            if not df.is_empty():
                frames.append(df)
        if not frames:
            if failed:
                raise ProviderRequestError(
                    f"eastmoney moneyflow 全部标的拉取失败(疑似限流/封禁): {failed[:5]}"
                )
            return _empty_moneyflow()
        out = pl.concat(frames, how="vertical")
        if start_time is not None:
            out = out.filter(pl.col("date") >= start_time.date())
        if end_time is not None:
            out = out.filter(pl.col("date") <= end_time.date())
        return out.sort(["symbol", "date"])


def _empty_moneyflow() -> pl.DataFrame:
    return em_client.parse_klines([], "-")


def availability() -> tuple[bool, str]:
    """loader 启动自检: 公开免 token 源, 恒可用(启动路径不做网络请求, 避免拖慢/被封)。"""
    return True, "ok"
