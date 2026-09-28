from __future__ import annotations

import logging

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)
from app.wencai import context
from app.wencai.api import router as wencai_router
from app.wencai.market import MarketData
from app.wencai.scheduler import PaperScheduler
from app.wencai.store import PaperStore

logger = logging.getLogger(__name__)

EXTENSION_ID = "wencai.trading"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(wencai_router)


def startup(context_: ExtensionContext) -> None:
    store = PaperStore(context_.data_dir)
    market = MarketData(context_.data_dir)
    scheduler = PaperScheduler(store, market)
    context.set_instances(store, market, scheduler)
    scheduler.start()
    logger.info("wencai trading module started; strategies=%d",
                len(store.load_strategies()))