# -*- coding: utf-8 -*-
"""问财实盘模拟 — 落盘安全与并发回归（原子写 / 读改写锁 / 损坏文件 fail-closed）。

独立测试文件（test_wencai_*）：不改动上游 test_paper_*.py，便于 fork 同步。

回归背景：多策略同一时刻结算时，accounts.json 的「读-改-写」无锁 + 非原子写 +
解析失败静默返回 {} 后整文件覆盖，会抹掉并发写入的账户（dde_02/dde_03 丢失根因）。
"""
from __future__ import annotations

import json
import threading

import pytest

from app.wencai.models import Account
from app.wencai.service import _persist_account
from app.wencai.store import PaperStore


# ── 目录布局 ────────────────────────────────────────────
def test_store_root_is_wencai(tmp_path):
    assert PaperStore(tmp_path).root == tmp_path / "wencai"


# ── 原子写 ──────────────────────────────────────────────
def test_atomic_write_leaves_no_tmp_file(tmp_path):
    store = PaperStore(tmp_path)
    store.save_accounts({"a": Account.create("a", "A", 100000.0)})
    assert store._acct.exists()
    assert list(store.root.glob("*.tmp")) == []
    assert json.loads(store._acct.read_text(encoding="utf-8"))[0]["id"] == "a"


# ── 损坏文件 fail-closed（绝不返回空数据后覆盖） ─────────
def test_load_accounts_fails_closed_on_corrupt_json(tmp_path):
    store = PaperStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    broken = "{ this is not json"
    store._acct.write_text(broken, encoding="utf-8")
    with pytest.raises(RuntimeError, match="accounts.json 解析失败"):
        store.load_accounts()
    assert store._acct.read_text(encoding="utf-8") == broken


def test_load_strategies_fails_closed_on_corrupt_json(tmp_path):
    store = PaperStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store._strat.write_text("[1,", encoding="utf-8")
    with pytest.raises(RuntimeError, match="strategies.json 解析失败"):
        store.load_strategies()


def test_load_returns_empty_when_file_absent(tmp_path):
    """文件不存在是正常的首次运行，不是错误。"""
    assert PaperStore(tmp_path).load_accounts() == {}


# ── locked() 串行化读-改-写 ─────────────────────────────
def test_locked_serializes_concurrent_read_modify_write(tmp_path):
    store = PaperStore(tmp_path)
    store.save_accounts({"base": Account.create("base", "base", 1000.0)})

    def add(i: int) -> None:
        with store.locked():
            accounts = store.load_accounts()
            accounts[f"a{i}"] = Account.create(f"a{i}", f"A{i}", 1000.0)
            store.save_accounts(accounts)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.load_accounts()) == 9  # base + 8，无丢失更新


# ── _persist_account 并发不丢账户（dde_02/dde_03 回归） ──
def test_persist_account_keeps_all_accounts_under_concurrency(tmp_path):
    store = PaperStore(tmp_path)
    store.save_accounts({"base": Account.create("base", "base", 1000.0)})

    def persist(i: int) -> None:
        _persist_account(store, Account.create(f"p{i}", f"P{i}", 1000.0))

    threads = [threading.Thread(target=persist, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(store.load_accounts()) == {"base"} | {f"p{i}" for i in range(8)}