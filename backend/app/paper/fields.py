"""问财返回字段的归一化与条件判定。

问财列名常带日期后缀（如 ``dde大单净量[20260904]``、``涨跌幅[20260824-20260904]``），
这里按列名前缀解析为稳定 key，供买入条件（app.paper.models.FieldFilter）引用。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# 常见字段目录，供前端下拉与 /strategy/options 使用。
# return_Nd 为"涨跌幅"按区间内交易日近似的飘动 key，无法穷举，故这里列常见档位。
IWENCAI_FIELD_CATALOG: list[tuple[str, str, str]] = [
    ("dde_net", "DDE大单净量", "number"),
    ("return_1d", "1日区间涨跌幅", "number"),
    ("return_5d", "5日区间涨跌幅", "number"),
    ("return_10d", "10日区间涨跌幅", "number"),
    ("return_20d", "20日区间涨跌幅", "number"),
    ("auction_pct", "竞价涨幅", "number"),
    ("vol_ratio", "量比", "number"),
    ("board", "上市板块", "string"),
    ("name", "股票简称", "string"),
]

_CODE_RE = re.compile(r"(\d{6})")
_RANGE_RE = re.compile(r"(\d{8})-(\d{8})")
_COL_DATE_RE = re.compile(r"\[\d{8}(?:-\d{8})?\]\s*$")


def strip_col_date(k: str) -> str:
    """去掉问财列名末端的日期后缀，如 ``dde大单净量[20260904]`` → ``dde大单净量``。"""
    m = _COL_DATE_RE.search(k)
    return (k[:m.start()] + k[m.end():]).strip() if m else k


def _num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).replace("%", "").replace(",", "").strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _code(raw: Any) -> str | None:
    if raw is None:
        return None
    m = _CODE_RE.search(str(raw))
    return m.group(1) if m else None


def _period_days(start: str, end: str) -> int | None:
    """按区间内周一至周五计数，近似 A 股交易日数；无法解析返回 None。"""
    try:
        a = datetime.strptime(start, "%Y%m%d").date()
        b = datetime.strptime(end, "%Y%m%d").date()
    except ValueError:
        return None
    if b < a:
        return None
    n = 0
    cur = a
    while cur <= b:
        if cur.weekday() < 5:
            n += 1
        cur += date.resolution  # 一天
    return n


def normalize_row(raw: dict[str, Any]) -> dict[str, Any]:
    """把一条问财返回记录（含日期后缀列名）解析为稳定 key 字典。"""
    out: dict[str, Any] = {}
    for col, v in raw.items():
        col = str(col or "")
        if col.startswith("dde大单净量"):
            out["dde_net"] = _num(v)
        elif col.startswith("涨跌幅"):
            m = _RANGE_RE.search(col)
            if m:
                days = _period_days(m.group(1), m.group(2))
                if days is not None:
                    out[f"return_{days}d"] = _num(v)
        elif col.startswith("竞价涨幅"):
            out["auction_pct"] = _num(v)
        elif col.startswith("量比"):
            out["vol_ratio"] = _num(v)
        elif col.startswith("上市板块"):
            out["board"] = str(v)
        elif col.startswith("股票简称"):
            out["name"] = str(v).strip()
    return out


def normalize_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 6 位股票代码建立 {symbol: norm_dict} 索引（去重保序取首条）。"""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = _code(r.get("股票代码") or r.get("代码"))
        if code and code not in out:
            out[code] = normalize_row(r)
    return out


# ── 条件判定 ────────────────────────────────────────────


def evaluate_filter(norm: dict[str, Any], field: str, op: str, value: Any) -> bool:
    """判定单条归一化记录是否满足条件。字段缺失/不可转 → False。"""
    actual = norm.get(field)
    if actual is None or actual == "":
        return False
    if op in (">", ">=", "<", "<="):
        f = _num(actual)
        if f is None or not isinstance(value, (int, float)):
            return False
        if op == ">":
            return f > value
        if op == ">=":
            return f >= value
        if op == "<":
            return f < value
        return f <= value
    if op in ("==", "!="):
        # 数值/字符串通用：双方都能转数值则数值比较，否则字符串比较
        f = _num(actual)
        if f is not None and isinstance(value, (int, float)):
            return (f == value) if op == "==" else (f != value)
        s = str(actual)
        return (s == str(value)) if op == "==" else (s != str(value))
    # 字符串操作
    s = str(actual)
    if op == "contains":
        return str(value) in s
    if op == "not_contains":
        return str(value) not in s
    return False


def evaluate_filters(norm: dict[str, Any], filters) -> bool:
    """全部字段条件都满足才算字段来源通过。filters 为 FieldFilter 列表。"""
    for f in filters:
        if not evaluate_filter(norm, f.field, f.op, f.value):
            return False
    return True


# ── 动态字段目录（测试问财 / 快照回退） ──────────────────

_LABEL = {k: label for k, label, _ in IWENCAI_FIELD_CATALOG}


def _label_for(key: str, raw_col: str | None = None) -> str:
    return _LABEL.get(key) or raw_col or key


def fields_from_normalized(fields_map: dict[str, dict[str, Any]],
                           raw_by_symbol: dict[str, dict[str, Any]] | None = None) -> list[dict]:
    """从归一化字段索引归纳可用的字段目录 [{key,label,type}]。

    fields_map: {symbol: norm_dict}；raw_by_symbol: {symbol: raw列} 可选，用于 label 取原始列名。
    """
    raw_by_symbol = raw_by_symbol or {}
    keys: list[str] = []
    for norm in fields_map.values():
        for k in norm:
            if k not in keys:
                keys.append(k)
    out: list[dict] = []
    for k in keys:
        sample = next((norm.get(k) for norm in fields_map.values() if k in norm), None)
        raw_col = next(
            (col for symbol, raw in raw_by_symbol.items()
             for col, v in raw.items() if _key_of(col) == k),
            None)
        typ = "string" if _num(sample) is None else "number"
        out.append({"key": k, "label": _label_for(k, raw_col), "type": typ})
    return out


def _key_of(col: str) -> str | None:
    if col.startswith("dde大单净量"):
        return "dde_net"
    if col.startswith("涨跌幅"):
        m = _RANGE_RE.search(col)
        if m:
            days = _period_days(m.group(1), m.group(2))
            return f"return_{days}d" if days is not None else None
        return None
    if col.startswith("竞价涨幅"):
        return "auction_pct"
    if col.startswith("量比"):
        return "vol_ratio"
    if col.startswith("上市板块"):
        return "board"
    if col.startswith("股票简称"):
        return "name"
    return None


def available_fields(records: list[dict[str, Any]]) -> list[dict]:
    """对问财返回记录先归一化再归纳字段目录，供「测试问财」使用。"""
    if not records:
        return []
    fields_map = normalize_rows(records)
    raw_by_symbol: dict[str, dict[str, Any]] = {}
    for r in records:
        c = _code(r.get("股票代码") or r.get("代码"))
        if c:
            raw_by_symbol[c] = r
    return fields_from_normalized(fields_map, raw_by_symbol)