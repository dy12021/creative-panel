"""创意面板后端回归测试。设计依据 SPEC 第 11 节。"""
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))

import db  # noqa: E402  供下方 fixture 使用；必须晚于 sys.path.insert


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """临时 db + 已建表的新连接。

    不需要 pop/reimport：db.connect() 每次调用都经 db_path() 重新读 PANEL_DB。
    """
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "t.db"))
    db.init_db()
    return db.connect()


def test_pragmas(conn):
    """14. 建连后 PRAGMA 必须是安全值——recursive_triggers 是硬性前提。"""
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0


def test_schema_exists(conn):
    for t in ("ideas", "events"):
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()


def test_status_constants():
    db = importlib.import_module("db")
    assert db.STATUS_KEYS == ["idea", "clarifying", "clarified", "building", "done"]
    assert db.STATUS_LABEL["done"] == "完成成品"
    assert db.STATUS_INDEX["building"] == 3


@pytest.mark.parametrize("raw,want", [
    ("视频,科普", ",视频,科普,"),
    ("视频, 科普, ", ",视频,科普,"),      # 7. strip + 丢空
    ("科普,视频,科普", ",科普,视频,"),      # 去重
    ("", ","),
])
def test_normalize_tags(raw, want):
    db = importlib.import_module("db")
    assert db.normalize_tags(raw) == want


def test_parse_tags_none_is_empty():
    db = importlib.import_module("db")
    assert db.parse_tags(None) == db.ZERO_TAG


def test_parse_tags_all_blank_raises():
    """18. 提供了但全空 -> 报错；未提供才允许为空。"""
    db = importlib.import_module("db")
    with pytest.raises(ValueError):
        db.parse_tags("  ,  ,")


def test_now_iso_format():
    """毫秒精度、无时区后缀。"""
    import re
    db = importlib.import_module("db")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}", db.now_iso())


def test_check_constraints(conn):
    """15. 绕过 API 直接写非法值也被数据库拒绝。"""
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO ideas(title,status,created_at,updated_at)"
            " VALUES('x','nope','t','t')")
    conn.execute(
        "INSERT INTO ideas(title,status,created_at,updated_at)"
        " VALUES('ok','idea','t','t')")
    with pytest.raises(Exception):   # kind 非法
        conn.execute(
            "INSERT INTO events(idea_id,kind,actor,created_at)"
            " VALUES(1,'bogus','human','t')")
    with pytest.raises(Exception):   # actor 非法
        conn.execute(
            "INSERT INTO events(idea_id,kind,actor,created_at)"
            " VALUES(1,'note','robot','t')")