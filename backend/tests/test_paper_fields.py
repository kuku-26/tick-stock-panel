# -*- coding: utf-8 -*-
"""问财字段归一化、条件判定、OR 买入逻辑与级联删除测试。"""
from __future__ import annotations

import pytest

from app.paper.fields import (evaluate_filter, evaluate_filters, normalize_row,
                              normalize_rows)
from app.paper.market import DayRow
from app.paper.models import FieldFilter
from app.paper.store import PaperStore
from app.paper.trading import process_day

# ── 样例原始列（近似用户提供的问财返回） ─────────────────


def sample_raw() -> dict:
    return {
        "股票代码": "000560.SZ",
        "股票简称": "我爱我家",
        "最新价": 3.15,
        "dde大单净量[20260904]": 9.701544,
        "涨跌幅[20260824-20260904]": 41.26,
        "涨跌幅[20260831-20260904]": 19.32,
        "竞价涨幅[20260904]": 0.70,
        "量比": 1.51,
        "上市板块": "主板",
    }


# ── normalize_row / normalize_rows ────────────────────────


def test_normalize_row_maps_stable_keys():
    n = normalize_row(sample_raw())
    assert n["dde_net"] == pytest.approx(9.701544)
    assert n["auction_pct"] == pytest.approx(0.70)
    assert n["vol_ratio"] == pytest.approx(1.51)
    assert n["board"] == "主板"
    assert n["name"] == "我爱我家"


def test_normalize_return_period_by_weekdays():
    # 0831→0904 共 5 个工作日 → return_5d；0824→0904 共 10 个工作日 → return_10d
    n = normalize_row(sample_raw())
    assert n["return_5d"] == pytest.approx(19.32)
    assert n["return_10d"] == pytest.approx(41.26)


def test_normalize_rows_indexed_by_symbol():
    rows = [sample_raw(), {**sample_raw(), "股票代码": "600000.SH", "股票简称": "浦发银行"}]
    fm = normalize_rows(rows)
    assert set(fm) == {"000560", "600000"}
    assert fm["000560"]["name"] == "我爱我家"


def test_normalize_non_numeric_returns_none():
    n = normalize_row({**sample_raw(), "量比": "--"})
    assert n["vol_ratio"] is None


# ── evaluate_filter / evaluate_filters ─────────────────────


def test_evaluate_numeric_ops():
    norm = {"dde_net": 9.7, "auction_pct": 0.7, "vol_ratio": 1.5}
    assert evaluate_filter(norm, "dde_net", ">", 0) is True
    assert evaluate_filter(norm, "auction_pct", "<", 10) is True
    assert evaluate_filter(norm, "auction_pct", ">", 0) is True
    assert evaluate_filter(norm, "vol_ratio", ">", 0) is True
    assert evaluate_filter(norm, "dde_net", "<=", 0) is False


def test_evaluate_string_ops():
    norm = {"board": "主板", "name": "我爱我家"}
    assert evaluate_filter(norm, "board", "==", "主板") is True
    assert evaluate_filter(norm, "name", "not_contains", "ST") is True
    assert evaluate_filter(norm, "name", "contains", "ST") is False


def test_evaluate_missing_field_fails():
    assert evaluate_filter({"dde_net": None}, "dde_net", ">", 0) is False
    assert evaluate_filter({}, "dde_net", ">", 0) is False


# ── FieldFilter 校验 ───────────────────────────────────────


def test_field_filter_validation():
    with pytest.raises(ValueError):
        FieldFilter.from_dict({"field": "", "op": ">", "value": 1})
    with pytest.raises(ValueError):
        FieldFilter.from_dict({"field": "dde_net", "op": "??", "value": 1})
    with pytest.raises(ValueError):
        FieldFilter.from_dict({"field": "dde_net", "op": ">", "value": "abc"})
    f = FieldFilter.from_dict({"field": "board", "op": "==", "value": "主板"})
    assert f.value == "主板"
    f2 = FieldFilter.from_dict({"field": "dde_net", "op": ">", "value": "0.5"})
    assert f2.value == 0.5


# ── OR 买入逻辑（信号库 / 问财字段 任一满足即买） ──────────


class FakeMarket:
    def __init__(self, rows):
        self.rows = rows
        self.limit_up = {}

    def day_rows(self, date, signal_ids=None):
        return self.rows.get(date, {})

    def symbol_limit_up(self, symbol):
        return None

    def buyable_at_open(self, symbol, row):
        return bool(row and row.open and row.open > 0 and row.volume and row.volume > 0)

    def sellable_at_close(self, row):
        return bool(row and row.close and row.close > 0)


