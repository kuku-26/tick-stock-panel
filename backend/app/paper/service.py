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
from .trading import process_day, strategy_signal_ids

logger = logging.getLogger(__name__)


class MarketDataNotReadyError(RuntimeError):
    """结算日 enriched 日线尚未落盘(盘后管道未完成), 无法定价。"""


def resolve_api_key(strategy: PaperStrategy) -> str:
    """策略级 api_key 优先，否则 IWENCAI_API_KEY 环境变量。"""
    key = (strategy.api_key or "").strip() or os.environ.get("IWENCAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未配置问财 API Key：请在环境变量 IWENCAI_API_KEY 或策略级 api_key 中设置")
    return key


def run_fetch(store: PaperStore, strategy: PaperStrategy, when: str = "") -> dict:
    """拉取一次问财选股并落盘当日快照。

    同问句去重：本策略当日快照缺失/为空，且已有其他同问句策略的非空快照时，
    直接复用其数据落盘（跳过网关调用）——避免同 key 同分钟并发同款请求触发
    网关偶发空结果，也节省调用次数。存储仍按策略独立落盘，读取方无需感知。

    底层接口为异步；本函数由同步上下文（API 线程池 / APScheduler 后台线程）调用，
    故用 asyncio.run 将协程跑到底。
    """
    today = date.today().isoformat()
    own = store.load_iwencai_snapshot(today, strategy.id)
    if not (own and own.get("count")):
        alt = _fallback_snapshot(store, strategy, today)
        if alt is not None:
            payload = {**alt, "strategy_id": strategy.id,
                       "bucket": when or alt.get("bucket", "daily")}
            store.save_iwencai_snapshot(today, strategy.id, payload)
            logger.info("策略 %s 复用同问句策略的当日选股快照(%d 只), 跳过网关调用",
                        strategy.name, payload.get("count", 0))
            return {"symbols": payload.get("symbols", []),
                    "count": payload.get("count", 0), "reused": True}
    strategy.api_key = resolve_api_key(strategy)
    return asyncio.run(iwencai_service.fetch_and_persist(store, strategy, when))


def run_simulate(store: PaperStore, market: MarketData, strategy: PaperStrategy,
                 date_str: str | None = None, fallback_candidates: list[str] | None = None) -> dict:
    """结算一个交易日：读当日问财候选 + 账户 → 模拟成交 → 落盘。

    当天未拉问到财快照时，可传入 fallback_candidates（如手动指定）。
    结算日 enriched 日线未落盘时抛 MarketDataNotReadyError(盘后管道未完成,
    静默空结算会掩盖"结算没生效"的问题)。
    """
    date_str = date_str or date.today().isoformat()
    accounts = store.load_accounts()
    account = accounts.get(strategy.account_id)
    if account is None:
        raise RuntimeError(f"策略 {strategy.name} 绑定的账户不存在: {strategy.account_id}")

    if not market.day_rows(date_str, strategy_signal_ids(strategy)):
        raise MarketDataNotReadyError(
            f"{date_str} 的行情数据尚未就绪（盘后管道未完成落盘），无法结算；"
            "请等待盘后管道完成后再试")

    candidates = fallback_candidates
    iwencai_rows: dict | None = None
    if candidates is None:
        snap = store.load_iwencai_snapshot(date_str, strategy.id)
        candidates = (snap or {}).get("symbols", []) if snap else []
        iwencai_rows = (snap or {}).get("fields") if snap else None
        if not candidates:
            alt = _fallback_snapshot(store, strategy, date_str)
            if alt is not None:
                candidates = alt.get("symbols", [])
                iwencai_rows = alt.get("fields")

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


def _fallback_snapshot(store: PaperStore, strategy: PaperStrategy,
                       date_str: str) -> dict | None:
    """本策略当日选股快照缺失或为空时，复用同问句、已启用策略的非空快照。

    场景：多个策略共用同一问句与抓取时间，网关偶发对同款并发请求返回空结果，
    导致单策略当日无候选而错过买入；fields 按股票代码索引、与策略无关，可直接复用。
    """
    for sid, other in store.load_strategies().items():
        if (sid == strategy.id or not other.enabled
                or other.iwencai_query != strategy.iwencai_query):
            continue
        snap = store.load_iwencai_snapshot(date_str, sid)
        if snap and snap.get("count"):
            logger.warning("策略 %s 当日选股快照为空, 复用同问句策略 %s 的快照(%d 只)",
                           strategy.name, other.name, snap.get("count"))
            return snap
    return None


def _persist_account(store: PaperStore, account: Account) -> None:
    accounts = store.load_accounts()
    accounts[account.id] = account
    store.save_accounts(accounts)