"""wencai MarketData 内存缓存语义 + 结算绕过缓存(force_refresh) 的回归测试。

背景(2026-09-30 事故): enriched 当日分区在盘中会被增量写入器反复重写; 集合竞价
阶段(09:25-09:30)无连续竞价成交, 数据源返回的 high/low 为空, 被 quote_service
用 close 兜底填充, 于是全市场 high==low==open==close。这份"盘前伪快照"一旦被
day_rows 缓存, 且策略 signal_ids 为空(缓存键 (date, ()) 与 api.py 的
day_rows(latest) 完全相同), 盘后结算就会命中它, 使 buyable_at_open 的
「最高=最低」一字板判定对全市场成立 → 候选被全部跳过 → 零买入。

修复: ① day_rows 增加 force_refresh, trading.process_day 结算时传 True 强制重读;
② day_rows 增加 cache=False, api.account_detail 的持仓现价兜底读取改用它, 从源头
不产生脏缓存(竞价窗口的轮询不再固化未定盘的当日分区)。
"""
from __future__ import annotations

from app.wencai.market import DayRow, MarketData


def test_day_rows_caches_until_force_refresh(tmp_path, monkeypatch):
    """同日同键命中缓存; force_refresh=True 强制重算并覆盖缓存。"""
    market = MarketData(tmp_path)
    calls = {"n": 0}

    def fake_compute(date: str, signal_ids: set[str]) -> dict[str, DayRow]:
        calls["n"] += 1
        # 第 1 次: 盘前伪快照(high=low=open, 无波动 → 被误判成一字板)
        # 第 2 次: 盘后定盘(有真实 high/low)
        high, low = (6.69, 6.69) if calls["n"] == 1 else (6.80, 6.39)
        return {"001330.SZ": DayRow("001330.SZ", open=6.69, close=6.69,
                                    volume=1_000_000.0, high=high, low=low)}

    monkeypatch.setattr(market, "_compute_day_rows", fake_compute)

    pre_open = market.day_rows("2026-09-30")
    assert calls["n"] == 1
    assert pre_open is not None
    # 伪一字板: 最高=最低 → 被 buyable_at_open 误杀
    assert market.buyable_at_open("001330.SZ", pre_open["001330.SZ"]) is False

    cached = market.day_rows("2026-09-30")  # 命中缓存, 不再重算
    assert calls["n"] == 1
    assert cached is pre_open

    settled = market.day_rows("2026-09-30", force_refresh=True)  # 绕过缓存重读
    assert calls["n"] == 2
    assert market.buyable_at_open("001330.SZ", settled["001330.SZ"]) is True


def test_empty_signal_ids_share_cache_key_with_default(tmp_path, monkeypatch):
    """空 signal_ids 与不传参同键 —— 正是与 api.py day_rows(latest) 撞车的条件。"""
    market = MarketData(tmp_path)
    calls = {"n": 0}

    def fake_compute(date: str, signal_ids: set[str]) -> dict[str, DayRow]:
        calls["n"] += 1
        return {}

    monkeypatch.setattr(market, "_compute_day_rows", fake_compute)
    market.day_rows("2026-09-30")
    market.day_rows("2026-09-30", set())
    assert calls["n"] == 1


def test_cache_false_does_not_poison_cache(tmp_path, monkeypatch):
    """cache=False 的读取(竞价窗口的持仓现价兜底)不得把伪快照写进缓存。

    api.account_detail 在 09:25-09:30 被前端轮询时读到的是盘前伪快照; 若它落进
    缓存, 盘后结算(signal_ids 为空 → 同键)就会命中它 → 零买入。改用 cache=False
    后, 结算读到的必须是盘后定盘数据。
    """
    market = MarketData(tmp_path)
    calls = {"n": 0}

    def fake_compute(date: str, signal_ids: set[str]) -> dict[str, DayRow]:
        calls["n"] += 1
        high, low = (6.69, 6.69) if calls["n"] == 1 else (6.80, 6.39)
        return {"001330.SZ": DayRow("001330.SZ", open=6.69, close=6.69,
                                    volume=1_000_000.0, high=high, low=low)}

    monkeypatch.setattr(market, "_compute_day_rows", fake_compute)

    pre_open = market.day_rows("2026-09-30", cache=False)   # 竞价窗口的兜底读取
    assert calls["n"] == 1
    assert market.buyable_at_open("001330.SZ", pre_open["001330.SZ"]) is False

    settled = market.day_rows("2026-09-30")                 # 结算: 未命中脏缓存
    assert calls["n"] == 2
    assert market.buyable_at_open("001330.SZ", settled["001330.SZ"]) is True


def test_limit_up_one_word_board_not_buyable_after_refresh(tmp_path, monkeypatch):
    """真一字板涨停(最高=最低=涨停价)在缓存与重读两种模式下都判不可买。"""
    market = MarketData(tmp_path)

    def fake_compute(date: str, signal_ids: set[str]) -> dict[str, DayRow]:
        return {"600825.SH": DayRow("600825.SH", open=10.35, close=10.35,
                                    volume=100_000.0, high=10.35, low=10.35,
                                    prev_close=9.41)}

    monkeypatch.setattr(market, "_compute_day_rows", fake_compute)
    cached = market.day_rows("2026-09-30")
    assert market.buyable_at_open("600825.SH", cached["600825.SH"]) is False
    refreshed = market.day_rows("2026-09-30", force_refresh=True)
    assert market.buyable_at_open("600825.SH", refreshed["600825.SH"]) is False