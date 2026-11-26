"""权重校准工具的单元测试（纯函数，不跑回测）。

验证双闸门工具的核心逻辑：单纯形网格、稳健分、样本充分性判定。
校准脚本位于 scripts/（运行时工具，随 data 目录约定不进核心包）。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1].parent / "scripts" / "calibrate_scoring_weights.py"


@pytest.fixture(scope="module")
def calib():
    spec = importlib.util.spec_from_file_location("calibrate_scoring_weights", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_simplex_weights_sum_to_one(calib):
    combos = calib.simplex_weights(["a", "b", "c"], 0.25)
    assert combos
    assert all(abs(sum(c.values()) - 1.0) < 1e-9 for c in combos)


def test_simplex_weights_complete_and_unique(calib):
    for step in (0.5, 0.25):
        combos = calib.simplex_weights(["a", "b", "c", "d"], step)
        n = len(combos)
        expected = math.comb(round(1 / step) + 4 - 1, 4 - 1)
        assert n == expected
        assert len({tuple(sorted(c.items())) for c in combos}) == n


def test_simplex_weights_rejects_bad_step(calib):
    with pytest.raises(ValueError):
        calib.simplex_weights(["a", "b"], 0.3)
    with pytest.raises(ValueError):
        calib.simplex_weights(["a"], 0.5)


def test_robust_score_penalizes_worst(calib):
    # 相同中位数、不同最差段 → 最差段高的稳健分更高
    a = calib.robust_score([1.0, 1.0, 1.0])
    b = calib.robust_score([1.0, 1.0, 0.0])
    assert a["score"] > b["score"]
    assert b["worst"] == 0.0


def test_robust_score_none_is_honest(calib):
    r = calib.robust_score([None, None])
    assert r["score"] == float("-inf")
    assert r["median"] is None


def test_adequacy_flags_short_span(calib):
    short = calib.adequacy(385, 3, 120)  # ~13 个月
    assert not short["statistically_sufficient"]
    assert short["issues"]
    long = calib.adequacy(1000, 4, 120)
    assert long["statistically_sufficient"]
    assert long["issues"] == []
