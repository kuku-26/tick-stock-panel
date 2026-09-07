"""问财实盘模拟的定时调度：按交易日出勤的执行两个任务。

  - fetch_job:  在策略 fetch_time（默认 09:25）拉取问财选股并落盘
  - simulate_job: 在策略 simulate_time（默认 15:30）结算当日行情

结算依赖当日 enriched 日线(盘后管道落盘, 默认 15:35 才开始跑), 因此
simulate_time 建议不早于盘后管道完成时间; 行情未就绪时本次结算跳过,
不自动重试。

调度失败只记录日志，绝不破坏主程序；单个策略故障不影响其它策略。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import context
from .market import MarketData
from .service import MarketDataNotReadyError, run_fetch, run_simulate
from .store import PaperStore

logger = logging.getLogger(__name__)

TZ = "Asia/Shanghai"


def _fetch(strategy_id: str) -> None:
    try:
        store = context.get_store()
        strategy = store.load_strategies().get(strategy_id)
        if strategy is None or not strategy.enabled:
            logger.info("paper fetch %s: 策略不存在或未启用, 跳过", strategy_id)
            return
        result = run_fetch(store, strategy, when="daily")
        logger.info("paper fetch %s: 命中 %d", strategy_id, result.get("count", 0))
    except Exception as e:
        logger.exception("paper fetch %s failed: %s", strategy_id, e)


def _simulate(strategy_id: str, date_str: str | None = None) -> None:
    """结算一个策略; 行情未就绪时记录告警并跳过本次结算(不重试)。"""
    try:
        store = context.get_store()
        strategy = store.load_strategies().get(strategy_id)
        if strategy is None or not strategy.enabled:
            logger.info("paper simulate %s: 策略不存在或未启用, 跳过", strategy_id)
            return
        result = run_simulate(store, context.get_market(), strategy, date_str)
        logger.info("paper simulate %s: %s", strategy_id, result)
    except MarketDataNotReadyError as e:
        logger.warning("paper simulate %s: 行情数据未就绪, 本次结算跳过: %s",
                       strategy_id, e)
    except Exception as e:
        logger.exception("paper simulate %s failed: %s", strategy_id, e)


class PaperScheduler:
    """例行调度器。策略 CRUD 后调用 reload() 重建触发。"""

    def __init__(self, store: PaperStore, market: MarketData):
        self._store = store
        self._market = market
        self._sched = BackgroundScheduler(timezone=TZ)

    def start(self) -> None:
        self._schedule_all()
        if not self._sched.running:
            self._sched.start()
        logger.info("paper scheduler started: %d strategies",
                    len(self._store.load_strategies()))

    def reload(self) -> None:
        if not self._sched.get_jobs() and not self._sched.running:
            self.start()
            return
        self._schedule_all()

    def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)
        logger.info("paper scheduler stopped")

    # ── 内部 ────────────────────────────────────────────
    def _schedule_all(self) -> None:
        strategies = self._store.load_strategies()
        # 移除旧的 paper_* cron job 后按当前策略重注册(幂等)。
        for job in self._sched.get_jobs():
            if job.id.startswith("paper_"):
                try:
                    self._sched.remove_job(job.id)
                except Exception:
                    pass
        for sid, s in strategies.items():
            if not s.enabled:
                continue
            fh, fm = _hhmm(s.fetch_time)
            sh, sm = _hhmm(s.simulate_time)
            self._sched.add_job(
                _fetch, trigger=CronTrigger(day_of_week="mon-fri", hour=fh, minute=fm,
                                            timezone=TZ),
                id=f"paper_fetch_{sid}", misfire_grace_time=1800,
                args=[sid], replace_existing=True,
            )
            self._sched.add_job(
                _simulate, trigger=CronTrigger(day_of_week="mon-fri", hour=sh, minute=sm,
                                               timezone=TZ),
                id=f"paper_sim_{sid}", misfire_grace_time=3600,
                args=[sid], replace_existing=True,
            )


def _hhmm(t: str) -> tuple[int, int]:
    hour, _, minute = t.partition(":")
    try:
        return int(hour), int(minute or "0")
    except ValueError:
        return 9, 25