# -*- coding: utf-8 -*-
"""问财实盘模拟 — 账户更新 / 手动交易 API 测试（直接调用端点函数 + 注入临时 store）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.paper import context
from app.paper.api import (AccountUpdateModel, ManualTradeModel,
                           account_detail, manual_trade, update_account)
from app.paper.market import DayRow
from app.paper.models import Account, PaperStrategy
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