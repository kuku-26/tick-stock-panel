"""文件持久化：账户 / 策略 / 每日问财快照 / 每日账户快照 / 逐笔成交。

目录布局（data/paper/）：
  accounts.json / strategies.json
  iwencai_snapshot/<date>/<strategy_id>.json
  days/<date>/<strategy_id>.json
  trades/<strategy_id>.jsonl
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import Account, DaySnapshot, PaperStrategy, TradeRecord

logger = logging.getLogger(__name__)


class PaperStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "paper"
        self._acct = self.root / "accounts.json"
        self._strat = self.root / "strategies.json"
        self._snap = self.root / "iwencai_snapshot"
        self._days = self.root / "days"
        self._trades = self.root / "trades"

    # ── 账户 ────────────────────────────────────────────
    def load_accounts(self) -> dict[str, Account]:
        if not self._acct.exists():
            return {}
        try:
            raw = json.loads(self._acct.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("accounts load failed: %s", e)
            return {}
        out: dict[str, Account] = {}
        for a in raw if isinstance(raw, list) else []:
            if isinstance(a, dict):
                try:
                    acc = Account.from_dict(a)
                except Exception as e:  # noqa: PERF203
                    logger.warning("skip bad account record: %s", e)
                    continue
                out[acc.id] = acc
        return out

    def save_accounts(self, accounts: dict[str, Account]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._acct.write_text(
            json.dumps([a.to_dict() for a in accounts.values()],
                       ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 策略 ────────────────────────────────────────────
    def load_strategies(self) -> dict[str, PaperStrategy]:
        if not self._strat.exists():
            return {}
        try:
            raw = json.loads(self._strat.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("strategies load failed: %s", e)
            return {}
        out: dict[str, PaperStrategy] = {}
        for s in raw if isinstance(raw, list) else []:
            if isinstance(s, dict):
                try:
                    strat = PaperStrategy.from_dict(s)
                except Exception as e:  # noqa: PERF203
                    logger.warning("skip bad strategy record: %s", e)
                    continue
                out[strat.id] = strat
        return out

    def save_strategies(self, strategies: dict[str, PaperStrategy]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._strat.write_text(
            json.dumps([s.to_dict() for s in strategies.values()],
                       ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 每日问财选股快照 ────────────────────────────────
    def snapshot_dir(self, d: str) -> Path:
        return self._snap / d

    def save_iwencai_snapshot(self, d: str, strategy_id: str, payload: dict[str, Any]) -> Path:
        p = self.snapshot_dir(d) / f"{strategy_id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("iwencai snapshot saved: %d symbols (%s, %s)",
                    len(payload.get("symbols", [])), d, strategy_id)
        return p

    def load_iwencai_snapshot(self, d: str, strategy_id: str) -> dict[str, Any] | None:
        p = self.snapshot_dir(d) / f"{strategy_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("snapshot load failed %s: %s", p, e)
            return None

    def list_iwencai_dates(self, strategy_id: str) -> list[str]:
        out: list[str] = []
        for d in self._snap.glob("*"):
            if (d / f"{strategy_id}.json").exists():
                out.append(d.name)
        return sorted(out)

    # ── 每日账户快照 ────────────────────────────────────
    def save_day(self, snap: DaySnapshot) -> Path:
        p = self._days / snap.date / f"{snap.strategy_id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p

    def load_day(self, d: str, strategy_id: str) -> dict[str, Any] | None:
        p = self._days / d / f"{strategy_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("day snapshot load failed %s: %s", p, e)
            return None

    def list_day_dates(self, strategy_id: str) -> list[str]:
        out: list[str] = []
        for d in self._days.glob("*"):
            if d.is_dir() and (d / f"{strategy_id}.json").exists():
                out.append(d.name)
        return sorted(out)

    # ── 逐笔成交 ────────────────────────────────────────
    def append_trades(self, strategy_id: str, trades: list[TradeRecord]) -> None:
        if not trades:
            return
        p = self._trades / f"{strategy_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")

    def load_trades(self, strategy_id: str) -> list[dict[str, Any]]:
        p = self._trades / f"{strategy_id}.jsonl"
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception as e:
                    logger.warning("trade line parse failed: %s", e)
        return out

    # ── 级联删除策略相关联的数据 ────────────────────────
    def delete_strategy_data(self, strategy_id: str) -> None:
        """删除一个策略的全部落盘数据：问财快照、每日账户快照、逐笔成交。"""
        removed = 0
        for date_dir in (self._snap, self._days):
            if not date_dir.is_dir():
                continue
            for d in date_dir.glob("*"):
                fp = d / f"{strategy_id}.json"
                if fp.exists():
                    fp.unlink()
                    removed += 1
                # 清空后清理空日期目录
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        trades_file = self._trades / f"{strategy_id}.jsonl"
        if trades_file.exists():
            trades_file.unlink()
            removed += 1
        if removed:
            logger.info("paper delete strategy data %s: removed %d files", strategy_id, removed)