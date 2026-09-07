# -*- coding: utf-8 -*-
"""问财实盘模拟 — 交易引擎与数据模型测试（使用假行情，不依赖仓库/网络）。"""
from __future__ import annotations

import dataclasses

import pytest

from app.paper import context
from app.paper.market import DayRow
from app.paper.models import (Account, BuyRule, PaperStrategy, SellRule,
                              LOT, Position)
from app.paper.scheduler import PaperScheduler, _simulate
from app.paper.service import MarketDataNotReadyError, run_simulate
from app.paper.store import PaperStore
from app.paper.trading import decide_exit, entry_signal_passes, process_day


class FakeMarket:
    """内存假行情：{date: {symbol: DayRow}} + 涨停价表。"""

    def __init__(self, rows=None, limit_up=None):
        self.rows = rows or {}
        self.limit_up = limit_up or {}
        self.day_rows_calls: list[tuple[str, tuple[str, ...]]] = []

    def day_rows(self, date: str, signal_ids=None):
        self.day_rows_calls.append((date, tuple(sorted(signal_ids or ()))))
        return self.rows.get(date, {})

    def symbol_limit_up(self, symbol: str):
        return self.limit_up.get(symbol)

    def buyable_at_open(self, symbol: str, row: DayRow | None):
        if row is None or row.open is None or row.open <= 0 or row.volume <= 0:
            return False
        lim = self.symbol_limit_up(symbol)
        if lim and row.open >= lim - 0.001:
            return False
        return True

    def sellable_at_close(self, row: DayRow | None):
        return bool(row and row.close and row.close > 0)


def r(symbol, open_, close, volume=100000, csg=None):
    return DayRow(symbol=symbol, open=open_, close=close, volume=volume, csg=csg or {})


def make_account(cash=100000):
    return Account.create("acc1", "测试账户", cash)


def make_strategy(**kw):
    defaults = dict(strategy_id="strat1", name="策略", account_id="acc1",
                    iwencai_query="query")
    defaults.update(kw)
    return PaperStrategy.create(**defaults)


# ── 信号求值 ────────────────────────────────────────────


def test_entry_no_signal_buys_all():
    s = make_strategy()
    assert entry_signal_passes(s, r("000001", 10, 10)) is True


def test_entry_signal_all_must_true():
    s = make_strategy()
    s.buy_rule.signal_ids = ["sig_a", "sig_b"]
    assert entry_signal_passes(s, r("000001", 10, 10, csg={"csg_sig_a": True, "csg_sig_b": True})) is True
    assert entry_signal_passes(s, r("000001", 10, 10, csg={"csg_sig_a": True, "csg_sig_b": False})) is False
    assert entry_signal_passes(s, r("000001", 10, 10, csg={"csg_sig_a": True})) is False


def test_exit_reasons():
    s = make_strategy()
    s.sell_rule = SellRule(stop_loss_pct=-0.1, take_profit_pct=0.2, max_hold_days=3)
    pos = Position("000001", 100, 10, "2026-01-01", hold_days=0)
    # 未触发任何条件
    assert decide_exit(s, dataclasses.replace(pos, **{"hold_days": 0}),
                       r("000001", 10, 11), "2026-01-02") is None
    # 止盈
    assert decide_exit(s, pos, r("000001", 10, 12.5), "2026-01-02") == "take_profit"
    # 止损
    assert decide_exit(s, pos, r("000001", 10, 8.9), "2026-01-02") == "stop_loss"
    # 最长持有
    assert decide_exit(s, dataclasses.replace(pos, **{"hold_days": 3}),
                       r("000001", 10, 11), "2026-01-02") == "max_hold"


def test_exit_signal_or_rule():
    s = make_strategy()
    s.sell_rule = SellRule(exit_signal_ids=["out"])
    assert decide_exit(s, Position("000001", 100, 10, "2026-01-01"),
                       r("000001", 10, 11, csg={"csg_out": True}), "2026-01-02") == "exit_signal"
    assert decide_exit(s, Position("000001", 100, 10, "2026-01-01"),
                       r("000001", 10, 11, csg={"csg_out": False}), "2026-01-02") is None


# ── 完整结算：买入（次日开盘） ────────────────────────────


