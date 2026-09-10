# -*- coding: utf-8 -*-
"""问财实盘模拟 — 交易引擎与数据模型测试（使用假行情，不依赖仓库/网络）。"""
from __future__ import annotations

import dataclasses
import json

import pytest

from app.paper import context
from app.paper.market import DayRow, MarketData
from app.paper.models import (Account, BuyRule, PaperStrategy, SellRule,
                              LOT, Position)
from app.paper.scheduler import PaperScheduler, _simulate
from app.paper.service import MarketDataNotReadyError, run_simulate
from app.paper.store import PaperStore
from app.paper.trading import (_exit_fill_price, decide_exit,
                               entry_signal_passes, process_day)


class FakeMarket:
    """内存假行情：{date: {symbol: DayRow}} + 涨停/跌停价表。"""

    def __init__(self, rows=None, limit_up=None, limit_down=None):
        self.rows = rows or {}
        self.limit_up = limit_up or {}
        self.limit_down = limit_down or {}
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

    def sellable_at_open(self, symbol: str, row: DayRow | None):
        if row is None or row.close is None or row.close <= 0:
            return False
        lim = self.limit_down.get(symbol)
        return not (lim and row.open is not None and row.open > 0
                    and row.open <= lim + 0.001)


def r(symbol, open_, close, volume=100000, csg=None, high=None, low=None, prev_close=None):
    return DayRow(symbol=symbol, open=open_, close=close, volume=volume, csg=csg or {},
                  high=high, low=low, prev_close=prev_close)


def make_account(cash=100000):
    return Account.create("acc1", "测试账户", cash)


def make_strategy(**kw):
    defaults = dict(strategy_id="strat1", name="策略", account_id="acc1",
                    iwencai_query="query")
    defaults.update(kw)
    return PaperStrategy.create(**defaults)


def test_strategy_created_at_backfill(tmp_path):
    """created_at: 新建策略取今天; 历史记录(无 created_at)按最早落盘日期回填并持久化。"""
    from app.paper.store import PaperStore
    store = PaperStore(tmp_path)
    s = make_strategy()
    assert s.created_at, "create() 应写入创建日期"
    store.save_strategies({"strat1": s})
    strat_file = tmp_path / "paper" / "strategies.json"
    # 模拟历史数据: 抹掉 created_at, 最早每日快照为 2026-01-02
    raw = json.loads(strat_file.read_text(encoding="utf-8"))
    raw[0].pop("created_at")
    strat_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "paper" / "days" / "2026-01-02").mkdir(parents=True)
    (tmp_path / "paper" / "days" / "2026-01-02" / "strat1.json").write_text("{}", encoding="utf-8")
    loaded = store.load_strategies()
    assert loaded["strat1"].created_at == "2026-01-02", "应按最早落盘日期回填"
    # 回填结果已持久化, 二次加载稳定
    again = json.loads(strat_file.read_text(encoding="utf-8"))
    assert again[0]["created_at"] == "2026-01-02"
    assert store.load_strategies()["strat1"].created_at == "2026-01-02"


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


def test_exit_prev_close_stop_loss():
    """止损-前收: 盘中 low 触及 前收×(1+pct) 即触发; 无前收则不判定该条件。"""
    s = make_strategy()
    s.sell_rule = SellRule(stop_loss_prev_close_pct=-0.05)
    pos = Position("000001", 100, 10, "2026-01-01", hold_days=0)
    # prev_close=10 → 线 9.5; low 9.4 触及触发
    assert decide_exit(s, pos, r("000001", 9.6, 9.45, low=9.4, prev_close=10.0),
                       "2026-01-02") == "stop_loss_prev"
    # low 9.6 未触及线 9.5
    assert decide_exit(s, pos, r("000001", 9.8, 9.7, low=9.6, prev_close=10.0),
                       "2026-01-02") is None
    # 无前收（prev_close 缺失/非正）→ 该条件不判定
    assert decide_exit(s, pos, r("000001", 9.0, 9.0, low=9.0), "2026-01-02") is None
    assert decide_exit(s, pos, r("000001", 9.0, 9.0, low=9.0, prev_close=0),
                       "2026-01-02") is None


