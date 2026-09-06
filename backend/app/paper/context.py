"""paper 模块运行时上下文：store / market / scheduler 共享单例。

由 backend/app/custom/paper.py 的 startup 钩子填充；API 与调度器读取。避免依赖
app.state（custom 扩展只拿到只读 ExtensionContext）。
"""
from __future__ import annotations

from .market import MarketData
from .scheduler import PaperScheduler
from .store import PaperStore

_store: PaperStore | None = None
_market: MarketData | None = None
_scheduler: PaperScheduler | None = None


def set_instances(store: PaperStore, market: MarketData, scheduler: PaperScheduler) -> None:
    global _store, _market, _scheduler
    _store, _market, _scheduler = store, market, scheduler


def get_store() -> PaperStore:
    if _store is None:
        raise RuntimeError("paper module not initialized")
    return _store


def get_market() -> MarketData:
    if _market is None:
        raise RuntimeError("paper module not initialized")
    return _market


def get_scheduler() -> PaperScheduler | None:
    return _scheduler