def test_buy_fills_at_next_open():
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10.5)},
    })
    acct = make_account()
    s = make_strategy()
    trades, snap = process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    assert len(trades) == 0, "候选在当日生成，PB 应在次日开盘成交"
    assert len(snap.trades) == 0
    assert len(acct.pending) == 1
    assert acct.pending[0].symbol == "000001"

    mk2 = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10.5)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
    })
    trades, snap = process_day(mk2, acct, s, "2026-01-03", candidates=[])
    buys = [t for t in trades if t.side == "buy"]
    assert len(buys) == 1
    assert buys[0].price == 10.2, "次日开盘价成交"
    # 单一致策略默认 max_position_pct=0.2 → 预算20000，10.5定价 → 1900股
    assert acct.positions["000001"].qty == 1900
    assert acct.positions["000001"].qty % LOT == 0
    assert round(acct.positions["000001"].avg_cost, 2) == round(10.2, 2)
    assert acct.cash == 100000 - 1900 * 10.2


def test_buy_skips_suspended_and_limit_up():
    # 000001 停牌（下一日缺失）、000002 涨停被跳过
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10), "000002": r("000002", 10, 10)},
        "2026-01-03": {"000002": r("000002", 12, 12)},
    }, limit_up={"000002": 12.0})
    acct = make_account()
    s = make_strategy()
    s.buy_rule.max_symbols = 0
    process_day(mk, acct, s, "2026-01-02", candidates=["000001", "000002"])
    assert len(acct.pending) == 2
    trades, _ = process_day(mk, acct, s, "2026-01-03", candidates=[])
    # 000001 停牌继续挂单；000002 开盘=12=涨停 → 跳过
    assert [t.symbol for t in trades if t.side == "buy"] == []
    assert {p.symbol for p in acct.pending} == {"000001", "000002"}
    assert acct.cash == 100000


# ── 卖出 ────────────────────────────────────────────────


def test_sell_at_close_and_t1():
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10, 12)},
        "2026-01-04": {"000001": r("000001", 10, 9)},
    })
    acct = make_account()
    s = make_strategy()
    s.sell_rule = SellRule(stop_loss_pct=-0.1, take_profit_pct=0.2)

    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])  # 生成PB
    _t, sep = process_day(mk, acct, s, "2026-01-03", candidates=[])  # 10.2? 买入
    # T+1：当天买入（entry_date=2026-01-03）不能在 01-03 卖出
    assert [t.side for t in sep.trades] == ["buy"]
    assert "000001" in acct.positions
    # 01-04：成本10 → 收盘9 → 跌停? close9 相对成本-10% 触发止损
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    sells = [t for t in trades if t.side == "sell"]
    assert len(sells) == 1
    assert sells[0].reason == "stop_loss"
    assert sells[0].price == 9
    assert "000001" not in acct.positions
    # 2000股@10买入(20000) → 2000股@9卖出(18000)，亏损
    assert acct.cash == 100000 - 2000 * 10 + 2000 * 9


def test_exit_signal_sell():
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
        "2026-01-04": {"000001": r("000001", 11, 11, csg={"csg_out": True})},
    })
    acct = make_account()
    s = make_strategy()
    s.sell_rule = SellRule(exit_signal_ids=["out"])
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    process_day(mk, acct, s, "2026-01-03", candidates=[])
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    assert [t.reason for t in trades if t.side == "sell"] == ["exit_signal"]


# ── 结算与快照 ──────────────────────────────────────────


def test_day_snapshot_nav_and_hold_days():
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10, 11)},
    })
    acct = make_account(cash=10000)
    s = make_strategy()
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    _, snap = process_day(mk, acct, s, "2026-01-03", candidates=[])
    assert acct.positions["000001"].hold_days == 1
    # 单一致候选预算=min(10000, 10000*0.2)=2000，10元定价 → 200股
    assert acct.positions["000001"].qty == 200
    assert round(snap.market_value, 2) == round(200 * 11, 2)
    assert round(snap.total_value, 2) == round(acct.cash + 200 * 11, 2)
    assert snap.nav > 1.0


def test_pending_carries_over_holiday():
    # 候选 01-02，但 01-03 无数据（假日/停牌批量缺失），01-04 才成交
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-04": {"000001": r("000001", 15, 15)},
    })
    acct = make_account()
    s = make_strategy()
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    t1, _ = process_day(mk, acct, s, "2026-01-03", candidates=[])
    assert [x.side for x in t1] == [], "假日无行情，PB 继续挂单"
    assert len(acct.positions) == 0
    t2, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    assert [x.side for x in t2] == ["buy"]
    # 候选日在01-02定价(10元,预算20000)即确定2000股，成交日资金充足全量成交
    assert acct.positions["000001"].qty == 2000


