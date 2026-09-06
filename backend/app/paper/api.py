"""问财实盘模拟 API 路由：账户 / 策略 CRUD + 手动触发 + 查询。

挂载到 /api/paper/*，通过 app.state.paper_store / paper_market / paper_scheduler
访问共享实例（由 backend/app/custom/paper.py 在启动时注入）。
"""
from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import context, service
from .fields import (IWENCAI_FIELD_CATALOG, available_fields,
                     fields_from_normalized, strip_col_date)
from .iwencai_service import run_query
from .models import (Account, BuyRule, LOT, PaperStrategy, Position,
                     SellRule, TradeRecord, make_id, validate_id)

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _store(request: Request):
    return context.get_store()


def _market(request: Request):
    return context.get_market()


def _scheduler(request: Request):
    return context.get_scheduler()


def _reload_scheduler(request: Request) -> None:
    sch = _scheduler(request)
    if sch is not None:
        sch.reload()


def _account_bound_to_other(strategies: dict, account_id: str, except_id: str) -> bool:
    """账户是否已被除 except_id 之外的策略绑定（一对一）。"""
    for sid, s in strategies.items():
        if sid != except_id and s.account_id == account_id:
            return True
    return False


def _json_safe(v):
    """把问财原始记录里的 NaN/Infinity（JSON 不支持）洗成 None，其余原样返回。"""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


# ── 请求模型 ────────────────────────────────────────────


class AccountModel(BaseModel):
    id: str
    name: str
    initial_cash: float = 100000.0


class AccountUpdateModel(BaseModel):
    name: str
    initial_cash: float


class ManualTradeModel(BaseModel):
    symbol: str
    side: str          # buy | sell
    qty: int
    price: float
    date: str = ""


class StrategyModel(BaseModel):
    id: str
    name: str
    account_id: str
    iwencai_query: str
    api_key: str = ""
    enabled: bool = True
    buy_rule: dict = {}
    sell_rule: dict = {}
    fetch_time: str = "09:25"
    simulate_time: str = "15:30"


# ── 账户 ────────────────────────────────────────────────


@router.get("/accounts")
def list_accounts(request: Request):
    accounts = _store(request).load_accounts()
    return {"accounts": [a.to_dict() for a in accounts.values()]}


@router.post("/accounts")
def create_account(req: AccountModel, request: Request):
    store = _store(request)
    validate_id(req.id, "账户id")
    if req.initial_cash <= 0:
        raise HTTPException(status_code=400, detail="initial_cash 必须大于 0")
    accounts = store.load_accounts()
    if req.id in accounts:
        raise HTTPException(status_code=400, detail="账户 id 已存在")
    accounts[req.id] = Account.create(req.id, req.name, req.initial_cash)
    store.save_accounts(accounts)
    return {"ok": True, "account": accounts[req.id].to_dict()}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str, request: Request):
    store = _store(request)
    accounts = store.load_accounts()
    if account_id not in accounts:
        raise HTTPException(status_code=404, detail="账户不存在")
    # 校验：仍有策略绑定时禁止删除
    for s in store.load_strategies().values():
        if s.account_id == account_id:
            raise HTTPException(status_code=400,
                                detail=f"账户仍被策略「{s.name}」绑定，请先删除/改绑策略")
    del accounts[account_id]
    store.save_accounts(accounts)
    return {"ok": True}


@router.put("/accounts/{account_id}")
def update_account(account_id: str, req: AccountUpdateModel, request: Request):
    store = _store(request)
    accounts = store.load_accounts()
    if account_id not in accounts:
        raise HTTPException(status_code=404, detail="账户不存在")
    if req.initial_cash <= 0:
        raise HTTPException(status_code=400, detail="initial_cash 必须大于 0")
    acc = accounts[account_id]
    acc.name = req.name.strip()
    acc.initial_cash = req.initial_cash
    store.save_accounts(accounts)
    return {"ok": True, "account": acc.to_dict()}