def make_acct():
    from app.paper.models import Account
    return Account.create("acc1", "测试", 100000)


def make_strategy(filters=None, signals=None):
    from app.paper.models import PaperStrategy
    s = PaperStrategy.create("strat1", "策略", "acc1", "query")
    s.buy_rule.iwencai_filters = [FieldFilter.from_dict(x) for x in (filters or [])]
    s.buy_rule.signal_ids = list(signals or [])
    return s


def row(symbol, csg=None):
    return DayRow(symbol=symbol, open=10, close=10, volume=100000, csg=csg or {})


def test_buy_no_conditions_buys_all():
    mk = FakeMarket({"2026-01-02": {"000001": row("000001"), "000002": row("000002")}})
    acct = make_acct()
    s = make_strategy()
    _, snap = process_day(mk, acct, s, "2026-01-02", ["000001", "000002"])
    assert len(acct.pending) == 2


def test_buy_field_condition_filters_candidates():
    mk = FakeMarket({"2026-01-02": {"000001": row("000001"), "000002": row("000002")}})
    acct = make_acct()
    s = make_strategy(filters=[{"field": "dde_net", "op": ">", "value": 3}])
    iwencai_rows = {
        "000001": {"dde_net": 9.7, "board": "主板"},
        "000002": {"dde_net": 0.5, "board": "主板"},
    }
    _, snap = process_day(mk, acct, s, "2026-01-02", ["000001", "000002"], iwencai_rows)
    assert [p.symbol for p in acct.pending] == ["000001"]


def test_buy_signal_and_field_or_any_source_passes():
    mk = FakeMarket({"2026-01-02": {
        "000001": row("000001"),
        "000002": row("000002", csg={"csg_go": True}),
        "000003": row("000003", csg={"csg_go": False}),
    }})
    acct = make_acct()
    s = make_strategy(filters=[{"field": "dde_net", "op": ">", "value": 3}], signals=["go"])
    iwencai_rows = {
        "000001": {"dde_net": 9.7},   # 字段满足（信号不满足）
        "000002": {"dde_net": 1.0},   # 信号满足（字段不满足）
        "000003": {"dde_net": 1.0},   # 都不满足
    }
    _, snap = process_day(mk, acct, s, "2026-01-02", ["000001", "000002", "000003"], iwencai_rows)
    assert sorted(p.symbol for p in acct.pending) == ["000001", "000002"]


# ── 动态问财字段目录（测试/快照回退） ─────────────────────


def test_available_fields_derived_from_records():
    from app.paper.fields import available_fields
    flds = available_fields([sample_raw(),
                             {**sample_raw(), "股票代码": "000001.SZ",
                              "股票简称": "平安银行", "dde大单净量[20260904]": -1.2,
                              "上市板块": "主板", "涨跌幅[20260824-20260904]": 5.0}])
    keys = {f["key"] for f in flds}
    assert {"dde_net", "auction_pct", "vol_ratio", "board", "name"} <= keys
    assert any(k.startswith("return_") for k in keys)
    by = {f["key"]: f for f in flds}
    assert by["dde_net"]["type"] == "number"
    assert by["board"]["type"] == "string"


def test_fields_from_normalized_uses_raw_label():
    from app.paper.fields import fields_from_normalized
    fm = {"000560": {"dde_net": 9.7}, "000001": {"dde_net": -1.2}}
    raw = {"000560": {"dde大单净量[20260904]": 9.7, "trade": 1}}
    flds = fields_from_normalized(fm, raw)
    dde = next(f for f in flds if f["key"] == "dde_net")
    # 已知 key 用静态目录中文 label；未知 key 回落原始列名
    assert dde["label"] == "DDE大单净量"
    assert dde["type"] == "number"


def test_signal_options_merges_snapshot_fields(tmp_path):
    from types import SimpleNamespace

    from app.paper import context as pc
    from app.paper.api import signal_options
    from app.paper.models import Account, PaperStrategy

    store = PaperStore(tmp_path / "data")
    store.save_accounts({"acc1": Account.create("acc1", "测试", 100000)})
    store.save_strategies({"strat1": PaperStrategy.create("strat1", "策略", "acc1", "query")})
    store.save_iwencai_snapshot("2026-09-04", "strat1",
                                {"symbols": ["000560"], "count": 1,
                                 "rows": [sample_raw()], "fields": {"000560": {
                                     "dde_net": 9.7, "return_10d": 20.0, "custom_flow": 1.0}}})
    pc.set_instances(store, None, None)
    out = signal_options(SimpleNamespace(), strategy_id="strat1")
    keys = {f["key"] for f in out["fields"]}
    assert "dde_net" in keys and "custom_flow" in keys   # 动态字段已并入
    out2 = signal_options(SimpleNamespace())
    assert "custom_flow" not in {f["key"] for f in out2["fields"]}


