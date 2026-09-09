"""问财实盘模拟数据结构。

所有金额单位：人民币元；价格使用项目 enriched 的前复权价（open/close）。
一手 = 100 股，成交只按整手。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
LOT = 100  # A股一手 = 100 股


def make_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time()*1000)}_{int(time.perf_counter_ns()%1000)}"


def validate_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"{label} 非法（仅小写字母数字下划线，1-40字符）")
    return value


# ── 买卖规则 ─────────────────────────────────────────────
# 信号 ids 均指现有自定义信号列，去掉 csg_ 前缀，用 id 填。求值时映射为 csg_<id>。

_NUMERIC_OPS = {">", ">=", "<", "<="}
_GENERAL_OPS = {"==", "!="}          # 数值/字符串通用（能转数值则数值比较）
_STRING_OPS = {"contains", "not_contains"}
_ALL_OPS = _NUMERIC_OPS | _GENERAL_OPS | _STRING_OPS


@dataclass
class FieldFilter:
    """对问财返回归一化字段的单条件。field 见 app.paper.fields.IWENCAI_FIELD_CATALOG。"""

    field: str
    op: str            # > >= < <= == != contains not_contains
    value: str | float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldFilter:
        field = str(d.get("field", "")).strip()
        if not field:
            raise ValueError("条件字段不能为空")
        op = str(d.get("op", "")).strip()
        if op not in _ALL_OPS:
            raise ValueError(f"非法比较操作符: {op}")
        raw = d.get("value")
        if op in _NUMERIC_OPS:
            try:
                value: str | float = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"字段 {field} 的 {op} 需要数值")
        elif op in _GENERAL_OPS:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = str(raw)
        else:
            value = str(raw)
        return cls(field=field, op=op, value=value)

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value}


@dataclass
class BuyRule:
    """买入规则。

    signal_ids: 信号库入场信号（csg_*），须全部成立才满足信号来源。
    iwencai_filters: 问财返回字段的数值/文本条件，须全部满足才满足字段来源。
        加入条件：信号库与字段条件"任一来源满足即可(OR)"；两者皆为空则买全部候选。
    sort_field: 候选排序依据的问财归一化字段（如 dde_net）；空则不排序（按问财返回顺序）。
    sort_order: 排序方向，desc 降序 / asc 升序。
    top_n: 排序后只买前 N 只（0=不限制）。
    max_position_pct: 单股预算占现金的比例上限。
    max_total_pct: 总仓位上限（持仓+待买入占资产净值比例，0=不限制）。
    max_symbols: 单日最大买入数量（0=不限制）。
    buy_time: 成交时点。next_open=次日开盘价（默认，避免未来函数）；same_open=当日开盘价。
    """

    signal_ids: list[str] = field(default_factory=list)
    iwencai_filters: list[FieldFilter] = field(default_factory=list)
    sort_field: str = ""
    sort_order: str = "desc"
    top_n: int = 0
    max_position_pct: float = 0.2
    max_total_pct: float = 0.0
    max_symbols: int = 0
    buy_time: str = "next_open"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> BuyRule:
        d = d or {}
        max_pct = float(d.get("max_position_pct", 0.2))
        if not (0 < max_pct <= 1):
            raise ValueError("max_position_pct 必须在 (0,1] 区间")
        max_tot = float(d.get("max_total_pct", 0.0))
        if not (0 <= max_tot <= 1):
            raise ValueError("max_total_pct 必须在 [0,1] 区间")
        max_sym = int(d.get("max_symbols", 0))
        if max_sym < 0:
            raise ValueError("max_symbols 不能为负")
        top = int(d.get("top_n", 0))
        if top < 0:
            raise ValueError("top_n 不能为负")
        sigs = d.get("signal_ids", [])
        if not isinstance(sigs, list) or not all(isinstance(s, str) for s in sigs):
            raise ValueError("signal_ids 必须是字符串列表")
        filters = d.get("iwencai_filters", [])
        if not isinstance(filters, list):
            raise ValueError("iwencai_filters 必须是列表")
        parsed = [FieldFilter.from_dict(f) for f in filters if isinstance(f, dict)]
        order = str(d.get("sort_order", "desc")).lower()
        if order not in ("desc", "asc"):
            raise ValueError("sort_order 只能是 desc 或 asc")
        return cls(signal_ids=list(sigs), iwencai_filters=parsed,
                   sort_field=str(d.get("sort_field", "")),
                   sort_order=order, top_n=top,
                   max_position_pct=max_pct, max_total_pct=max_tot,
                   max_symbols=max_sym, buy_time=str(d.get("buy_time", "next_open")))

    def to_dict(self) -> dict[str, Any]:
        return {"signal_ids": self.signal_ids,
                "iwencai_filters": [f.to_dict() for f in self.iwencai_filters],
                "sort_field": self.sort_field, "sort_order": self.sort_order,
                "top_n": self.top_n,
                "max_position_pct": self.max_position_pct,
                "max_total_pct": self.max_total_pct,
                "max_symbols": self.max_symbols,
                "buy_time": self.buy_time}


@dataclass
class SellRule:
    """卖出规则（任一命中即卖出，一次判定一次卖出）。

    exit_signal_ids: 持仓日任一成立即离场（OR）。
    stop_loss_pct / take_profit_pct: 相对持仓成本价的止损/止盈（如 -0.08 = 亏8%止损）。
        触发判定按当日盘中触及（low/high 碰到线价），成交也按线价（见 trading._sell_positions）。
    max_hold_days: 最长持有交易日数，超期卖出。None=不限制。1 = 买入次日卖出。
    sell_time: 信号/持股天数退出的成交时点。close=结算日收盘价（默认）；
        open=结算日开盘价（持股天数=1 时即"次日开盘卖出"）。止损/止盈始终按线价成交。
    """

    exit_signal_ids: list[str] = field(default_factory=list)
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_hold_days: int | None = None
    sell_time: str = "close"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> SellRule:
        d = d or {}
        sigs = d.get("exit_signal_ids", [])
        if not isinstance(sigs, list) or not all(isinstance(s, str) for s in sigs):
            raise ValueError("exit_signal_ids 必须是字符串列表")

        def _ratio(key: str) -> float | None:
            v = d.get(key)
            if v is None or v == "":
                return None
            v = float(v)
            if not (-1 < v < 100):
                raise ValueError(f"{key} 必须在 (-1,100) 区间（小数制）")
            return v

        hold = d.get("max_hold_days")
        hold = int(hold) if hold not in (None, "") else None
        if hold is not None and hold <= 0:
            raise ValueError("max_hold_days 必须为正整数")
        stime = str(d.get("sell_time", "close")).lower()
        if stime not in ("close", "open"):
            raise ValueError("sell_time 只能是 close 或 open")
        return cls(exit_signal_ids=list(sigs),
                   stop_loss_pct=_ratio("stop_loss_pct"),
                   take_profit_pct=_ratio("take_profit_pct"),
                   max_hold_days=hold,
                   sell_time=stime)

    def to_dict(self) -> dict[str, Any]:
        return {"exit_signal_ids": self.exit_signal_ids,
                "stop_loss_pct": self.stop_loss_pct,
                "take_profit_pct": self.take_profit_pct,
                "max_hold_days": self.max_hold_days,
                "sell_time": self.sell_time}


# ── 账户 ────────────────────────────────────────────────


@dataclass
class Position:
    symbol: str
    qty: int                    # 股数
    avg_cost: float             # 每股平均成本（买入价）
    entry_date: str             # 首次买入日期 YYYY-MM-DD
    hold_days: int = 0          # 已持有交易日数（每日结算+1）

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "qty": self.qty,
                "avg_cost": round(self.avg_cost, 4), "entry_date": self.entry_date,
                "hold_days": self.hold_days}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        return cls(symbol=str(d["symbol"]), qty=int(d["qty"]),
                   avg_cost=float(d["avg_cost"]), entry_date=str(d["entry_date"]),
                   hold_days=int(d.get("hold_days", 0)))


@dataclass
class PendingOrder:
    symbol: str
    qty: int                # 目标股数
    decision_date: str      # 选股/入场决策日期（在该日次日开盘成交）

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "qty": self.qty,
                "decision_date": self.decision_date}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingOrder:
        return cls(symbol=str(d["symbol"]), qty=int(d["qty"]),
                   decision_date=str(d["decision_date"]))


@dataclass
class Account:
    id: str
    name: str
    initial_cash: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)   # symbol -> Position
    pending: list[PendingOrder] = field(default_factory=list)
    enabled: bool = True
    last_record_date: str | None = None

    @classmethod
    def create(cls, account_id: str, name: str, initial_cash: float) -> Account:
        return cls(id=account_id, name=name, initial_cash=initial_cash, cash=initial_cash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name,
            "initial_cash": round(self.initial_cash, 2), "cash": round(self.cash, 2),
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "pending": [o.to_dict() for o in self.pending],
            "enabled": self.enabled, "last_record_date": self.last_record_date,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Account:
        return cls(
            id=str(d["id"]), name=str(d["name"]),
            initial_cash=float(d.get("initial_cash", d.get("cash", 0))),
            cash=float(d["cash"]),
            positions={s: Position.from_dict(p) for s, p in (d.get("positions") or {}).items()},
            pending=[PendingOrder.from_dict(o) for o in (d.get("pending") or [])],
            enabled=bool(d.get("enabled", True)),
            last_record_date=d.get("last_record_date"),
        )


# ── 策略 ────────────────────────────────────────────────


@dataclass
class PaperStrategy:
    id: str
    name: str
    account_id: str
    iwencai_query: str
    api_key: str = ""
    enabled: bool = True
    buy_rule: BuyRule = field(default_factory=BuyRule)
    sell_rule: SellRule = field(default_factory=SellRule)
    # 调度：交易日期可选（工作日自动跳过周末；节假日由数据缺失自然跳过）
    fetch_time: str = "09:25"
    simulate_time: str = "15:30"

    @classmethod
    def create(cls, strategy_id: str, name: str, account_id: str,
               iwencai_query: str, api_key: str = "") -> PaperStrategy:
        return cls(id=validate_id(strategy_id, "策略id"),
                   name=name, account_id=account_id,
                   iwencai_query=iwencai_query, api_key=api_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "account_id": self.account_id,
            "iwencai_query": self.iwencai_query, "api_key": self.api_key,
            "enabled": self.enabled, "buy_rule": self.buy_rule.to_dict(),
            "sell_rule": self.sell_rule.to_dict(),
            "fetch_time": self.fetch_time, "simulate_time": self.simulate_time,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PaperStrategy:
        return cls(
            id=validate_id(str(d["id"]), "策略id"),
            name=str(d["name"]),
            account_id=str(d["account_id"]),
            iwencai_query=str(d.get("iwencai_query", "")),
            api_key=str(d.get("api_key", "")),
            enabled=bool(d.get("enabled", True)),
            buy_rule=BuyRule.from_dict(d.get("buy_rule")),
            sell_rule=SellRule.from_dict(d.get("sell_rule")),
            fetch_time=str(d.get("fetch_time", "09:25")),
            simulate_time=str(d.get("simulate_time", "15:30")),
        )


# ── 成交与快照 ──────────────────────────────────────────


@dataclass
class TradeRecord:
    id: str
    account_id: str
    strategy_id: str
    date: str           # 成交日
    symbol: str
    side: str           # buy | sell
    qty: int
    price: float
    amount: float
    reason: str         # entry_fill | exit_signal | stop_loss | take_profit | max_hold

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "account_id": self.account_id,
                "strategy_id": self.strategy_id, "date": self.date,
                "symbol": self.symbol, "side": self.side, "qty": self.qty,
                "price": round(self.price, 4), "amount": round(self.amount, 2),
                "reason": self.reason}


@dataclass
class DaySnapshot:
    account_id: str
    strategy_id: str
    date: str
    cash: float
    positions: dict[str, Position]
    market_value: float
    total_value: float
    nav: float                       # 相对初始资金净值
    trades: list[TradeRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id, "strategy_id": self.strategy_id,
            "date": self.date, "cash": round(self.cash, 2),
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "market_value": round(self.market_value, 2),
            "total_value": round(self.total_value, 2),
            "nav": round(self.nav, 4),
            "trades": [t.to_dict() for t in self.trades],
        }