@router.post("/accounts/{account_id}/trade")
def manual_trade(account_id: str, req: ManualTradeModel, request: Request):
    """手动新增/平仓一笔交易：直接改账户现金与持仓，并追加一条"manual"成交流水。"""
    store = _store(request)
    accounts = store.load_accounts()
    if account_id not in accounts:
        raise HTTPException(status_code=404, detail="账户不存在")
    acc = accounts[account_id]
    symbol = req.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    if req.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side 只能是 buy/sell")
    if req.qty <= 0 or req.qty % LOT != 0:
        raise HTTPException(status_code=400, detail="数量必须为 100 股整手且 > 0")
    if req.price <= 0:
        raise HTTPException(status_code=400, detail="价格必须大于 0")
    date = req.date.strip() or _date.today().isoformat()

    if req.side == "buy":
        cost = req.qty * req.price
        if acc.cash + 1e-9 < cost:
            raise HTTPException(status_code=400,
                                detail=f"可用资金不足（需 {cost:.2f}，当前 {acc.cash:.2f}）")
        acc.cash -= cost
        if symbol in acc.positions:
            pos = acc.positions[symbol]
            tq = pos.qty + req.qty
            pos.avg_cost = (pos.avg_cost * pos.qty + cost) / tq
            pos.qty = tq
        else:
            acc.positions[symbol] = Position(symbol, req.qty, req.price, date)
    else:
        pos = acc.positions.get(symbol)
        if pos is None or pos.qty < req.qty:
            have = pos.qty if pos else 0
            raise HTTPException(status_code=400,
                                detail=f"持仓不足（当前 {have} 股，需 {req.qty} 股）")
        acc.cash += req.qty * req.price
        pos.qty -= req.qty
        if pos.qty == 0:
            del acc.positions[symbol]
    store.save_accounts(accounts)

    # 追加一条人工成交到绑定策略的流水（保证列表可见、级联删除一致）
    bound = next((s for s in store.load_strategies().values()
                  if s.account_id == account_id), None)
    if bound is not None:
        tr = TradeRecord(make_id("t"), account_id, bound.id, date, symbol,
                         req.side, req.qty, req.price,
                         round(req.qty * req.price, 2), "manual")
        store.append_trades(bound.id, [tr])
    return {"ok": True, "account": acc.to_dict()}


# ── 策略 ────────────────────────────────────────────────


@router.get("/strategies")
def list_strategies(request: Request):
    store = _store(request)
    strategies = store.load_strategies()
    accounts = store.load_accounts()
    out = []
    for s in strategies.values():
        d = s.to_dict()
        d["account_name"] = accounts.get(s.account_id).name if s.account_id in accounts else ""
        out.append(d)
    return {"strategies": out}


@router.get("/strategy/options")
def signal_options(request: Request, strategy_id: str | None = None):
    """返回可选的自定义信号与问财字段，供买卖规则下拉。

    传入 strategy_id 时，若该策略已有问财快照，则把快照中实际返回的动态字段并入
    `fields`（动态优先、去重，供“快照回退”），否则仅返回内置静态目录。
    """
    from app.strategy import custom_signals
    store = _store(request)
    data_dir = store.root.parent  # data/paper -> data
    sigs = custom_signals.load_all(data_dir)
    fields: list[dict] = ([{"key": k, "label": label, "type": typ}
                           for k, label, typ in IWENCAI_FIELD_CATALOG])
    if strategy_id:
        strategies = store.load_strategies()
        if strategy_id in strategies:
            dates = store.list_iwencai_dates(strategy_id)
            if dates:
                payload = store.load_iwencai_snapshot(dates[-1], strategy_id)
                if payload and payload.get("fields"):
                    dynamic = fields_from_normalized(payload["fields"])
                    merged: list[dict] = []
                    seen: set[str] = set()
                    for d in dynamic + fields:
                        if d["key"] not in seen:
                            seen.add(d["key"])
                            merged.append(d)
                    fields = merged
    return {"signals": [
        {"id": s.get("id"), "name": s.get("name"), "kind": s.get("kind"),
         "column": custom_signals.column_name(s.get("id", ""))}
        for s in sigs
    ], "fields": fields}


class PreviewModel(BaseModel):
    query: str
    api_key: str = ""


@router.post("/strategy/preview")
def preview_fields(req: PreviewModel, request: Request):
    """测试一次问财选股，返回实际返回的列（归一化字段目录），供「不写死」字段下拉。"""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="问财问句不能为空")
    try:
        df, symbols = run_query(req.query.strip(), req.api_key.strip())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"问财测试失败: {e}")
    records = df.to_dict(orient="records") if df is not None and not df.empty else []
    return {"count": len(symbols), "symbols": symbols,
            "fields": available_fields(records)}


