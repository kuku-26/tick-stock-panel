"""行情数据抽象：读取项目日线 enriched + 个股维表，供模拟成交定价与信号求值。

对外提供纯数据接口，便于单测注入假行情（不依赖真实仓库/网络）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

import polars as pl

from app.parquet import scan_enriched_parquet

logger = logging.getLogger(__name__)

# enriched 分区只落盘原始列(指标与 csg_* 信号列按项目设计仅在内存计算)。
# 结算求值信号需回看足够长的历史窗口, 覆盖 ma60/boll/macd 等长窗口指标与信号日期偏移。
_DAY_ROWS_LOOKBACK = 120

# day_rows 直接从磁盘读取的基础列(ENRICHED_STORAGE_SCHEMA 的子集)
_DISK_COLS = ("symbol", "date", "open", "high", "low", "close", "volume",
              "amount", "turnover_rate", "consecutive_limit_ups",
              "consecutive_limit_downs")


@dataclass
class DayRow:
    """某标的在某交易日的 enriched 行快照（价格均为前复权）。"""

    symbol: str
    open: float
    close: float
    volume: float
    high: float | None = None
    low: float | None = None
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
        self._rows_cache: dict[tuple[str, tuple[str, ...]], dict[str, DayRow]] = {}

    # ── 涨跌停价 ────────────────────────────────────────
    # 涨跌停判定统一用板块比例推算：prev_close 与 open 同为前复权价，比例判定
    # 在复权空间自洽。不使用 instruments 维表的 limit_up——那是静态快照的真实价
    # （不复权且会过期），与前复权行情比较属于口径错配（2026-09-22 中国长城
    # 涨停开盘被误判可买的根因之一）。推算价按交易所口径取整到分（tick 0.01），
    # 容差 0.005（半分钱）覆盖交易所四舍五入与 Python 银行家舍入的差异。
    @staticmethod
    def _heuristic_limit_up(symbol: str, prev_close: float | None) -> float | None:
        if prev_close is None or prev_close <= 0:
            return None
        return MarketData._limit_price(symbol, prev_close, +1)

    @staticmethod
    def _limit_price(symbol: str, prev_close: float, direction: int) -> float | None:
        """按板块涨跌幅限制推算涨停(+1)/跌停(-1)价。ST 的 5% 档无法从代码判断, 按
        主板口径处理 (与涨停判定同口径, 不引入额外偏差)。"""
        if prev_close is None or prev_close <= 0:
            return None
        if symbol.startswith(("300", "301", "688")):
            pct = 0.20
        elif symbol.startswith(("8", "4")):
            pct = 0.30
        else:
            pct = 0.10
        return round(prev_close * (1 + direction * pct), 2)

    # ── enriched 日线 ─────────────────────────────────
    def day_rows(self, date: str,
                 signal_ids: set[str] | None = None) -> dict[str, DayRow]:
        """读取指定交易日的行情行(含信号), 返回 {symbol: DayRow}。

        enriched 分区不含指标/信号列, 因此读取 date 及其前 _DAY_ROWS_LOOKBACK
        个交易日的原始数据, 复用 indicators 管线按需计算 signal_ids 对应的
        csg_* 信号列后取当日行。结果按 (date, signal_ids) 缓存。
        """
        sig_key = tuple(sorted(signal_ids or ()))
        cache_key = (date, sig_key)
        if cache_key in self._rows_cache:
            return self._rows_cache[cache_key]
        rows = self._compute_day_rows(date, set(sig_key))
        self._rows_cache[cache_key] = rows
        return rows

    def _compute_day_rows(self, date: str, signal_ids: set[str]) -> dict[str, DayRow]:
        part_dates = sorted(p.name.removeprefix("date=")
                            for p in self._enriched.glob("date=*") if p.is_dir())
        window = [d for d in part_dates if d <= date][-(_DAY_ROWS_LOOKBACK + 1):]
        # 当日分区缺失 = 行情未就绪(盘后管道未完成 / 非交易日)
        if not window or window[-1] != date:
            return {}
        files = [str(f) for d in window
                 for f in (self._enriched / f"date={d}").glob("*.parquet")]
        if not files:
            return {}
        try:
            lf = scan_enriched_parquet(files)
            cols = [c for c in _DISK_COLS if c in lf.collect_schema().names()]
            df = lf.select(cols).collect()
        except Exception as e:
            logger.warning("enriched day %s read failed: %s", date, e)
            return {}
        if df.is_empty() or "symbol" not in df.columns:
            return {}

        df = self._attach_signals(df, signal_ids)

        target = _date.fromisoformat(date)
        today = df.filter(pl.col("date") == target)
        out: dict[str, DayRow] = {}
        csg_cols = [c for c in today.columns if c.startswith("csg_")]
        for r in today.to_dicts():
            sym = str(r.get("symbol"))
            if not sym:
                continue
            csg = {c: bool(r.get(c)) for c in csg_cols} if csg_cols else {}
            out[sym] = DayRow(
                symbol=sym,
                open=_f(r.get("open")),
                close=_f(r.get("close")),
                volume=_f(r.get("volume")),
                high=_f(r.get("high")),
                low=_f(r.get("low")),
                prev_close=_f(r.get("prev_close")),
                csg=csg or None,
            )
        return out

    @staticmethod
    def _attach_signals(df: pl.DataFrame, signal_ids: set[str]) -> pl.DataFrame:
        """按需计算 prev_close 指标列与 csg_* 信号列(复用主 indicators 管线)。

        信号编译/求值失败时仅告警降级(保留基础行情), 不阻断结算定价。
        """
        try:
            from app.indicators.pipeline import (INDICATOR_COLUMNS,
                                                 compute_indicators,
                                                 compute_signals,
                                                 get_signal_dependencies)
        except Exception as e:
            logger.warning("indicators pipeline unavailable: %s", e)
            return df
        ind_need: set[str] = {"prev_close"}
        needed_cols: set[str] = set()
        if signal_ids:
            needed_cols = {f"csg_{s}" for s in signal_ids}
            deps = get_signal_dependencies()
            roots: set[str] = set()
            for col in needed_cols:
                roots |= deps.get(col, frozenset())
            # 信号条件可引用注册表因子 (如虚拟因子 ma10_bias), 其自身不是
            # 指标列, 但计算依赖指标列 (ma10); 需展开因子依赖后并入
            # ind_need, 否则 materialize_scoring_columns 因缺列静默跳过,
            # 信号求值始终为 False。
            from app.factors.registry import factor_dependencies
            for _ in range(4):  # 依赖链展开 (virtual→base), 防御性深度上限
                expanded = set(factor_dependencies(sorted(roots)))
                if expanded <= roots:
                    break
                roots |= expanded
            ind_need |= roots & set(INDICATOR_COLUMNS)
        try:
            df = compute_indicators(df, ind_need)
            if needed_cols:
                df = compute_signals(df, needed_cols)
        except Exception as e:
            logger.warning("signal compute failed: %s", e)
        return df

    # ── 可成交判断 ──────────────────────────────────
    def buyable_at_open(self, symbol: str, row: DayRow | None) -> bool:
        """开盘是否可买入：有行情、非停牌、非一字板、开盘未封涨停。"""
        if row is None:
            return False
        if row.open is None or row.open <= 0 or row.volume is None or row.volume <= 0:
            return False  # 停牌 / 无成交
        if row.high is not None and row.low is not None \
                and row.high > 0 and row.low > 0 \
                and abs(row.high - row.low) <= row.high * 1e-6:
            return False  # 一字板(最高=最低, 全天封死), 开盘无法买到
        limit = self._heuristic_limit_up(symbol, row.prev_close)
        if limit and row.open >= limit - 0.005:
            return False  # 开盘即封涨停，无法买到
        return True

    def sellable_at_open(self, symbol: str, row: DayRow | None) -> bool:
        """当日是否可卖出：有行情有成交，且开盘未封跌停。

        开盘即封跌停（含一字跌停）时卖单无法成交，当日不卖（继续持有，
        等待后续交易日再按规则判定）。
        """
        if row is None:
            return False
        if row.close is None or row.close <= 0:
            return False  # 停牌 / 无成交
        limit_dn = self._limit_price(symbol, row.prev_close, -1)
        return not (limit_dn is not None and row.open is not None and row.open > 0
                    and row.open <= limit_dn + 0.005)  # 开盘即封跌停则不可卖

    def limit_down_open_sell_price(self, symbol: str, row: DayRow | None) -> float | None:
        """「跌停打开卖出」成交价：开盘封跌停且盘中打开（高点>跌停价）→ 跌停价；否则 None。

        卖单按跌停价排队，盘中打开即按排队价（跌停价）成交，而非打开后的更高价。
        需要当日完整盘口（high），故仅限盘后结算时启用。
        """
        if row is None or row.open is None or row.open <= 0 \
                or row.close is None or row.close <= 0:
            return None  # 停牌 / 无成交
        dn = self._limit_price(symbol, row.prev_close, -1)
        if dn is None or row.open > dn + 0.005:
            return None  # 开盘不是跌停价
        high = row.high if (row.high is not None and row.high > 0) else None
        if high is None or high <= dn + 0.005:
            return None  # 全天封死，盘中未打开
        return dn

    def limit_up_open_buy_price(self, symbol: str, row: DayRow | None) -> float | None:
        """「涨停打开买入」成交价：开盘封涨停且盘中打开（低点<涨停价）→ 涨停价；否则 None。

        买单按涨停价排队，盘中打开即按排队价（涨停价）成交，而非打开后的更低价。
        涨停价口径与 buyable_at_open 一致（板块比例推算, 与行情同复权口径）。
        """
        if row is None or row.open is None or row.open <= 0 \
                or row.volume is None or row.volume <= 0:
            return None  # 停牌 / 无成交
        limit = self._heuristic_limit_up(symbol, row.prev_close)
        if limit is None or row.open < limit - 0.005:
            return None  # 开盘不是涨停价
        low = row.low if (row.low is not None and row.low > 0) else None
        if low is None or low >= limit - 0.005:
            return None  # 全天封死，盘中未打开
        return limit

    def latest_date(self) -> str | None:
        """enriched 最新分区日期(通常为最近一个已落盘交易日); 无数据返回 None。"""
        ds = sorted(p.name.removeprefix("date=")
                    for p in self._enriched.glob("date=*") if p.is_dir())
        return ds[-1] if ds else None


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