def test_exit_prev_close_fill_price():
    """止损-前收按线价成交, 开盘跳空穿越线位时按开盘价成交。"""
    rule = SellRule(stop_loss_prev_close_pct=-0.05)
    s = make_strategy()
    s.sell_rule = rule
    pos = Position("000001", 100, 10, "2026-01-01")
    # 盘中触发: 开盘 9.7 高于线 9.5 → 按线价 9.5 成交
    assert _exit_fill_price(rule, pos, r("000001", 9.7, 9.4, low=9.4, prev_close=10.0),
                            "stop_loss_prev") == pytest.approx(9.5)
    # 跳空低开穿越线位: 开盘 9.2 → 按开盘价 9.2 成交
    assert _exit_fill_price(rule, pos, r("000001", 9.2, 9.1, low=9.1, prev_close=10.0),
                            "stop_loss_prev") == pytest.approx(9.2)


def test_exit_prev_close_take_profit():
    """止盈-前收: 盘中 high 触及 前收×(1+pct) 即触发; 无前收则不判定该条件。"""
    s = make_strategy()
    s.sell_rule = SellRule(take_profit_prev_close_pct=0.05)
    pos = Position("000001", 100, 10, "2026-01-01", hold_days=0)
    # prev_close=10 → 线 10.5; high 10.6 触及触发
    assert decide_exit(s, pos, r("000001", 10.2, 10.5, high=10.6, prev_close=10.0),
                       "2026-01-02") == "take_profit_prev"
    # high 10.4 未触及线 10.5
    assert decide_exit(s, pos, r("000001", 10.1, 10.3, high=10.4, prev_close=10.0),
                       "2026-01-02") is None
    # 无前收（prev_close 缺失/非正）→ 该条件不判定
    assert decide_exit(s, pos, r("000001", 10.8, 10.9, high=10.9), "2026-01-02") is None
    assert decide_exit(s, pos, r("000001", 10.8, 10.9, high=10.9, prev_close=0),
                       "2026-01-02") is None


def test_exit_prev_close_take_profit_fill_price():
    """止盈-前收按线价成交, 开盘跳空高开穿越线位时按开盘价成交。"""
    rule = SellRule(take_profit_prev_close_pct=0.05)
    pos = Position("000001", 100, 10, "2026-01-01")
    # 盘中触发: 开盘 10.2 低于线 10.5 → 按线价 10.5 成交
    assert _exit_fill_price(rule, pos, r("000001", 10.2, 10.6, high=10.6, prev_close=10.0),
                            "take_profit_prev") == pytest.approx(10.5)
    # 跳空高开穿越线位: 开盘 10.8 → 按开盘价 10.8 成交
    assert _exit_fill_price(rule, pos, r("000001", 10.8, 10.9, high=10.9, prev_close=10.0),
                            "take_profit_prev") == pytest.approx(10.8)


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
    """process_day 应把策略买卖规则引用的信号集传给 day_rows(按需计算 csg 列)。"""
    mk = FakeMarket({"2026-01-02": {"000001": r("000001", 10, 10)}})
    acct = make_account()
    s = make_strategy()
    s.buy_rule.signal_ids = ["sig_a"]
    s.sell_rule.exit_signal_ids = ["out_b"]
    process_day(mk, acct, s, "2026-01-02", candidates=[])
    assert mk.day_rows_calls == [("2026-01-02", ("out_b", "sig_a"))]


# ── 代码格式对齐(问财 6 位码 vs 行情带后缀) ────────────


def test_process_day_aligns_iwencai_code_to_market_symbol():
    """问财候选/字段为 6 位码、行情键带交易所后缀时, 结算应能正常买入。

    回归测试: 此前 rows.get(6位码) 全部落空导致静默零成交。
    """
    mk = FakeMarket({"2026-01-02": {
        "603976.SH": r("603976.SH", 36.1, 37.0),
        "000523.SZ": r("000523.SZ", 3.96, 4.0),
        "600371.SH": r("600371.SH", 2.5, 2.6),
    }})
    acct = make_account()
    s = make_strategy()
    s.buy_rule.buy_time = "same_open"
    s.buy_rule.sort_field = "dde_net"
    s.buy_rule.sort_order = "desc"
    s.buy_rule.top_n = 3
    s.buy_rule.max_position_pct = 0.5
    cands = ["603976", "000523", "600371"]
    iwencai_rows = {
        "603976": {"dde_net": 0.012, "name": "正川股份"},
        "000523": {"dde_net": 0.28, "name": "红棉股份"},
        "600371": {"dde_net": 0.054, "name": "华远地产"},
    }
    trades, _ = process_day(mk, acct, s, "2026-01-02", cands, iwencai_rows)
    bought = {t.symbol for t in trades if t.side == "buy"}
    assert bought == {"603976.SH", "000523.SZ", "600371.SH"}, \
        "候选与字段代码应自动对齐到行情格式并完成买入"


