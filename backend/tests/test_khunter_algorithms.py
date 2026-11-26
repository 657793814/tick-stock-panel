"""KHunter 移植算法的语义验证（纯函数，零网络/零IO）。"""
from datetime import date, timedelta

import pytest

from app.khunter_algorithms import (
    BAND_RANGE, BAND_SPROUT, BAND_TREND, DIR_UP, DIR_DOWN,
    AdxState, adx_step, adx_run, adx_regime, adx_allows_entry,
    is_distribution_veto, is_main_net_flow_veto,
    compute_flow_metrics, continuous_inflow_days,
)


# ---------------- ADX 状态机 ----------------

def test_adx_band_thresholds():
    assert adx_step(None, 19.0).band == BAND_RANGE
    assert adx_step(None, 20.0).band == BAND_SPROUT
    assert adx_step(None, 25.0).band == BAND_TREND


def test_adx_hysteresis_holds_band_on_small_dip():
    # 26 → 24.2：24.2 >= 25-1=24，迟滞应仍保持 trend（纯阈值会误判 sprout）
    st = adx_step(None, 26.0)
    assert st.band == BAND_TREND
    st2 = adx_step(st, 24.2)
    assert st2.band == BAND_TREND, "迟滞应托住 trend"
    # 跌破 24.0（严格小于下界-buffer）才降档
    st3 = adx_step(st2, 23.9)
    assert st3.band == BAND_SPROUT


def test_adx_direction_two_day_same_direction():
    # 连升两日才 up；单日升/来回震荡沿用前值
    states = adx_run([10, 12, 14])
    assert states[2].direction == DIR_UP
    states2 = adx_run([10, 12, 11])  # 升后降 → 不 up
    assert states2[2].direction != DIR_UP
    states3 = adx_run([20, 22, 21, 20, 19])  # 连降两日 → down
    assert states3[-1].direction == DIR_DOWN


def test_adx_missing_data_preserves_state():
    st = AdxState(band=BAND_TREND, direction=DIR_UP, adx=30.0, prev_adx=28.0)
    out = adx_step(st, None)
    assert out == st


def test_adx_entry_requires_up_and_trend():
    up_trend = AdxState(band=BAND_TREND, direction=DIR_UP, adx=30.0)
    down_trend = AdxState(band=BAND_TREND, direction=DIR_DOWN, adx=30.0)
    up_sprout = AdxState(band=BAND_SPROUT, direction=DIR_UP, adx=22.0)
    assert adx_allows_entry(up_trend) is True
    assert adx_allows_entry(down_trend) is False
    assert adx_allows_entry(up_sprout) is False  # 默认只放 trend


def test_adx_high_guard_downgrades_to_range():
    # adx>=40 且方向非上升 → 震荡（空仓级）
    st = AdxState(band=BAND_TREND, direction=DIR_DOWN, adx=42.0)
    assert adx_regime(st) == BAND_RANGE
    st2 = AdxState(band=BAND_TREND, direction=DIR_UP, adx=42.0)
    assert adx_regime(st2) == BAND_TREND  # 方向上升 → 按 band


# ---------------- 出货否决 ----------------

def test_distribution_veto_by_rate_first():
    # 占比字段优先：大单<-1% 且 小单>1% → 否决
    hit, _ = is_distribution_veto(-500, 500, lg_rate=-1.5, sm_rate=1.2)
    assert hit is True
    # 有占比字段但未达阈值 → 不否决（不再走回退）
    miss, _ = is_distribution_veto(-9999, 9999, lg_rate=-0.5, sm_rate=0.5)
    assert miss is False


def test_distribution_veto_fallback_and_direction():
    # 回退：金额/成交额（万元）
    hit, _ = is_distribution_veto(-500, 300, amount_5d=10000)  # 5%>1% 且3%>1%
    assert hit is True
    # 兜底：无占比无成交额，纯方向
    hit2, _ = is_distribution_veto(-100, 100)
    assert hit2 is True
    # 同向（都流入）不否决
    assert is_distribution_veto(100, 100)[0] is False


# ---------------- 主力净额否决 ----------------

def test_main_net_flow_veto_combined():
    # 绝对额达标 + 占比确认 → 否决
    hit, _ = is_main_net_flow_veto(-12000, lg_rate=-6.0)
    assert hit is True
    # 绝对额超阈但占比温和 → 不否决（防错杀）
    miss, _ = is_main_net_flow_veto(-12000, lg_rate=-2.0)
    assert miss is False
    # 绝对额未达阈 → 直接放行
    assert is_main_net_flow_veto(-5000, lg_rate=-9.0)[0] is False


# ---------------- 5日聚合 ----------------

def test_flow_metrics_complete_and_fail_closed():
    rows = [
        {"date": date(2026, 1, i), "net_amount": 100 * i,
         "large_net": 60 * i, "small_net": 40 * i,
         "lg_rate": 1.0, "sm_rate": 0.5, "main_rate": 1.5}
        for i in range(1, 6)
    ]
    m = compute_flow_metrics(rows, window=5)
    assert m["complete"] is True and m["missing"] == 0
    assert m["net_flow_5d"] == 100 * (1 + 2 + 3 + 4 + 5)
    # 窗口不齐 → complete=False（回测严格模式应拒绝判定）
    m2 = compute_flow_metrics(rows[:3], window=5)
    assert m2["complete"] is False and m2["missing"] == 2
    assert compute_flow_metrics([], window=5)["complete"] is False


# ---------------- 连续净流入 ----------------

