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
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
        # 保护 accounts/strategies 的"读-改-写"整体原子性：多个策略同一时刻结算
        # （scheduler 线程池）或结算与 API 写操作并发时，避免互相覆盖丢账户。
        self._lock = threading.Lock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """串行化账户/策略的读-改-写。调用方需自行把 load→改→save 整段包进来。

        只提供普通 Lock（不可重入）：持锁期间不要再次进入 `locked()`，
        也不要调用会自行加锁的 `_persist_account`。
        """
        with self._lock:
            yield

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """临时文件 + os.replace 原子落盘：任何时刻读到的要么是旧内容要么是新内容，
        不会出现被截断的半截 JSON（并发读拿不到内容就会误判为空）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # ── 账户 ────────────────────────────────────────────
    def load_accounts(self) -> dict[str, Account]:
        if not self._acct.exists():
            return {}
        try:
            raw = json.loads(self._acct.read_text(encoding="utf-8"))
        except Exception as e:
            # 绝不静默返回 {}：调用方随后 save_accounts 会用这个空字典覆盖整份
            # accounts.json，把其他账户一并抹掉（历史上 dde_02/dde_03 丢失的根因）。
            logger.error("accounts load failed: %s", e)
            raise RuntimeError(f"accounts.json 解析失败，已拒绝按空数据继续: {e}") from e
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
        self._atomic_write(
            self._acct,
            json.dumps([a.to_dict() for a in accounts.values()],
                       ensure_ascii=False, indent=2))

    # ── 策略 ────────────────────────────────────────────
    def load_strategies(self) -> dict[str, PaperStrategy]:
        if not self._strat.exists():
            return {}
        try:
            raw = json.loads(self._strat.read_text(encoding="utf-8"))
        except Exception as e:
            # 同 load_accounts：不得返回 {}，否则调用方保存时会把全部策略覆盖掉。
            logger.error("strategies load failed: %s", e)
            raise RuntimeError(f"strategies.json 解析失败，已拒绝按空数据继续: {e}") from e
        out: dict[str, PaperStrategy] = {}
        for s in raw if isinstance(raw, list) else []:
            if isinstance(s, dict):
                try:
                    strat = PaperStrategy.from_dict(s)
                except Exception as e:  # noqa: PERF203
                    logger.warning("skip bad strategy record: %s", e)
                    continue
                out[strat.id] = strat
        if any(not s.created_at for s in out.values()):
            self._backfill_created_at(out)
        return out

    def _backfill_created_at(self, strategies: dict[str, PaperStrategy]) -> None:
        """历史策略无 created_at：按该策略最早落盘日期（每日快照/选股快照）回填，
        无任何落盘记录则取今天。回填一次后立即持久化。"""
        import datetime
        changed = False
        for sid, strat in strategies.items():
            if strat.created_at:
                continue
            dates = [d.name for d in self._days.glob("*")
                     if d.is_dir() and (d / f"{sid}.json").exists()]
            dates += [d.name for d in self._snap.glob("*")
                      if d.is_dir() and (d / f"{sid}.json").exists()]
            strat.created_at = min(dates) if dates else datetime.date.today().isoformat()
            changed = True
        if changed:
            self.save_strategies(strategies)

    def save_strategies(self, strategies: dict[str, PaperStrategy]) -> None:
        self._atomic_write(
            self._strat,
            json.dumps([s.to_dict() for s in strategies.values()],
                       ensure_ascii=False, indent=2))

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

    def rewrite_trades(self, strategy_id: str, trades: list[dict[str, Any]]) -> None:
        """整文件原子重写流水（删除交易后回放重建用）。"""
        p = self._trades / f"{strategy_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        tmp.replace(p)

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