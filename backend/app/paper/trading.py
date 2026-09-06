"""模拟成交引擎：每日结算一个交易日的账户。

流程（对交易日 D，在盘后运行）：
  1. 成交历史 PB（pending buy）：以 D 的开盘价成交之前生成的待买入单
     （这些单最早在 D 生成，故天然的“次日开盘买入”语义，且跨周末/节假日自动延后
     到有行情的第一天）。停牌/开盘封涨停则顺延。
  2. 卖出：对 D 之前已建仓的持仓，按卖出规则以 D 的收盘价离场（T+1：D 当天
     新买入的持仓不参与当天卖出）。
  3. 生成新的待买入单：以 D 的问财候选 + D 的入场信号为准，目标在 D 之后第一
     个交易日开盘成交。
  4. 结算：按 D 收盘价评估市值 → 写当日快照，持有天数 +1。

纯函数式，入参为 market 数据与策略/账户；不持久化（由调用方保存）。
"""
from __future__ import annotations

import logging

from .fields import evaluate_filters
from .market import DayRow, MarketData
from .models import (
    LOT,
    Account,
    DaySnapshot,
    PaperStrategy,
    PendingOrder,
    Position,
    TradeRecord,
    make_id,
)

logger = logging.getLogger(__name__)


def entry_signal_passes(strategy: PaperStrategy, row: DayRow | None) -> bool:
    """入场信号：策略配置的 signal_ids 需在候选日全部成立；空列表即全选。"""
    ids = strategy.buy_rule.signal_ids
    if not ids:
        return row is not None
    if row is None:
        return False
    return all(row.signal(sid) for sid in ids)


def buy_signal_passes(strategy: PaperStrategy, row: DayRow | None,
                      iwencai_row: dict | None = None) -> bool:
    """买入条件：信号库(csg) 与 问财字段条件"任一来源满足即可(OR)"；两者皆空则买全部。

    - signal_pass: signal_ids 全成立（空列表 → False）
    - field_pass: iwencai_filters 全成立（空列表 → False）
    - 两来源皆空 → True（全部候选可买）
    """
    rule = strategy.buy_rule
    has_signals = bool(rule.signal_ids)
    has_filters = bool(rule.iwencai_filters)
    if not has_signals and not has_filters:
        return row is not None

    signal_pass = bool(rule.signal_ids and row is not None
                       and all(row.signal(sid) for sid in rule.signal_ids))
    field_pass = bool(rule.iwencai_filters
                      and evaluate_filters(iwencai_row or {}, rule.iwencai_filters))
    return signal_pass or field_pass


def decide_exit(strategy: PaperStrategy, pos: Position,
                row: DayRow | None, date: str) -> str | None:
    """按卖出规则判定是否离场，返回原因；不离开场则 None。"""
    rule = strategy.sell_rule
    if row is None:
        return None  # 无当日行情，暂缓卖出（数据缺失则继续持有）
    close = row.close

    if rule.exit_signal_ids and any(row.signal(sid) for sid in rule.exit_signal_ids):
        return "exit_signal"

    if rule.take_profit_pct is not None and close >= pos.avg_cost * (1 + rule.take_profit_pct):
        return "take_profit"

    if rule.stop_loss_pct is not None and close <= pos.avg_cost * (1 + rule.stop_loss_pct):
        return "stop_loss"

    if rule.max_hold_days is not None and pos.hold_days >= rule.max_hold_days:
        return "max_hold"

    _ = date
    return None


