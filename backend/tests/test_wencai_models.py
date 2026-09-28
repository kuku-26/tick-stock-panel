# -*- coding: utf-8 -*-
"""问财实盘模拟 — 卖出规则参数校验（止损/止盈符号与区间）。

独立测试文件（test_wencai_*）：不改动上游 test_paper_*.py，便于 fork 同步。
止损/止盈线价 = 基准价 × (1 + pct)，符号填反会把线价放到现价另一侧从而恒触发，
故必须强制：止损为负、止盈为正。
"""
from __future__ import annotations

import pytest

from app.wencai.models import SellRule


# ── 合法值放行 ──────────────────────────────────────────
def test_sell_rule_accepts_valid_signs():
    r = SellRule.from_dict({
        "stop_loss_pct": -0.05,
        "stop_loss_prev_close_pct": -0.03,
        "take_profit_pct": 0.2,
        "take_profit_prev_close_pct": 0.1,
    })
    assert r.stop_loss_pct == -0.05
    assert r.stop_loss_prev_close_pct == -0.03
    assert r.take_profit_pct == 0.2
    assert r.take_profit_prev_close_pct == 0.1


def test_sell_rule_optional_ratios_default_none():
    r = SellRule.from_dict({})
    assert r.stop_loss_pct is None
    assert r.take_profit_pct is None
    r2 = SellRule.from_dict({"stop_loss_pct": "", "take_profit_pct": None})
    assert r2.stop_loss_pct is None
    assert r2.take_profit_pct is None


# ── 符号校验 ────────────────────────────────────────────
@pytest.mark.parametrize("key", ["stop_loss_pct", "stop_loss_prev_close_pct"])
@pytest.mark.parametrize("value", [0.05, 1, 0])
def test_sell_rule_rejects_non_negative_stop_loss(key, value):
    with pytest.raises(ValueError, match="必须为负数"):
        SellRule.from_dict({key: value})


@pytest.mark.parametrize("key", ["take_profit_pct", "take_profit_prev_close_pct"])
@pytest.mark.parametrize("value", [-0.2, 0])
def test_sell_rule_rejects_non_positive_take_profit(key, value):
    with pytest.raises(ValueError, match="必须为正数"):
        SellRule.from_dict({key: value})


# ── 区间校验（小数制，须在 (-1, 100)） ───────────────────
@pytest.mark.parametrize("payload", [
    {"stop_loss_pct": -1},
    {"stop_loss_pct": -1.5},
    {"take_profit_pct": 100},
    {"take_profit_pct": 120},
])
def test_sell_rule_rejects_out_of_range(payload):
    with pytest.raises(ValueError, match="区间"):
        SellRule.from_dict(payload)


# ── 其余字段校验 ────────────────────────────────────────
def test_sell_rule_rejects_bad_sell_time():
    with pytest.raises(ValueError, match="sell_time"):
        SellRule.from_dict({"sell_time": "midnight"})


def test_sell_rule_rejects_non_positive_hold_days():
    with pytest.raises(ValueError, match="max_hold_days"):
        SellRule.from_dict({"max_hold_days": 0})


def test_sell_rule_rejects_non_str_exit_signals():
    with pytest.raises(ValueError, match="exit_signal_ids"):
        SellRule.from_dict({"exit_signal_ids": [1, 2]})


# ── 往返一致 ────────────────────────────────────────────
def test_sell_rule_roundtrip_preserves_signs():
    raw = {"stop_loss_pct": -0.08, "take_profit_pct": 0.3,
           "max_hold_days": 3, "sell_time": "next_open"}
    out = SellRule.from_dict(raw).to_dict()
    assert out["stop_loss_pct"] == -0.08
    assert out["take_profit_pct"] == 0.3
    assert out["max_hold_days"] == 3
    assert out["sell_time"] == "next_open"