@router.post("/strategies")
def create_strategy(req: StrategyModel, request: Request):
    store = _store(request)
    validate_id(req.id, "策略id")
    if not req.iwencai_query.strip():
        raise HTTPException(status_code=400, detail="iwencai_query 不能为空")
    accounts = store.load_accounts()
    if req.account_id not in accounts:
        raise HTTPException(status_code=400, detail="绑定的账户不存在")
    strategies = store.load_strategies()
    if req.id in strategies:
        raise HTTPException(status_code=400, detail="策略 id 已存在")
    if _account_bound_to_other(strategies, req.account_id, req.id):
        raise HTTPException(status_code=400, detail="该账户已被其它策略卡片绑定（一对一）")
    try:
        buy = BuyRule.from_dict(req.buy_rule)
        sell = SellRule.from_dict(req.sell_rule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    strategy = PaperStrategy.create(req.id, req.name, req.account_id,
                                    req.iwencai_query.strip(), req.api_key.strip())
    strategy.enabled = req.enabled
    strategy.buy_rule = buy
    strategy.sell_rule = sell
    strategy.fetch_time = req.fetch_time
    strategy.simulate_time = req.simulate_time
    strategies[req.id] = strategy
    store.save_strategies(strategies)
    _reload_scheduler(request)
    return {"ok": True, "strategy": strategy.to_dict()}


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: str, req: StrategyModel, request: Request):
    store = _store(request)
    strategies = store.load_strategies()
    if strategy_id not in strategies:
        raise HTTPException(status_code=404, detail="策略不存在")
    accounts = store.load_accounts()
    if req.account_id not in accounts:
        raise HTTPException(status_code=400, detail="绑定的账户不存在")
    if _account_bound_to_other(strategies, req.account_id, strategy_id):
        raise HTTPException(status_code=400, detail="该账户已被其它策略卡片绑定（一对一）")
    try:
        buy = BuyRule.from_dict(req.buy_rule)
        sell = SellRule.from_dict(req.sell_rule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    s = strategies[strategy_id]
    s.name = req.name
    s.account_id = req.account_id
    s.iwencai_query = req.iwencai_query.strip()
    s.api_key = req.api_key.strip()
    s.enabled = req.enabled
    s.buy_rule = buy
    s.sell_rule = sell
    s.fetch_time = req.fetch_time
    s.simulate_time = req.simulate_time
    store.save_strategies(strategies)
    _reload_scheduler(request)
    return {"ok": True, "strategy": s.to_dict()}


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: str, request: Request):
    store = _store(request)
    strategies = store.load_strategies()
    if strategy_id not in strategies:
        raise HTTPException(status_code=404, detail="策略不存在")
    del strategies[strategy_id]
    store.save_strategies(strategies)
    store.delete_strategy_data(strategy_id)  # 级联清理该卡片的问财快照/日快照/成交
    _reload_scheduler(request)
    return {"ok": True}


# ── 手动触发 ────────────────────────────────────────────


@router.post("/strategies/{strategy_id}/fetch")
def fetch_now(strategy_id: str, request: Request):
    store = _store(request)
    strategy = store.load_strategies().get(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    try:
        result = service.run_fetch(store, strategy, when="manual")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"问财拉取失败: {e}")
    return {"ok": True, **result}