# ── 模型/规则校验 ───────────────────────────────────────


def test_buy_rule_validation():
    with pytest.raises(ValueError):
        BuyRule.from_dict({"max_position_pct": 1.2})
    with pytest.raises(ValueError):
        BuyRule.from_dict({"signal_ids": "not-a-list"})
    with pytest.raises(ValueError):
        BuyRule.from_dict({"max_total_pct": 1.2})
    with pytest.raises(ValueError):
        BuyRule.from_dict({"sort_order": "sideways"})
    with pytest.raises(ValueError):
        BuyRule.from_dict({"top_n": -1})
    assert BuyRule.from_dict({}).max_position_pct == 0.2
    assert BuyRule.from_dict({}).sort_order == "desc"


def test_sell_rule_validation():
    with pytest.raises(ValueError):
        SellRule.from_dict({"stop_loss_pct": -1.5})
    with pytest.raises(ValueError):
        SellRule.from_dict({"max_hold_days": 0})


def test_lot_rounding():
    mk = FakeMarket({"2026-01-02": {"000001": r("000001", 10, 10)}})
    acct = make_account()
    s = make_strategy()
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    qty = acct.pending[0].qty
    assert qty % LOT == 0 and qty >= LOT


# ── 按字段排序 + TopN ────────────────────────────────────


def test_buy_sort_by_field_then_topn():
    mk = FakeMarket({"2026-01-02": {
        "000001": r("000001", 10, 10),
        "000002": r("000002", 10, 10),
        "000003": r("000003", 10, 10),
    }})
    acct = make_account()
    s = make_strategy()
    s.buy_rule = BuyRule.from_dict({
        "max_position_pct": 0.3, "max_symbols": 0,
        "sort_field": "dde_net", "sort_order": "desc", "top_n": 2,
    })
    iwencai = {
        "000001": {"dde_net": 3.0},
        "000002": {"dde_net": 9.0},
        "000003": {"dde_net": 1.5},
    }
    process_day(mk, acct, s, "2026-01-02",
                candidates=["000001", "000002", "000003"], iwencai_rows=iwencai)
    picks = [p.symbol for p in acct.pending]
    assert picks == ["000002", "000001"], "应按 dde_net 降序取前2"


def test_buy_sort_missing_field_dropped_last():
    mk = FakeMarket({"2026-01-02": {
        "000001": r("000001", 10, 10),
        "000002": r("000002", 10, 10),
    }})
    acct = make_account()
    s = make_strategy()
    s.buy_rule = BuyRule.from_dict(
        {"sort_field": "dde_net", "sort_order": "desc", "top_n": 0})
    iwencai = {"000002": {"dde_net": 5.0}}  # 000001 缺 dde_net
    process_day(mk, acct, s, "2026-01-02",
                candidates=["000001", "000002"], iwencai_rows=iwencai)
    assert [p.symbol for p in acct.pending] == ["000002", "000001"], "缺值的 000001 应排末尾（仍可选）"


# ── 总仓位上限（半仓） ───────────────────────────────────


def test_buy_total_pct_cap_half():
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10), "000002": r("000002", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10, 10), "000002": r("000002", 10, 10)},
    })
    acct = make_account(cash=100000)
    s = make_strategy()
    s.buy_rule = BuyRule.from_dict(
        {"max_position_pct": 1.0, "max_total_pct": 0.5, "max_symbols": 0})
    process_day(mk, acct, s, "2026-01-02", candidates=["000001", "000002"])
    invested = sum(p.qty * 10 for p in acct.pending)
    assert invested == 50000, "总仓位上限=半仓，首日投入应恰为 50000"

    # 次日 PB 成交后账户回到半仓，再次生成买入：已占满预算，不再新增
    _t, _sn = process_day(mk, acct, s, "2026-01-03", candidates=["000001", "000002"])
    assert acct.positions["000001"].qty == 5000
    assert not acct.pending, "已达半仓，不应新增买入"
    assert round(acct.cash, 2) == 50000.0


# ── 当日开盘价买入（buy_time = same_open） ───────────────