# ── 一字板/开盘涨停不买入 ────────────────────────────────


def test_market_buyable_at_open_rejects_one_word_board(tmp_path):
    """一字板(最高=最低, 全天封死)与开盘涨停都不可买。"""
    mk = MarketData(tmp_path / "no_such_data")
    one_word = DayRow(symbol="000523.SZ", open=3.96, close=3.96,
                      volume=1000, high=3.96, low=3.96)
    assert mk.buyable_at_open("000523.SZ", one_word) is False, "一字板应不可买"
    normal = DayRow(symbol="600000.SH", open=10.0, close=10.5,
                    volume=1000, high=10.8, low=9.9)
    assert mk.buyable_at_open("600000.SH", normal) is True


def test_same_open_skips_open_limit_up(tmp_path):
    """same_open 买入时开盘涨停/一字板跳过, 其余候选照常买入。"""
    mk = FakeMarket(
        {"2026-01-02": {
            "000523": r("000523", 3.96, 3.96, volume=1000),
            "600000": r("600000", 10.0, 10.2, volume=1000),
        }},
        limit_up={"000523": 3.96},
    )
    acct = make_account()
    s = make_strategy()
    s.buy_rule.buy_time = "same_open"
    s.buy_rule.sort_field = "dde_net"
    s.buy_rule.sort_order = "desc"
    s.buy_rule.top_n = 2
    s.buy_rule.max_position_pct = 0.5
    trades, snap = process_day(
        mk, acct, s, "2026-01-02", ["000523", "600000"],
        {"000523": {"dde_net": 0.9}, "600000": {"dde_net": 0.1}})
    bought = {t.symbol for t in trades if t.side == "buy"}
    assert bought == {"600000"}, "开盘涨停的 000523 应被跳过"
    assert "000523" not in snap.positions


# ── 卖出: 开盘跌停不卖 / 止损止盈线价 / sell_time=open ──


def test_sell_blocked_when_open_at_limit_down():
    """开盘即封跌停的持仓当日不卖（跌停无法成交），继续持有。"""
    # 成本 10，收盘 8.5 触发止损(-0.1 线 9.0)；但开盘 8.5 封跌停(跌停价 9.0? 否，
    # 用 prev_close=10 → 跌停 9.0，open=9.0 封死) —— 用 prev_close 10/open 9/close 8.5
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
        "2026-01-04": {"000001": r("000001", 9.0, 8.5, prev_close=9.44)},
    }, limit_down={"000001": 9.0})
    acct = make_account()
    s = make_strategy()
    s.sell_rule = SellRule(stop_loss_pct=-0.1)
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    process_day(mk, acct, s, "2026-01-03", candidates=[])  # 买入建仓
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    assert [t for t in trades if t.side == "sell"] == [], "开盘封跌停当日不可卖"
    assert "000001" in acct.positions, "持仓应保留到下一交易日"


def test_market_sellable_at_open_rejects_limit_down(tmp_path):
    """真 MarketData: 开盘触及跌停价不可卖, 正常开盘可卖。"""
    mk = MarketData(tmp_path / "no_such_data")
    limit_down = DayRow(symbol="600000.SH", open=9.0, close=8.8, volume=1000,
                        high=9.2, low=8.8, prev_close=10.0)
    assert mk.sellable_at_open("600000.SH", limit_down) is False, "开盘=跌停价应不可卖"
    normal = DayRow(symbol="600000.SH", open=9.5, close=9.6, volume=1000,
                    high=9.8, low=9.3, prev_close=10.0)
    assert mk.sellable_at_open("600000.SH", normal) is True


