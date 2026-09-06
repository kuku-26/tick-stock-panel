# -*- coding: utf-8 -*-
"""问财实盘模拟 — 账户更新 / 手动交易 API 测试（直接调用端点函数 + 注入临时 store）。"""
from __future__ import annotations

import pytest

from app.paper import context
from app.paper.api import (AccountUpdateModel, ManualTradeModel,
                           manual_trade, update_account)
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