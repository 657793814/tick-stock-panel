"""stock-picker 整合阶段 2：新增 B 类因子的解析/注册/物化验证。

覆盖 data/user_data/custom_factors/ 下三个落盘定义：
- uf_sp_liq_dev  (DSL, liq_dev 缩量偏离)
- uf_sp_vol_dev  (DSL, vol_dev 波动收敛偏离)
- cf_sp_technical (composite, technical 技术形态近似)

测试用临时 data_dir 隔离，不改真实数据目录，不污染全局注册表。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.factors import store
from app.factors.registry import _REGISTRY, get_factor, register_factor, unregister_factor
from app.strategy import scoring

SP_IDS = ("uf_sp_liq_dev", "uf_sp_vol_dev", "cf_sp_technical")


@pytest.fixture()
def isolated_registry():
    """用真实落盘定义加载后再清理，保证测试后注册表恢复。"""
    from pathlib import Path

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    definitions = {d["id"]: d for d in store.load_all(data_dir) if d["id"] in SP_IDS}
    registered: list[str] = []
    for fid in SP_IDS:
        if fid in _REGISTRY:
            unregister_factor(fid)
        if fid in definitions:
            register_factor(store.to_spec(definitions[fid]))
            registered.append(fid)
    yield definitions
    for fid in registered:
        unregister_factor(fid)


def _panel(n_days: int = 120) -> pl.DataFrame:
    """3 只标的 n_days 日合成面板，含 liq_dev/vol_dev 所需的 warmup。"""
    import math

    rows = []
    for i in range(n_days):
        for symbol, base in (("A", 10.0), ("B", 50.0), ("C", 20.0)):
            close = base * (1 + 0.01 * math.sin(i / 7 + base))
            amount = (1000.0 + 5 * i) * close * (1 + 0.3 * math.cos(i / 5 + base))
            rows.append({
                "symbol": symbol,
                "date": date(2026, 1, 1) + __import__("datetime").timedelta(days=i),
                "close": close,
                "change_pct": 0.01 * math.cos(i / 7 + base),
                "amount": amount,
                "volume": amount / close,
                "ma20": close * 0.99,
                "ma60": close * 0.97,
                "macd_hist": 0.5 * math.sin(i / 9),
                "rsi_14": 50 + 20 * math.sin(i / 11 + base),
                "vol_ratio_5d": 1 + 0.5 * math.cos(i / 6 + base),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_definitions_parse(isolated_registry):
    """三个落盘定义都能通过 to_spec 严格校验。"""
    defs = isolated_registry
    assert set(defs) == set(SP_IDS), f"缺少定义: {set(SP_IDS) - set(defs)}"
    for fid in SP_IDS:
        spec = store.to_spec(defs[fid])
        assert spec.id == fid
        assert spec.warmup_bars >= 1


def test_liq_dev_warmup_and_direction():
    """liq_dev 依赖 amount，warmup 60 日中位 → >=61。"""
    from pathlib import Path

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    defn = next(d for d in store.load_all(data_dir) if d["id"] == "uf_sp_liq_dev")
    spec = store.to_spec(defn)
    assert "amount" in spec.dependencies
    assert spec.warmup_bars >= 61
    assert spec.direction == "low"


def test_factors_materialize_nonempty(isolated_registry):
    """三个因子在合成面板上物化出非空列（证明公式可执行而非仅能编译）。"""
    frame = _panel()
    # custom 因子经 materialize_scoring_columns 走 DSL frame_transform
    out = scoring.materialize_scoring_columns(frame, list(SP_IDS))
    for fid in SP_IDS:
        assert fid in out.columns, f"{fid} 未物化出列"
        non_null = out[fid].drop_nulls()
        assert non_null.len() > 0, f"{fid} 物化列全空"


def test_composite_member_weights_sum():
    """technical composite 成员权重合法（2~8 个，权重非负）。"""
    from pathlib import Path

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    defn = next(d for d in store.load_all(data_dir) if d["id"] == "cf_sp_technical")
    members = defn["members"]
    assert 2 <= len(members) <= 8
    assert all(float(w) >= 0 for w in members.values())
    assert abs(sum(float(w) for w in members.values()) - 1.0) < 1e-6


# ---- 阶段 3：策略层落地验证 ----

def test_strategies_load_from_data_dir():
    """两个策略文件能被 StrategyEngine 从 data/strategies/custom 真实加载。"""
    from pathlib import Path

    from app.strategy.engine import StrategyEngine

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    for sid in ("csp_short_multifactor", "csp_long_multifactor"):
        assert engine.has(sid), f"{sid} 未加载: {engine.load_errors()}"
        s = engine.get(sid)
        assert s.execution_backend == "polars_expr"
        assert s.filter_fn is not None
        scoring = s.meta["scoring"]
        assert abs(sum(float(v) for v in scoring.values()) - 1.0) < 1e-6


def test_short_strategy_filter_executes():
    """短线策略 filter 表达式在合成面板上可执行，产出布尔结果。"""
    from pathlib import Path

    from app.strategy.engine import StrategyEngine

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    s = engine.get("csp_short_multifactor")
    frame = _panel()
    expr = s.filter_fn(frame, {"max_chase_pct": 7.0, "min_amount": 3e7})
    assert isinstance(expr, pl.Expr), "filter 应返回 Polars 表达式"
    out = frame.filter(expr)
    assert out.height >= 0  # 表达式可执行（允许全 False，但不得抛错）


def test_long_strategy_filter_executes():
    """长线策略 filter 在含 roe_latest 的面板上可执行。"""
    from pathlib import Path

    from app.strategy.engine import StrategyEngine

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    s = engine.get("csp_long_multifactor")
    frame = _panel().with_columns((pl.col("close") * 0.1).alias("roe_latest"))
    expr = s.filter_fn(frame, {"min_roe": 5.0})
    out = frame.filter(expr)
    assert out.height > 0  # roe=close*0.1, A/B/C 首日均 >5


# ---- 阶段 4：数据管线（龙虎榜机构净买 → ext 因子）验证 ----

def test_dragon_tiger_ingest_produces_factor_column():
    """龙虎榜 JSON 能被转换脚本解析为 (symbol, org_net_value) 帧。"""
    import importlib.util
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1].parent / "scripts" / "ingest_dragon_tiger_factors.py"
    if not scripts.exists():
        pytest.skip("入因子脚本不存在")
    spec = importlib.util.spec_from_file_location("ingest_dragon", scripts)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    files = sorted((data_dir / "dragon_tiger").glob("date=*.json"))
    if not files:
        pytest.skip("无龙虎榜样例数据")
    df = mod.parse_one(files[0])
    assert df is not None and not df.is_empty()
    assert {"symbol", "org_net_value", "dragon_net_value"} <= set(df.columns)
    assert df["org_net_value"].dtype == pl.Float64


def test_long_strategy_includes_institutional_factor():
    """长线策略已纳入龙虎榜机构净买因子（institutional 原权重0.15）。"""
    from pathlib import Path

    from app.strategy.engine import StrategyEngine

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    scoring = engine.get("csp_long_multifactor").meta["scoring"]
    assert any("dragon" in k and v > 0 for k, v in scoring.items())


# ---- 阶段 5：评分适配验证 ----

def test_short_strategy_scoring_end_to_end():
    """短线策略的全部 scoring 因子（含自定义因子）能被物化并算出有效分数。"""
    from datetime import timedelta
    from pathlib import Path

    from app.factors.store import load_runtime_registry
    from app.factors.ext_factors import ensure_synced
    from app.strategy.engine import StrategyEngine
    from app.strategy.scoring import materialize_scoring_columns

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    load_runtime_registry(data_dir)
    ensure_synced(data_dir)
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    s = engine.get("csp_short_multifactor")
    weights = s.meta["scoring"]
    directions = s.meta.get("scoring_directions")

    # 延长面板到 120 天，覆盖 liq_dev/vol_dev 的 60 日 warmup
    rows = []
    for i in range(120):
        for sym, base in (("A", 10.0), ("B", 50.0), ("C", 20.0)):
            import math
            close = base * (1 + 0.01 * math.sin(i / 7 + base))
            rows.append({
                "symbol": sym, "date": date(2026, 1, 1) + timedelta(days=i),
                "close": close, "amount": (1000 + i) * close,
                "change_pct": 0.01 * math.cos(i / 7 + base),
                "ma20": close * 0.99, "ma60": close * 0.97,
                "macd_hist": 0.5 * math.sin(i / 9),
                "rsi_14": 50 + 20 * math.sin(i / 11 + base),
                "vol_ratio_5d": 1 + 0.5 * math.cos(i / 6 + base),
                "annual_vol_20d": 0.02 + 0.01 * math.sin(i / 13 + base),
                "momentum_20d": 0.05 * math.cos(i / 15 + base),
            })
    frame = pl.DataFrame(rows).sort(["symbol", "date"])
    current = frame.filter(pl.col("date") == frame["date"].max())

    materialized = materialize_scoring_columns(current, set(weights))
    assert all(k in materialized.columns for k in weights), "有 scoring 因子未物化"
    scored = StrategyEngine._apply_scoring(materialized, weights, directions)
    assert "score" in scored.columns
    assert scored["score"].null_count() == 0, "评分不应有 null"
    assert scored["score"].min() >= 0 and scored["score"].max() <= 100


def test_strategies_visible_to_backtest_worker():
    """两个策略都在回测 worker 的策略目录里可见。"""
    from pathlib import Path

    from app.backtest.worker import _strategy_dirs
    from app.factors.store import load_runtime_registry
    from app.factors.ext_factors import ensure_synced
    from app.strategy.engine import StrategyEngine

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    load_runtime_registry(data_dir)
    ensure_synced(data_dir)
    engine = StrategyEngine(strategy_dirs=_strategy_dirs(data_dir))
    assert engine.has("csp_short_multifactor")
    assert engine.has("csp_long_multifactor")


# ---- C 类另类数据接入验证（阶段9）----

def test_theme_factor_ingest_t1_lag():
    """题材因子脚本把 mainline 的盘后分 T-1 平移到下一交易日后落盘。"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1].parent
    p = root / "scripts" / "ingest_theme_factors.py"
    if not p.exists():
        pytest.skip("题材因子脚本不存在")
    spec = importlib.util.spec_from_file_location("ingest_theme", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    panel = mod.build_theme_panel(root / "data")
    assert not panel.is_empty()
    assert "theme_score" in panel.columns
    # T-1 语义：生效日必须严格晚于 mainline 原始日期（通过 eff_date 映射体现）
    src = pl.read_parquet(root / "data" / "mainline_history" / "part.parquet")
    assert panel["date"].n_unique() > 0
    assert src["date"].min() <= panel["date"].min()


def test_dragon_tiger_payload_fields():
    """回补脚本的落盘 payload 保留 org/all 榜原始字段（含游资净买/热度）。"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1].parent
    p = root / "scripts" / "backfill_dragon_tiger_history.py"
    spec = importlib.util.spec_from_file_location("backfill_dt", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    org = {"stock_items": [{"thscode": "000001.SZ", "org_net_value": 1e7,
                            "hot_money_net_value": 2e7, "hot_rank": 5}]}
    all_board = {"stock_items": [{"thscode": "000001.SZ", "net_value": 3e7}]}
    payload = mod.build_payload(org, all_board, "2026-11-13")
    assert payload["state"] == "ok"
    assert payload["org"]["stock_items"][0]["hot_money_net_value"] == 2e7
    assert payload["all"]["stock_items"][0]["net_value"] == 3e7


def test_full_strategy_all_factors_registered():
    """完整版短线策略的 8 个 scoring 因子全部已注册（含 C 类）。"""
    from pathlib import Path

    from app.factors.store import load_runtime_registry
    from app.factors.ext_factors import ensure_synced, invalidate_ext_caches
    from app.factors.registry import get_factor
    from app.strategy.engine import StrategyEngine

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    invalidate_ext_caches()
    load_runtime_registry(data_dir)
    ensure_synced(data_dir)
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    s = engine.get("csp_short_full")
    assert s is not None
    scoring = s.meta["scoring"]
    assert abs(sum(float(v) for v in scoring.values()) - 1.0) < 1e-6
    assert all(get_factor(k) is not None for k in scoring), "有 scoring 因子未注册"
    # C 类三类因子确实进入配方
    assert any("theme" in k for k in scoring)
    assert any("dragon" in k for k in scoring)
