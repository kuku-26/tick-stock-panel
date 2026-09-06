"""业务编排：拉起问财选股 / 结算交易日，供调度器与 API 共用。"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date

from . import iwencai_service
from .market import MarketData
from .models import Account, PaperStrategy
from .store import PaperStore
from .trading import process_day

logger = logging.getLogger(__name__)


def resolve_api_key(strategy: PaperStrategy) -> str:
    """策略级 api_key 优先，否则 IWENCAI_API_KEY 环境变量。"""
    key = (strategy.api_key or "").strip() or os.environ.get("IWENCAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未配置问财 API Key：请在环境变量 IWENCAI_API_KEY 或策略级 api_key 中设置")
    return key


def run_fetch(store: PaperStore, strategy: PaperStrategy, when: str = "") -> dict:
    """拉取一次问财选股并落盘当日快照。

    底层接口为异步；本函数由同步上下文（API 线程池 / APScheduler 后台线程）调用，
    故用 asyncio.run 将协程跑到底。
    """
    strategy.api_key = resolve_api_key(strategy)
    return asyncio.run(iwencai_service.fetch_and_persist(store, strategy, when))


def run_simulate(store: PaperStore, market: MarketData, strategy: PaperStrategy,
                 date_str: str | None = None, fallback_candidates: list[str] | None = None) -> dict:
    """结算一个交易日：读当日问财候选 + 账户 → 模拟成交 → 落盘。

    当天未拉问到财快照时，可传入 fallback_candidates（如手动指定）。
    """
    date_str = date_str or date.today().isoformat()
    accounts = store.load_accounts()
    account = accounts.get(strategy.account_id)
    if account is None:
        raise RuntimeError(f"策略 {strategy.name} 绑定的账户不存在: {strategy.account_id}")

    candidates = fallback_candidates
    iwencai_rows: dict | None = None
    if candidates is None:
        snap = store.load_iwencai_snapshot(date_str, strategy.id)
        candidates = (snap or {}).get("symbols", []) if snap else []
        iwencai_rows = (snap or {}).get("fields") if snap else None

    trades, snapshot = process_day(market, account, strategy, date_str, candidates, iwencai_rows)
    store.append_trades(strategy.id, trades)
    store.save_day(snapshot)
    _persist_account(store, account)
    return {
        "date": date_str, "candidates": len(candidates),
        "trades": len(trades),
        "buy": sum(1 for t in trades if t.side == "buy"),
        "sell": sum(1 for t in trades if t.side == "sell"),
        "cash": round(account.cash, 2),
        "positions": len(account.positions),
        "total_value": round(snapshot.total_value, 2),
        "nav": round(snapshot.nav, 4),
    }


def _persist_account(store: PaperStore, account: Account) -> None:
    accounts = store.load_accounts()
    accounts[account.id] = account
    store.save_accounts(accounts)