def test_continuous_inflow_days():
    base = date(2026, 1, 1)
    flows = [(base + timedelta(days=i), v) for i, v in enumerate([100, 200, 300, -50, 100])]
    assert continuous_inflow_days(flows) == 1  # 最后一日100，前一日-50中断
    flows2 = [(base + timedelta(days=i), v) for i, v in enumerate([100, 200, 300, 400])]
    assert continuous_inflow_days(flows2) == 4
    assert continuous_inflow_days([]) == 0
    # as_of 截断
    assert continuous_inflow_days(flows, as_of=base + timedelta(days=2)) == 3


# ---------------- ADX 计算 service（Wilder 平滑）----------------

def test_wilder_rma_constant_series():
    from app.services.adx_regime import _wilder_rma
    r = _wilder_rma([10.0] * 40, 14)
    assert abs(r[-1] - 10.0) < 1e-9


def test_compute_adx_strong_uptrend():
    import polars as pl
    from datetime import date, timedelta
    from app.services.adx_regime import compute_adx
    base = date(2026, 1, 1)
    rows = [{"date": base + timedelta(days=i), "open": 100 + 2*i,
             "high": 102 + 2*i, "low": 99 + 2*i, "close": 101 + 2*i}
            for i in range(60)]
    df = pl.DataFrame(rows, schema={"date": pl.Date, "open": pl.Float64,
                                    "high": pl.Float64, "low": pl.Float64, "close": pl.Float64})
    adx = compute_adx(df)
    assert adx["adx"][-1] > 25
    assert adx["plus_di"][-1] > adx["minus_di"][-1]


def test_adx_strategy_loads_and_filters():
    from pathlib import Path
    import polars as pl
    from app.strategy.engine import StrategyEngine
    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    s = engine.get("khunter_adx_regime")
    assert s is not None
    assert abs(sum(float(v) for v in s.meta["scoring"].values()) - 1.0) < 1e-6
    frame = pl.DataFrame({
        "change_pct": [0.02, 0.02],
        "ext_ext_adx_adx_entry": [1.0, 0.0],
        "ext_ext_adx_adx": [30.0, 28.0],
    })
    out = frame.filter(s.filter_fn(frame, {"min_adx": 25.0}))
    assert out.height == 1  # 只放行 entry=1


# ---------------- 资金流脚本纯函数（无网络）----------------

def test_moneyflow_secid_and_parse():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1].parent
    p = root / "scripts" / "ingest_moneyflow_factors.py"
    if not p.exists():
        pytest.skip("资金流脚本不存在")
    spec = importlib.util.spec_from_file_location("ingest_mf", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.to_secid("600000.SH") == "1.600000"
    assert mod.to_secid("000001.SZ") == "0.000001"
    assert mod.to_secid("830799.BJ") == "0.830799"
    # f51..f61 样例：主力f52=大单f55+超大单f56
    line = "2026-11-20,-100.0,-50.0,100.0,-30.0,-70.0,-1.5,-0.8,1.5,-0.5,-1.0"
    df = mod.parse_klines([line], "000001.SZ")
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["main_net"] == row["large_net"] + row["super_large_net"]


# ---------------- eastmoney 插件契约（可插拔验证） ----------------

def test_eastmoney_plugin_registered_in_loader():
    """插件被真实 loader 注册: available、origin=builtin。

    moneyflow 属非路由数据集(参照 fuyao.dragon_tiger 项目约定), 不进清单 datasets
    → 清单 datasets 为空列表。
    """
    from app.data_providers.custom import loader
    plugins = {p["name"]: p for p in loader.list_plugins()}
    em = plugins.get("eastmoney")
    assert em is not None, "eastmoney 未被 loader 注册"
    assert em["available"] is True
    assert em["datasets"] == []
    assert em["origin"] == "builtin"


def test_eastmoney_provider_dataset_contract():
    """非路由约定: config.datasets 为空 → provider_has_dataset 全 False(不抢路由)。

    消费契约 = provider 方法存在: get_provider("eastmoney").get_moneyflow 可调用。
    """
    from app.data_providers.custom import loader
    for ds in ("daily", "realtime", "minute", "adj_factor", "financial", "moneyflow"):
        assert loader.provider_has_dataset("eastmoney", ds) is False
    provider = loader.get_provider("eastmoney")
    assert callable(provider.get_moneyflow)
    assert callable(provider.close)


def test_eastmoney_get_moneyflow_normalization(monkeypatch):
    """get_moneyflow 归一化: 排序、恒等式、时间过滤; 全失败 fail-closed。"""
    from datetime import datetime
    from app.plugins.eastmoney import client as emc
    from app.plugins.eastmoney.provider import EastmoneyProvider
    from app.data_providers.base import ProviderRequestError

    fake = ["2026-11-20,-100.0,-50.0,100.0,-30.0,-70.0,-1.5,-0.8,1.5,-0.5,-1.0",
            "2026-11-19,50.0,20.0,-30.0,20.0,30.0,0.8,0.3,-0.5,0.3,0.5"]
    monkeypatch.setattr(emc, "fetch_fflow_daykline", lambda secid, **kw: list(fake))
    p = EastmoneyProvider()
    df = p.get_moneyflow(["000001.SZ"])
    assert df.height == 2 and df["date"].to_list() == sorted(df["date"].to_list())
    row = df.row(0, named=True)
    assert row["main_net"] == row["large_net"] + row["super_large_net"]
    # 时间过滤
    df2 = p.get_moneyflow(["000001.SZ"], start_time=datetime(2026, 11, 20))
    assert df2.height == 1 and df2["date"][0].isoformat() == "2026-11-20"
    # 全失败 → ProviderRequestError
    monkeypatch.setattr(emc, "fetch_fflow_daykline", lambda secid, **kw: None)
    try:
        p.get_moneyflow(["X.Y"])
        raise AssertionError("全失败应抛 ProviderRequestError")
    except ProviderRequestError:
        pass