def _fill_pending(market: MarketData, account: Account, strategy: PaperStrategy,
                  rows: dict[str, DayRow], date: str) -> list[TradeRecord]:
    """以当日开盘价成交历史待买入单；停牌/封涨停/资金不足则顺延/缩减。"""
    trades: list[TradeRecord] = []
    remaining: list[PendingOrder] = []
    for order in account.pending:
        row = rows.get(order.symbol)
        if not market.buyable_at_open(order.symbol, row):
            remaining.append(order)
            continue
        price = row.open
        affordable = int(account.cash // (price * LOT)) * LOT
        qty = min(order.qty, affordable)
        if qty < LOT or affordable < LOT:
            remaining.append(order)
            continue
        cost = qty * price
        account.cash -= cost
        if order.symbol in account.positions:
            pos = account.positions[order.symbol]
            total_qty = pos.qty + qty
            pos.avg_cost = (pos.avg_cost * pos.qty + cost) / total_qty
            pos.qty = total_qty
        else:
            account.positions[order.symbol] = Position(order.symbol, qty, price, date)
        trades.append(TradeRecord(make_id("t"), account.id, strategy.id, date,
                                  order.symbol, "buy", qty, price, cost, "entry_fill"))
    account.pending = remaining
    return trades


def _sell_positions(market: MarketData, account: Account, strategy: PaperStrategy,
                    rows: dict[str, DayRow], date: str) -> list[TradeRecord]:
    """对 D 之前建仓的持仓按收盘价卖出（T+1）。"""
    trades: list[TradeRecord] = []
    for symbol in list(account.positions.keys()):
        pos = account.positions[symbol]
        if pos.entry_date >= date:
            continue  # 当日新建仓，T+1 不可卖
        row = rows.get(symbol)
        if not market.sellable_at_close(row):
            continue
        reason = decide_exit(strategy, pos, row, date)
        if reason is None:
            continue
        price = row.close
        qty = pos.qty
        proceeds = qty * price
        account.cash += proceeds
        del account.positions[symbol]
        trades.append(TradeRecord(make_id("t"), account.id, strategy.id, date,
                                  symbol, "sell", qty, price, proceeds, reason))
    return trades


def _num(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _buy_today(account: Account, strategy: PaperStrategy, sym: str, qty: int,
               price: float, date: str) -> TradeRecord:
    """当日立即买入并建仓（用于 buy_time=same_open），返回一条买入成交。"""
    cost = qty * price
    account.cash -= cost
    if sym in account.positions:
        pos = account.positions[sym]
        total_qty = pos.qty + qty
        pos.avg_cost = (pos.avg_cost * pos.qty + cost) / total_qty
        pos.qty = total_qty
    else:
        account.positions[sym] = Position(sym, qty, price, date)
    return TradeRecord(make_id("t"), account.id, strategy.id, date,
                       sym, "buy", qty, price, cost, "entry_fill")


def _generate_buys(account: Account, strategy: PaperStrategy, candidates: list[str],
                   rows: dict[str, DayRow], date: str, existing: set[str],
                   iwencai_rows: dict[str, dict] | None = None) -> list[TradeRecord]:
    """按买入规则生成买入并返回当日产生的买入成交。

    排序：按 sort_field 的归一化数值升/降序（缺失值排末尾）；再取 top_n。
    仓位：单股受 max_position_pct 限制；总投入（持仓+待买入）受 max_total_pct 上限。
    成交时点：next_open → 生成待买入单（次日开盘成交）；same_open → 当日立即用开盘价买入。
    """
    rule = strategy.buy_rule
    iwencai_rows = iwencai_rows or {}
    same_open = rule.buy_time == "same_open"

    # 1. 收集满足买入条件的候选
    pool: list[str] = []
    for sym in candidates:
        if sym in existing:
            continue
        row = rows.get(sym)
        if row is None:
            continue
        if not buy_signal_passes(strategy, row, iwencai_rows.get(sym)):
            continue
        pool.append(sym)
    if not pool:
        return []

    # 2. 按字段排序 + TopN
    if rule.sort_field:
        desc = rule.sort_order.lower() != "asc"

        def keyof(sym: str) -> float:
            v = _num((iwencai_rows.get(sym) or {}).get(rule.sort_field))
            return v if v is not None else (float("-inf") if desc else float("inf"))
        pool.sort(key=keyof, reverse=desc)
    if rule.top_n and rule.top_n > 0:
        pool = pool[:rule.top_n]
    picked = pool
    if rule.max_symbols and rule.max_symbols > 0:
        picked = picked[:rule.max_symbols]
    if not picked:
        return []

    # 3. 仓位分配
    def pos_value(sym: str, pos) -> float:
        r = rows.get(sym)
        return (r.close or 0.0) * pos.qty if r and r.close else pos.qty * pos.avg_cost
    total_value = account.cash + sum(pos_value(s, p) for s, p in account.positions.items())
    committed = sum(pos_value(s, p) for s, p in account.positions.items())
    committed += sum(pos_value(o.symbol, o) for o in account.pending)
    # 总仓位预算：现金或 max_total_pct*总资产 扣除已投入后的余额
    total_budget = account.cash if not (rule.max_total_pct and rule.max_total_pct > 0) \
        else max(0.0, total_value * rule.max_total_pct - committed)

    per_budget = min(account.cash / len(picked), account.cash * rule.max_position_pct)
    buys: list[TradeRecord] = []
    for sym in picked:
        if account.cash <= 0 or total_budget <= 0:
            break
        price = (rows[sym].open if same_open else rows[sym].close) or 0.0
        if price <= 0:
            continue
        budget = min(per_budget, account.cash, total_budget)
        qty = int(budget // (price * LOT)) * LOT
        if qty < LOT:
            continue
        if same_open:
            buys.append(_buy_today(account, strategy, sym, qty, price, date))
        else:
            account.pending.append(PendingOrder(sym, qty, date))
        total_budget -= qty * price
    return buys


def process_day(market: MarketData, account: Account, strategy: PaperStrategy,
                date: str, candidates: list[str],
                iwencai_rows: dict[str, dict] | None = None) -> tuple[list[TradeRecord], DaySnapshot]:
    """结算一个交易日，返回 (trades, snapshot)。会就地修改 account。

    iwencai_rows: 当日问财快照的归一化字段（{symbol: {field: value}}），供买入字段条件判定。
    """
    rows = market.day_rows(date)

    trades: list[TradeRecord] = []
    trades += _fill_pending(market, account, strategy, rows, date)     # 1. 次日开盘
    trades += _sell_positions(market, account, strategy, rows, date)   # 2. 当日收盘卖出
    existing = set(account.positions) | {p.symbol for p in account.pending}
    trades += _generate_buys(account, strategy, candidates or [], rows, date,
                             existing, iwencai_rows)                    # 3. 买入（当日开盘 或 生成PB）

    # 4. 结算：市值 + 净值 + 持有天数 +1
    market_value = 0.0
    for sym, pos in list(account.positions.items()):
        row = rows.get(sym)
        price = row.close if (row and row.close and row.close > 0) else pos.avg_cost
        market_value += pos.qty * price
    # 已有持仓待买入单占用：不影响现金（成交时才扣），仅按现金+市值计净值。
    total = account.cash + market_value
    nav = total / account.initial_cash if account.initial_cash else 1.0

    for pos in account.positions.values():
        pos.hold_days += 1

    account.last_record_date = date
    snap = DaySnapshot(account.id, strategy.id, date, account.cash, {
        s: Position.from_dict(p.to_dict()) for s, p in account.positions.items()
    }, market_value, total, nav, trades=list(trades))
    return trades, snap