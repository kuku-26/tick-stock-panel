# -*- coding: utf-8 -*-
"""问财实盘模拟 — 例行任务交易日门控回归。

独立测试文件 (test_wencai_*): 不改动上游 test_paper_*.py, 便于 fork 同步。

回归背景: 调度器原先仅靠 CronTrigger(day_of_week="mon-fri") 出勤, 无交易日历,
2026-10-01(国庆)09:29 仍抓出 17 只基于上一交易日行情的假名单并落盘。现两个
例行任务出勤前先过交易日探针; 探针未知(None)时维持原行为。
"""
from __future__ import annotations

import types

import pytest

# context 先于 scheduler: 二者存在既有循环导入 (context→scheduler→context)。
# context 对 PaperScheduler 仅用于类型标注, 调度器只在函数内引用 context,
# 先加载 context 可安全打破环 (与 app/custom/wencai.py 的加载顺序一致)。
from app.wencai import context  # noqa: F401
from app.wencai import scheduler


class _FakeStore:
    def __init__(self, strategies: dict):
        self._strategies = strategies

    def load_strategies(self) -> dict:
        return self._strategies


@pytest.fixture
def wired(monkeypatch):
    """替换探针/上下文/落盘调用, 只观察例行任务是否真正出勤。"""
    calls: dict[str, list] = {"fetch": [], "simulate": []}
    ctx = types.SimpleNamespace(
        get_store=lambda: _FakeStore({"dde_01": types.SimpleNamespace(enabled=True)}),
        get_market=lambda: object(),
    )
    monkeypatch.setattr(scheduler, "context", ctx)
    monkeypatch.setattr(scheduler, "run_fetch",
                        lambda store, strategy, when: calls["fetch"].append(when) or {"count": 0})
    monkeypatch.setattr(scheduler, "run_simulate",
                        lambda store, market, strategy, date_str: calls["simulate"].append(date_str) or {})
    return calls


def test_fetch_skips_on_holiday(monkeypatch, wired):
    """工作日但休市(国庆): 不抓名单, 不落盘假数据。"""
    monkeypatch.setattr(scheduler.trading_day, "is_trading_day", lambda *a, **k: False)
    scheduler._fetch("dde_01")
    assert wired["fetch"] == []


def test_fetch_runs_on_trading_day(monkeypatch, wired):
    monkeypatch.setattr(scheduler.trading_day, "is_trading_day", lambda *a, **k: True)
    scheduler._fetch("dde_01")
    assert wired["fetch"] == ["daily"]


def test_fetch_runs_when_probe_unknown(monkeypatch, wired):
    """探针未知(None)维持原行为, 不误杀交易日。"""
    monkeypatch.setattr(scheduler.trading_day, "is_trading_day", lambda *a, **k: None)
    scheduler._fetch("dde_01")
    assert wired["fetch"] == ["daily"]


def test_fetch_runs_when_probe_raises(monkeypatch, wired):
    """探针异常不得阻断例行任务。"""
    def boom(*a, **k):
        raise RuntimeError("probe down")

    monkeypatch.setattr(scheduler.trading_day, "is_trading_day", boom)
    scheduler._fetch("dde_01")
    assert wired["fetch"] == ["daily"]


def test_simulate_skips_holiday_scheduled_run(monkeypatch, wired):
    """例行结算(date_str 为空)受交易日门控。"""
    monkeypatch.setattr(scheduler.trading_day, "is_trading_day", lambda *a, **k: False)
    scheduler._simulate("dde_01")
    assert wired["simulate"] == []


def test_simulate_explicit_date_ignores_holiday(monkeypatch, wired):
    """显式日期(手动/补跑)不受「今日是否交易日」门控。"""
    monkeypatch.setattr(scheduler.trading_day, "is_trading_day", lambda *a, **k: False)
    scheduler._simulate("dde_01", "2026-09-30")
    assert wired["simulate"] == ["2026-09-30"]