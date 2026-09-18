# -*- coding: utf-8 -*-
"""问财实盘模拟 — 账户更新 / 手动交易 API 测试（直接调用端点函数 + 注入临时 store）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.paper import context
from app.paper.api import (AccountUpdateModel, ManualTradeModel,
                           account_detail, daily_records, manual_trade,
                           update_account)
from app.paper.market import DayRow
from app.paper.models import Account, DaySnapshot, PaperStrategy, TradeRecord
from app.paper.store import PaperStore


def _seed(tmp_path) -> PaperStore:
    store = PaperStore(tmp_path)
    context.set_instances(store, None, None)
    store.save_accounts({"acc1": Account.create("acc1", "测试账户", 100000.0)})
    store.save_strategies({"s1": PaperStrategy.create("s1", "策略", "acc1", "query")})
    return store


def test_update_account_renames_and_changes_initial_cash(tmp_path):
    _seed(tmp_path)
    update_account("acc1", AccountUpdateModel(name=" 新名字 ", initial_cash=50000.0), None)
    acc = context.get_store().load_accounts()["acc1"]
    assert acc.name == "新名字"
    assert acc.initial_cash == 50000.0
    # 初始资金变更不影响当前可用现金
    assert acc.cash == 100000.0

    with pytest.raises(Exception):
        update_account("acc1", AccountUpdateModel(name="x", initial_cash=0), None)


def test_manual_buy_and_sell(tmp_path):
    store = _seed(tmp_path)
    manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="buy", qty=100, price=10.0), None)
    acc = store.load_accounts()["acc1"]
    assert acc.positions["600000.SH"].qty == 100
    assert acc.positions["600000.SH"].avg_cost == 10.0
    assert round(acc.cash, 2) == 99000  # 100000 - 100*10
    # 手数限制：须为 100 的整倍
    with pytest.raises(Exception):
        manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="buy", qty=150, price=1.0), None)

    # 全部平仓卖出
    manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="sell", qty=100, price=11.0), None)
    acc = store.load_accounts()["acc1"]
    assert "600000.SH" not in acc.positions
    assert round(acc.cash, 2) == 99000 + 100 * 11.0

    # 已无持仓再卖出 → 报错
    with pytest.raises(Exception):
        manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="sell", qty=100, price=11.0), None)


def test_manual_trade_appends_record_to_bound_strategy(tmp_path):
    store = _seed(tmp_path)
    manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="buy", qty=100, price=10.0), None)
    trades = store.load_trades("s1")
    assert len(trades) == 1
    assert trades[0]["reason"] == "manual"
    assert trades[0]["symbol"] == "600000.SH"
    assert trades[0]["side"] == "buy"


def test_manual_buy_backfilled_date_recomputes_hold_days(tmp_path):
    """手动补录历史日期的买入应视同该日建仓重算 hold_days（与引擎/回放同口径）。

    回归场景: dde_01 手动补录 09-16 买入 000993 后 hold_days=0，导致
    max_hold_days=1 的持仓次日结算不卖出（晚一个交易日）。
    """
    store = _seed(tmp_path)
    acc = store.load_accounts()["acc1"]
    acc.last_record_date = "2026-09-16"
    store.save_accounts({"acc1": acc})
    for d in ("2026-09-15", "2026-09-16"):
        store.save_day(DaySnapshot("acc1", "s1", d, 100000.0, {}, 0.0, 100000.0, 1.0))

    # 补录 09-15 的买入: 经历 09-15/16 两个结算日 → hold_days=2
    manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="buy",
                                          qty=100, price=10.0, date="2026-09-15"), None)
    pos = store.load_accounts()["acc1"].positions["600000.SH"]
    assert pos.entry_date == "2026-09-15"
    assert pos.hold_days == 2, "09-15 建仓, 经历 09-15/16 两个结算日"

    # 补录日期晚于最后结算日（未结算）→ hold_days=0, 待当日结算后 +1
    manual_trade("acc1", ManualTradeModel(symbol="000001.SZ", side="buy",
                                          qty=100, price=10.0, date="2026-09-17"), None)
    pos2 = store.load_accounts()["acc1"].positions["000001.SZ"]
    assert pos2.hold_days == 0, "买入日期晚于最后结算日时为 0"

    # 加仓已有持仓不改变原 entry_date 与 hold_days
    manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="buy",
                                          qty=100, price=11.0, date="2026-09-16"), None)
    pos3 = store.load_accounts()["acc1"].positions["600000.SH"]
    assert pos3.qty == 200 and pos3.entry_date == "2026-09-15"
    assert pos3.hold_days == 2


def test_daily_records_aggregates_fetch_and_settlement(tmp_path):
    """/records 按日汇总各策略的选股名单与结算记录；未落盘项显式标记。"""
    store = _seed(tmp_path)
    d = "2026-09-18"
    store.save_iwencai_snapshot(d, "s1", {
        "strategy_id": "s1", "symbols": ["000001"], "count": 1,
        "fields": {"000001": {"name": "平安银行"}},
    })
    store.save_day(DaySnapshot("acc1", "s1", d, 99000.0, {}, 1000.0, 100000.0, 1.0,
                               trades=[TradeRecord("t1", "acc1", "s1", d, "000001.SZ",
                                                   "buy", 100, 10.0, 1000.0, "entry_fill")]))

    out = daily_records(None, d)
    assert out["date"] == d
    rec = out["records"][0]
    assert rec["strategy_id"] == "s1" and rec["name"] == "策略"
    # 选股：已落盘 + 名称映射
    assert rec["fetch"]["done"] is True
    assert rec["fetch"]["count"] == 1
    assert rec["fetch"]["symbols"][0] == {"symbol": "000001", "name": "平安银行"}
    # 结算：日快照存在，成交明细透传
    assert rec["settle"] is not None
    assert rec["settle"]["total_value"] == 100000.0
    assert rec["settle"]["trades"][0]["reason"] == "entry_fill"

    # 无任何落盘的日期：done=False / settle=None
    empty = daily_records(None, "2026-09-19")["records"][0]
    assert empty["fetch"]["done"] is False and empty["settle"] is None


def test_settle_now_guards(tmp_path):
    """手动结算防重入与行情未就绪保护。"""
    from datetime import date as _date

    from fastapi import HTTPException

    from app.paper.api import settle_now
    from app.paper.market import MarketData

    store = _seed(tmp_path)
    context.set_instances(store, MarketData(tmp_path), None)

    # 行情未就绪（空行情目录）→ 503
    with pytest.raises(HTTPException) as ei:
        settle_now("s1", None)
    assert ei.value.status_code == 503

    # 当日已结算 → 409 拒绝重复结算
    d = _date.today().isoformat()
    store.save_day(DaySnapshot("acc1", "s1", d, 100000.0, {}, 0.0, 100000.0, 1.0))
    with pytest.raises(HTTPException) as ei:
        settle_now("s1", None)
    assert ei.value.status_code == 409


# ── 账户详情：持仓名称/现价/收益率与流水名称 ──────────────


class _FakeRepo:
    def get_name_map(self, symbols=None):
        return {"600000.SH": "浦发银行"}


class _FakeMarket:
    def latest_date(self):
        return "2026-01-02"

    def day_rows(self, date, signal_ids=None):
        return {"600000.SH": DayRow(symbol="600000.SH", open=10.0,
                                    close=12.0, volume=1000)}


def test_account_detail_names_and_pnl(tmp_path):
    """持仓/流水应带名称, 持仓带最新价与收益率(相对成本价)。"""
    store = _seed(tmp_path)
    manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="buy",
                                          qty=100, price=10.0), None)
    context.set_instances(store, _FakeMarket(), None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        store=store, repo=_FakeRepo())))
    detail = account_detail("acc1", request)
    pos = detail["positions"][0]
    assert pos["name"] == "浦发银行"
    assert pos["last_price"] == 12.0
    assert pos["pnl_pct"] == 20.0  # (12-10)/10*100
    assert detail["trades"][0]["name"] == "浦发银行"


def test_account_detail_without_market_data(tmp_path):
    """无行情时 last_price/pnl_pct 为 null, 名称回退为代码, 不报错。"""
    store = _seed(tmp_path)
    manual_trade("acc1", ManualTradeModel(symbol="600000.SH", side="buy",
                                          qty=100, price=10.0), None)

    class _NoDataMarket:
        def latest_date(self):
            return None

        def day_rows(self, date, signal_ids=None):
            return {}

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        store=store, repo=_FakeRepo())))
    context.set_instances(store, _NoDataMarket(), None)
    detail = account_detail("acc1", request)
    pos = detail["positions"][0]
    assert pos["last_price"] is None and pos["pnl_pct"] is None
    assert pos["name"] == "浦发银行"