# ── 账户详情（持仓/流水/余额曲线数据源） ──────────────────


def test_account_detail_resolves_bound_strategy(tmp_path):
    from types import SimpleNamespace

    from app.paper import context as pc
    from app.paper.api import account_detail
    from app.paper.models import (Account, DaySnapshot, PaperStrategy, Position,
                                  TradeRecord)

    store = PaperStore(tmp_path / "data")
    acct = Account.create("acc1", "测试", 100000)
    pos = Position("000560", 1000, 3.0, "2026-09-04", 1)
    acct.positions = {"000560": pos}
    store.save_accounts({"acc1": acct})
    store.save_strategies({"strat1": PaperStrategy.create("strat1", "策略", "acc1", "query")})
    trade = TradeRecord("t1", "acc1", "strat1", "2026-09-04", "000560", "buy",
                        1000, 3.0, 3000.0, "entry_fill")
    store.append_trades("strat1", [trade])
    store.save_day(DaySnapshot("acc1", "strat1", "2026-09-04", 97000.0, {"000560": pos},
                               3000.0, 100000.0, 1.0, [trade]))
    pc.set_instances(store, None, None)
    detail = account_detail("acc1", SimpleNamespace())
    assert detail["strategy_id"] == "strat1"
    assert detail["positions"] == [{"symbol": "000560", "qty": 1000, "avg_cost": 3.0}]
    assert detail["days"][0]["total_value"] == 100000.0
    assert detail["trades"][0]["amount"] == 3000.0


def test_account_detail_no_bound_is_empty(tmp_path):
    from types import SimpleNamespace

    from app.paper import context as pc
    from app.paper.api import account_detail
    from app.paper.models import Account

    store = PaperStore(tmp_path / "data")
    store.save_accounts({"acc2": Account.create("acc2", "无绑定", 100000)})
    pc.set_instances(store, None, None)
    detail = account_detail("acc2", SimpleNamespace())
    assert detail["strategy_id"] is None
    assert detail["days"] == [] and detail["trades"] == []


# ── 级联删除落盘数据 ──────────────────────────────────────


# ── 账户/策略 读写往返（回归：load 时按对象 id 而非 dict 属性） ──


def test_accounts_and_strategies_roundtrip(tmp_path):
    from app.paper.models import Account, PaperStrategy
    store = PaperStore(tmp_path / "data")
    acct = Account.create("acc1", "测试", 100000)
    strat = PaperStrategy.create("strat1", "策略", "acc1", "query")
    store.save_accounts({acct.id: acct})
    store.save_strategies({strat.id: strat})

    loaded_accs = store.load_accounts()
    assert list(loaded_accs) == ["acc1"]
    assert loaded_accs["acc1"].name == "测试"

    loaded_strats = store.load_strategies()
    assert list(loaded_strats) == ["strat1"]
    assert loaded_strats["strat1"].account_id == "acc1"
    assert loaded_strats["strat1"].buy_rule.to_dict()["iwencai_filters"] == []


def test_delete_strategy_data_cascades(tmp_path):
    store = PaperStore(tmp_path / "data")
    store.save_iwencai_snapshot("2026-01-02", "strat1", {"symbols": ["000001"]})
    store.save_iwencai_snapshot("2026-01-03", "strat1", {"symbols": ["000002"]})
    store.save_iwencai_snapshot("2026-01-04", "strat2", {"symbols": ["000003"]})
    from app.paper.models import DaySnapshot, Position
    snap = DaySnapshot("acc1", "strat1", "2026-01-02", 10000.0,
                       {"000001": Position("000001", 100, 10, "2026-01-02")}, 1000.0, 11000.0, 1.1)
    store.save_day(snap)
    store.append_trades("strat1", [])

    assert (store.snapshot_dir("2026-01-02") / "strat1.json").exists()
    store.delete_strategy_data("strat1")

    assert not (store.snapshot_dir("2026-01-02") / "strat1.json").exists()
    assert not (store.snapshot_dir("2026-01-03") / "strat1.json").exists()
    assert (store.snapshot_dir("2026-01-04") / "strat2.json").exists(), "他策略数据不受影响"
    assert not store.list_iwencai_dates("strat1")
    assert not store.list_day_dates("strat1")