def test_stop_loss_fills_at_line_price():
    """止损: 盘中跌破线(触发) → 成交价=止损线价, 而非收盘价。"""
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
        # open 10.5 > 线 9.0, low 8.7 触线, 收盘 9.4 > 线
        "2026-01-04": {"000001": r("000001", 10.5, 9.4, high=10.8, low=8.7)},
    })
    acct = make_account()
    s = make_strategy()
    s.buy_rule.buy_time = "same_open"  # 第一天(01-02)即以开盘价 10 买入, 成本线按 10 推算
    s.sell_rule = SellRule(stop_loss_pct=-0.1)  # 线价 = 10 × 0.9 = 9.0
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    process_day(mk, acct, s, "2026-01-03", candidates=[])
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    sells = [t for t in trades if t.side == "sell"]
    assert len(sells) == 1 and sells[0].reason == "stop_loss"
    assert sells[0].price == pytest.approx(9.0), "应按止损线价成交"
    assert sells[0].price != pytest.approx(9.4), "不应按收盘价成交"


def test_take_profit_fills_at_line_price():
    """止盈: 盘中触线(触发) → 成交价=止盈线价; 收盘回落也不影响。"""
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
        # open 10.5, high 12.5 触及止盈线 12.0, 收盘回落 11.2
        "2026-01-04": {"000001": r("000001", 10.5, 11.2, high=12.5, low=10.4)},
    })
    acct = make_account()
    s = make_strategy()
    s.buy_rule.buy_time = "same_open"  # 第一天(01-02)即以开盘价 10 买入, 成本线按 10 推算
    s.sell_rule = SellRule(take_profit_pct=0.2)  # 线价 = 10 × 1.2 = 12.0
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    process_day(mk, acct, s, "2026-01-03", candidates=[])
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    sells = [t for t in trades if t.side == "sell"]
    assert len(sells) == 1 and sells[0].reason == "take_profit"
    assert sells[0].price == pytest.approx(12.0), "应按止盈线价成交"


def test_gap_through_line_fills_at_open():
    """跳空穿越线位按开盘价成交: 低开破止损线按 open(更低), 高开破止盈线按 open(更高)。"""
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
        # 跳空低开 8.5 < 止损线 9.0
        "2026-01-04": {"000001": r("000001", 8.5, 8.6, high=9.1, low=8.4)},
    })
    acct = make_account()
    s = make_strategy()
    s.sell_rule = SellRule(stop_loss_pct=-0.1)
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    process_day(mk, acct, s, "2026-01-03", candidates=[])
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    sells = [t for t in trades if t.side == "sell"]
    assert sells[0].reason == "stop_loss"
    assert sells[0].price == pytest.approx(8.5), "跳空低开应按开盘价成交(比线价更差)"

    # 跳空高开破止盈线: open 12.6 > 线 12.0 → 按 12.6 成交
    mk2 = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
        "2026-01-04": {"000001": r("000001", 12.6, 12.1, high=12.8, low=11.9)},
    })
    acct2 = make_account()
    s2 = make_strategy()
    s2.sell_rule = SellRule(take_profit_pct=0.2)
    process_day(mk2, acct2, s2, "2026-01-02", candidates=["000001"])
    process_day(mk2, acct2, s2, "2026-01-03", candidates=[])
    trades2, _ = process_day(mk2, acct2, s2, "2026-01-04", candidates=[])
    sells2 = [t for t in trades2 if t.side == "sell"]
    assert sells2[0].reason == "take_profit"
    assert sells2[0].price == pytest.approx(12.6), "跳空高开应按开盘价成交(比线价更好)"


def test_max_hold_days_1_sells_next_open():
    """持股天数=1 + sell_time=open: 第一天买入, 第二天以开盘价卖出。"""
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.7, 11)},
    })
    acct = make_account(cash=10000)
    s = make_strategy()
    s.buy_rule.buy_time = "same_open"  # 第一天(01-02)即买入建仓
    s.buy_rule.max_position_pct = 0.5
    s.sell_rule = SellRule(max_hold_days=1, sell_time="open")
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])   # 买入日
    trades, _ = process_day(mk, acct, s, "2026-01-03", candidates=[])  # 次日
    sells = [t for t in trades if t.side == "sell"]
    assert len(sells) == 1 and sells[0].reason == "max_hold"
    assert sells[0].price == pytest.approx(10.7), "持股天数=1 应在次日以开盘价卖出"
    assert not acct.positions


