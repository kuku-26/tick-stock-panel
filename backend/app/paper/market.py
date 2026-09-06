"""行情数据抽象：读取项目日线 enriched + 个股维表，供模拟成交定价与信号求值。

对外提供纯数据接口，便于单测注入假行情（不依赖真实仓库/网络）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from app.parquet import scan_enriched_parquet

logger = logging.getLogger(__name__)


@dataclass
class DayRow:
    """某标的在某交易日的 enriched 行快照（价格均为前复权）。"""

    symbol: str
    open: float
    close: float
    volume: float
    prev_close: float | None = None
    csg: dict[str, bool] = None  # csg_<id> -> bool；None 表示当日无该信号列

    def signal(self, signal_id: str) -> bool:
        col = f"csg_{signal_id}"
        if not self.csg:
            return False
        return bool(self.csg.get(col, False))


class MarketData:
    """从项目 parquet 读行情。storage_dir 指向 app 的 data 目录。"""

    def __init__(self, storage_dir: Path):
        self._enriched = storage_dir / "kline_daily_enriched"
        self._inst = storage_dir / "instruments" / "instruments.parquet"
        self._limit_up: dict[str, float] = {}
        self._load_instruments()

    # ── instruments / 涨停价 ────────────────────────────
    def _load_instruments(self) -> None:
        try:
            if not self._inst.exists():
                return
            df = pl.read_parquet(self._inst).select(
                [c for c in ("symbol", "limit_up") if c in pl.read_parquet(self._inst).columns]
            )
            if "symbol" in df.columns and "limit_up" in df.columns:
                rows = df.to_dicts()
                self._limit_up = {r["symbol"]: float(r["limit_up"])
                                  for r in rows if r.get("limit_up")}
        except Exception as e:
            logger.warning("instruments load failed: %s", e)

    def symbol_limit_up(self, symbol: str) -> float | None:
        """标的当日涨停价；无维表时按板块启发式推算。"""
        v = self._limit_up.get(symbol)
        if v is not None:
            return v
        return None

    @staticmethod
    def _heuristic_limit_up(symbol: str, prev_close: float | None) -> float | None:
        if prev_close is None or prev_close <= 0:
            return None
        if symbol.startswith(("300", "301", "688")):
            pct = 0.20
        elif symbol.startswith(("8", "4")):
            pct = 0.30
        else:
            pct = 0.10
        return round(prev_close * (1 + pct), 3)

    # ── enriched 日线 ───────────────────────────────────
    def day_rows(self, date: str) -> dict[str, DayRow]:
        """读取指定交易日的 enriched 全市场行，返回 {symbol: DayRow}。"""
        part = self._enriched / f"date={date}"
        if not part.exists():
            return {}
        files = sorted(part.glob("*.parquet"))
        if not files:
            return {}
        try:
            sources = [str(f) for f in files]
            cols = scan_enriched_parquet(sources).collect_schema().names()
            df = scan_enriched_parquet(sources, columns=cols).collect()
        except Exception as e:
            logger.warning("enriched day %s read failed: %s", date, e)
            return {}
        if df.is_empty() or "symbol" not in df.columns:
            return {}

        out: dict[str, DayRow] = {}
        csg_cols = [c for c in df.columns if c.startswith("csg_")]
        for r in df.to_dicts():
            sym = str(r.get("symbol"))
            if not sym:
                continue
            csg = {c: bool(r.get(c)) for c in csg_cols} if csg_cols else {}
            out[sym] = DayRow(
                symbol=sym,
                open=_f(r.get("open")),
                close=_f(r.get("close")),
                volume=_f(r.get("volume")),
                prev_close=_f(r.get("prev_close")),
                csg=csg or None,
            )
        return out

    # ── 可成交判断 ──────────────────────────────────────
    def buyable_at_open(self, symbol: str, row: DayRow | None) -> bool:
        """次日开盘是否可买入：有行情、非停牌、开盘未封涨停。"""
        if row is None:
            return False
        if row.open is None or row.open <= 0 or row.volume is None or row.volume <= 0:
            return False  # 停牌 / 无成交
        limit = self.symbol_limit_up(symbol) or self._heuristic_limit_up(symbol, row.prev_close)
        if limit and row.open >= limit - 0.001:
            return False  # 开盘即封涨停，无法买到
        return True

    def sellable_at_close(self, row: DayRow | None) -> bool:
        """当日收盘是否可卖出：有行情且有成交。"""
        if row is None:
            return False
        return row.close is not None and row.close > 0


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None