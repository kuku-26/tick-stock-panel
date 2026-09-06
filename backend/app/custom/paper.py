from __future__ import annotations

import logging

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.paper import context
from app.paper.api import router as paper_router
from app.paper.market import MarketData
from app.paper.scheduler import PaperScheduler
from app.paper.store import PaperStore

logger = logging.getLogger(__name__)

EXTENSION_ID = "paper.trading"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(paper_router)


def startup(context_: ExtensionContext) -> None:
    store = PaperStore(context_.data_dir)
    market = MarketData(context_.data_dir)
    scheduler = PaperScheduler(store, market)
    context.set_instances(store, market, scheduler)
    scheduler.start()
    logger.info("paper trading module started; strategies=%d",
                len(store.load_strategies()))