def test_roll_after_sell_same_day():
    """滚仓: 结算先卖后买, 当日卖出释放的资金与仓位额度同日即可再买入新标的。

    Day1 建仓 2 只（总仓位上限 0.5, 单股上限 0.2, same_open 买入）;
    Day2 max_hold_days=1 触发全部卖出(sell_time=open), 同日应能买入 2 只新标的。
    """
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10), "000002": r("000002", 20, 20)},
        "2026-01-03": {"000001": r("000001", 10, 10), "000002": r("000002", 20, 20),
                       "000003": r("000003", 10, 10), "000004": r("000004", 20, 20)},
    })
    acct = make_account(cash=10000)
    s = make_strategy()
    s.buy_rule.max_total_pct = 0.5
    s.buy_rule.max_position_pct = 0.2
    s.buy_rule.buy_time = "same_open"
    s.sell_rule = SellRule(max_hold_days=1, sell_time="open")
    process_day(mk, acct, s, "2026-01-02", candidates=["000001", "000002"])
    assert len(acct.positions) == 2
    trades, _ = process_day(mk, acct, s, "2026-01-03", candidates=["000003", "000004"])
    sells = [t for t in trades if t.side == "sell"]
    buys = [t for t in trades if t.side == "buy"]
    assert len(sells) == 2 and len(buys) == 2, "卖出释放额度后同日应能再买入新标的"
    assert {b.symbol for b in buys} == {"000003", "000004"}
    # 再买入总额不超过总仓位上限 (0.5 × 总资产 10000 = 5000)
    assert sum(b.amount for b in buys) <= 5000 + 1e-6


def test_partial_sell_frees_quota_for_rebuy():
    """部分卖出同样释放额度: 卖 1 只后, 剩余持仓 + 新买入 ≤ 总仓位上限。

    单股上限按总资产口径: 卖出释放资金后, 新买单可按 max_position_pct × 总资产 全额配置。
    """
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10), "000002": r("000002", 20, 20)},
        "2026-01-03": {"000001": r("000001", 10, 10), "000002": r("000002", 20, 20),
                       "000003": r("000003", 10, 10)},
        # 000001 止损: prev_close 10, 线 9.5, low 9.4 触发
        "2026-01-04": {"000001": r("000001", 9.7, 9.3, low=9.4, prev_close=10.0),
                       "000002": r("000002", 20, 20),
                       "000003": r("000003", 10, 10)},
    })
    acct = make_account(cash=10000)
    s = make_strategy()
    s.buy_rule.max_total_pct = 0.5
    s.buy_rule.max_position_pct = 0.2
    s.buy_rule.buy_time = "same_open"
    s.sell_rule = SellRule(stop_loss_prev_close_pct=-0.05, max_hold_days=5)
    process_day(mk, acct, s, "2026-01-02", candidates=["000001", "000002"])
    process_day(mk, acct, s, "2026-01-03", candidates=[])
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=["000003"])
    sells = [t for t in trades if t.side == "sell"]
    buys = [t for t in trades if t.side == "buy"]
    assert len(sells) == 1 and sells[0].symbol == "000001"
    assert len(buys) == 1 and buys[0].symbol == "000003", "止损卖出释放的额度应可同日再买入"
    # 总投入不超过 0.5 × 总资产
    total_value = acct.cash + sum(
        p.qty * (10 if p.symbol == "000003" else 20) for p in acct.positions.values())
    invested = total_value - acct.cash
    assert invested <= total_value * 0.5 + 1e-6


def test_exit_signal_sell_time_open():
    """sell_time=open 时信号离场也按结算日开盘价成交。"""
    mk = FakeMarket({
        "2026-01-02": {"000001": r("000001", 10, 10)},
        "2026-01-03": {"000001": r("000001", 10.2, 11)},
        "2026-01-04": {"000001": r("000001", 11.3, 11.0, csg={"csg_out": True})},
    })
    acct = make_account()
    s = make_strategy()
    s.sell_rule = SellRule(exit_signal_ids=["out"], sell_time="open")
    process_day(mk, acct, s, "2026-01-02", candidates=["000001"])
    process_day(mk, acct, s, "2026-01-03", candidates=[])
    trades, _ = process_day(mk, acct, s, "2026-01-04", candidates=[])
    sells = [t for t in trades if t.side == "sell"]
    assert sells[0].reason == "exit_signal"
    assert sells[0].price == pytest.approx(11.3), "sell_time=open 应按开盘价成交"


def test_sell_rule_rejects_bad_sell_time():
    with pytest.raises(ValueError):
        SellRule.from_dict({"sell_time": "noon"})