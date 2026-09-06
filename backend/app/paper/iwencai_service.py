"""问财选股服务：调用 iwencai_client 拉取自然语言选股结果，落盘并提取股票代码。

客户端库入口见 app.plugins.iwencai_client.IWencaiClient（已验证导入可用）。
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from app.plugins.iwencai_client import IWencaiClient

from .fields import normalize_rows, strip_col_date

logger = logging.getLogger(__name__)

# 问财返回数据中可能的股票代码列（按优先级，取第一个存在的）
_CODE_COLS = ("股票代码", "代码", "证券代码", "代码[2026]")
_PAD = "0"


def extract_symbols(df: pd.DataFrame) -> list[str]:
    """从问财返回的 DataFrame 提取 6 位股票代码列表（去重、保序）。"""
    if df is None or df.empty:
        return []
    col = None
    for name in _CODE_COLS:
        if name in df.columns:
            col = name
            break
    if col is None:
        # 兜底：找一个值几乎全是 6 位数字的列
        for c in df.columns:
            sample = df[c].dropna().astype(str).head(20)
            if len(sample) and all(s.strip().isdigit() for s in sample):
                col = c
                break
    if col is None:
        logger.warning("iwencai snapshot: 未找到股票代码列，结果可能为空")
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in df[col].dropna().astype(str):
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 6:
            if digits not in seen:
                seen.add(digits)
                out.append(digits)
    return out


def run_query(query: str, api_key: str, sort_key: str | None = None) -> tuple[pd.DataFrame, list[str]]:
    """执行一次问财选股，返回 (原始清洗后的 DataFrame, 股票代码列表)。"""
    client = IWencaiClient()
    df = client.get(query=query, api_key=api_key, sort_key=sort_key, sort_order="desc")
    return df, extract_symbols(df)


async def fetch_and_persist(store, strategy, when: str = "") -> dict[str, Any]:
    """拉取一个策略的问财结果并落盘。同步调用会阻塞事件循环，但本模块按
    时间调度在后台线程执行，可接受。

    返回汇总信息；网络/网关异常时抛 IWencaiAPIError。
    """
    df, symbols = run_query(strategy.iwencai_query, strategy.api_key)
    records = df.to_dict(orient="records") if not df.empty else []
    # 落盘前清洗原始列名：去掉问财返回的日期后缀，如 dde大单净量[20260904] → dde大单净量
    records = [{strip_col_date(str(k)): v for k, v in r.items()} for r in records]
    payload: dict[str, Any] = {
        "strategy_id": strategy.id,
        "bucket": when,
        "symbols": symbols,
        "count": len(symbols),
        "rows": records,
        # 归一化后的字段（按 symbol 索引），供买入条件判定，无需重算原始列
        "fields": normalize_rows(records),
    }
    store.save_iwencai_snapshot(_today(), strategy.id, payload)
    return {"symbols": symbols, "count": len(symbols)}


def _today() -> str:
    from datetime import date
    return date.today().isoformat()