@router.post("/strategies/{strategy_id}/simulate")
def simulate_now(strategy_id: str, request: Request, date: str | None = None,
                 fallback: str = ""):
    store = _store(request)
    strategy = store.load_strategies().get(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    date_str = date or _date.today().isoformat()
    fallback_candidates = [s.strip() for s in fallback.split(",") if s.strip()] if fallback else None
    try:
        result = service.run_simulate(store, _market(request), strategy,
                                      date_str, fallback_candidates)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


# ── 查询 ────────────────────────────────────────────────


@router.get("/strategies/{strategy_id}/iwencai")
def iwencai_snapshot(strategy_id: str, request: Request, date: str | None = None,
                     list: bool = False):
    store = _store(request)
    if list or date is None:
        return {"dates": store.list_iwencai_dates(strategy_id)}
    payload = store.load_iwencai_snapshot(date, strategy_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="该日期无问财快照")
    return payload


@router.get("/strategies/{strategy_id}/days")
def day_list(strategy_id: str, request: Request):
    store = _store(request)
    dates = store.list_day_dates(strategy_id)
    out = []
    for d in dates:
        snap = store.load_day(d, strategy_id)
        if snap:
            out.append({"date": d, "total_value": snap.get("total_value"),
                        "cash": snap.get("cash"), "nav": snap.get("nav"),
                        "trades": len(snap.get("trades", []))})
    return {"days": out}


@router.get("/strategies/{strategy_id}/trades")
def trades(strategy_id: str, request: Request):
    return {"trades": _store(request).load_trades(strategy_id)}


@router.get("/strategies/{strategy_id}/snapshot")
def snapshot_sheet(strategy_id: str, request: Request, date: str | None = None):
    """某个策略某一天（默认最近一次）问财选股快照的完整明细（按接口返回的原始列）。"""
    store = _store(request)
    if strategy_id not in store.load_strategies():
        raise HTTPException(status_code=404, detail="策略不存在")
    dates = store.list_iwencai_dates(strategy_id)
    if not dates:
        return {"date": None, "rows": [], "columns": [], "count": 0}
    d = date or dates[-1]
    snap = store.load_iwencai_snapshot(d, strategy_id) or {}
    rows = snap.get("rows") or []
    # 列名去掉日期后缀（如 [20260904]、[20260824-20260904]），行 key 同步重映射
    columns: list[str] = []
    cleaned_rows: list[dict] = []
    for r in rows:
        out: dict = {}
        for k, v in r.items():
            key = str(k)
            kk = strip_col_date(key)
            # 清洗后与其他列撞名时，保留带日期的原始列名（避免丢数据）
            kk = kk if kk not in out else key
            if kk not in columns:
                columns.append(kk)
            out[kk] = v
        cleaned_rows.append(out)
    return {"date": d, "rows": [_json_safe(r) for r in cleaned_rows],
            "columns": columns, "count": snap.get("count", len(rows))}


@router.get("/strategies/{strategy_id}/latest")
def latest_snapshot(strategy_id: str, request: Request):
    """最新一次问财选股快照（供策略卡片内联展示当日选股名单）。"""
    store = _store(request)
    if strategy_id not in store.load_strategies():
        raise HTTPException(status_code=404, detail="策略不存在")
    dates = store.list_iwencai_dates(strategy_id)
    if not dates:
        return {"date": None, "symbols": [], "count": 0}
    snap = store.load_iwencai_snapshot(dates[-1], strategy_id) or {}
    fields = snap.get("fields") or {}
    symbols = snap.get("symbols") or []
    lst = [{"symbol": s, "name": (fields.get(s) or {}).get("name", "")} for s in symbols]
    return {"date": dates[-1], "symbols": lst, "count": len(lst)}


@router.get("/account/{account_id}/detail")
def account_detail(account_id: str, request: Request):
    """账户详情：持仓股 / 交易流水 / 余额曲线（复用一对一绑定策略的快照与成交）。"""
    store = _store(request)
    accounts = store.load_accounts()
    if account_id not in accounts:
        raise HTTPException(status_code=404, detail="账户不存在")
    account = accounts[account_id]
    strategies = store.load_strategies()
    bound = next((s for s in strategies.values() if s.account_id == account_id), None)

    days: list[dict] = []
    trades: list[dict] = []
    if bound is not None:
        for d in store.list_day_dates(bound.id):
            snap = store.load_day(d, bound.id) or {}
            days.append({
                "date": d,
                "cash": snap.get("cash"),
                "market_value": snap.get("market_value"),
                "total_value": snap.get("total_value"),
                "nav": snap.get("nav"),
            })
        for t in store.load_trades(bound.id):
            trades.append({
                "date": t.get("date"),
                "symbol": t.get("symbol"),
                "side": t.get("side"),
                "qty": t.get("qty"),
                "price": t.get("price"),
                "reason": t.get("reason"),
                "amount": round((t.get("qty") or 0) * (t.get("price") or 0), 2),
            })

    positions = [{"symbol": p.symbol, "qty": p.qty, "avg_cost": p.avg_cost}
                 for p in account.positions.values()]
    return {
        "account": account.to_dict(),
        "strategy_id": bound.id if bound else None,
        "strategy_name": bound.name if bound else None,
        "days": days, "trades": trades, "positions": positions,
    }