def test_buy_same_open_uses_today_open_price():
    mk = FakeMarket({"2026-01-02": {"000001": r("000001", 10, 12)}})
    acct = make_account()
    s = make_strategy()
    s.buy_rule = BuyRule.from_dict(
        {"buy_time": "same_open", "max_position_pct": 0.5, "max_symbols": 0})
    trades, _ = process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    buys = [t for t in trades if t.side == "buy"]
    assert len(buys) == 1
    assert buys[0].price == 10, "当日买入应按当日开盘价 10 成交，而非收盘价 12"
    assert buys[0].reason == "entry_fill"
    # 当日开盘价 10，预算 min(100000,100000*0.5)=50000 → 5000 股
    assert acct.positions["000001"].qty == 5000
    assert acct.positions["000001"].avg_cost == 10
    assert not acct.pending, "same_open 不应生成待买入单"
    assert round(acct.cash, 2) == 100000 - 5000 * 10


def test_buy_same_open_position_minus_marketvalue_nextopen_differs():
    # 当日开盘买入后，结算市值按当日收盘价评估（区别于买入价）
    mk = FakeMarket({"2026-01-02": {"000001": r("000001", 10, 12)}})
    acct = make_account()
    s = make_strategy()
    s.buy_rule = BuyRule.from_dict({"buy_time": "same_open", "max_position_pct": 0.5})
    _, snap = process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    assert round(snap.market_value, 2) == 5000 * 12, "持仓市值应按收盘价评估"


# ── 结算数据就绪检查（run_simulate） ─────────────────────


def _seed_store(tmp_path) -> PaperStore:
    store = PaperStore(tmp_path)
    store.save_accounts({"acc1": Account.create("acc1", "测试账户", 100000.0)})
    store.save_strategies({"strat1": PaperStrategy.create("strat1", "策略", "acc1", "query")})
    return store


def test_run_simulate_raises_when_day_rows_missing(tmp_path):
    """当日 enriched 未落盘：明确报错且不落任何结算产物（fail-closed）。"""
    store = _seed_store(tmp_path)
    s = store.load_strategies()["strat1"]
    with pytest.raises(MarketDataNotReadyError, match="尚未就绪"):
        run_simulate(store, FakeMarket({}), s, "2026-01-02")
    assert store.list_day_dates("strat1") == [], "未就绪时不应写日快照"
    assert store.load_trades("strat1") == []
    assert store.load_accounts()["acc1"].cash == 100000.0


def test_run_simulate_ok_when_day_rows_ready(tmp_path):
    store = _seed_store(tmp_path)
    s = store.load_strategies()["strat1"]
    mk = FakeMarket({"2026-01-02": {"000001": r("000001", 10, 11)}})
    result = run_simulate(store, mk, s, "2026-01-02",
                          fallback_candidates=["000001"])
    assert result["date"] == "2026-01-02"
    assert store.list_day_dates("strat1") == ["2026-01-02"]


# ── 调度器：结算未就绪跳过 / 就绪正常结算 ────────────────


def test_simulate_skips_when_data_not_ready(tmp_path):
    """数据未就绪时不抛异常、不落任何结算产物(调度器仅记告警)。"""
    store = _seed_store(tmp_path)
    mk = FakeMarket({})  # 当日无行情
    sched = PaperScheduler(store, mk)
    context.set_instances(store, mk, sched)
    sched.start()
    try:
        _simulate("strat1", date_str="2026-01-02")  # 不应抛出
        assert store.list_day_dates("strat1") == [], "未就绪时不应写日快照"
        assert store.load_trades("strat1") == []
    finally:
        sched.stop()


def test_simulate_settles_when_data_ready(tmp_path):
    store = _seed_store(tmp_path)
    mk = FakeMarket({"2026-01-02": {"000001": r("000001", 10, 11)}})
    sched = PaperScheduler(store, mk)
    context.set_instances(store, mk, sched)
    sched.start()
    try:
        _simulate("strat1", date_str="2026-01-02")
        assert store.list_day_dates("strat1") == ["2026-01-02"]
    finally:
        sched.stop()


def test_process_day_passes_strategy_signals_to_market():
    """process_day 应把策略买卖规则引用的信号集传给 day_rows（按需计算 csg 列）。"""
    mk = FakeMarket({"2026-01-02": {"000001": r("000001", 10, 10)}})
    acct = make_account()
    s = make_strategy()
    s.buy_rule.signal_ids = ["sig_a"]
    s.sell_rule.exit_signal_ids = ["out_b"]
    process_day(mk, acct, s, "2026-01-02", candidates=[])
    assert mk.day_rows_calls == [("2026-01-02", ("out_b", "sig_a"))]