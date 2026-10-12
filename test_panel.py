"""创意面板后端回归测试。设计依据 SPEC 第 11 节。"""
import importlib
import os
import sqlite3
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


def test_now_iso_format(tmp_path, monkeypatch):
    """毫秒精度、无时区后缀。

    与 conn fixture 一致地隔离 PANEL_DB：这条测试不设 env 时会走 db_path()
    的缺省分支，在真实项目目录下建出 data/panel.db（I1）。
    """
    import re
    db = importlib.import_module("db")
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "t.db"))
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}", db.now_iso())
    # 防回归：取时间戳不该在磁盘上留下任何东西
    assert not (tmp_path / "t.db").exists()


# --- trg_ideas_touch 回归保护 -------------------------------------------------
# 任务书把 WHEN NEW.updated_at = OLD.updated_at 标成承重结论。下面两条把它钉死：
# 删掉整个触发器、或只删 WHEN 子句，至少会有一条变红。


def test_touch_trigger_refreshes_updated_at(conn):
    """不带 updated_at 的 UPDATE 必须刷新时间戳（触发器在干活）。"""
    import re
    conn.execute(
        "INSERT INTO ideas(title,status,created_at,updated_at)"
        " VALUES('a','idea','t','t')")
    conn.commit()
    conn.execute("UPDATE ideas SET title='b' WHERE id=1")
    conn.commit()
    got = conn.execute("SELECT updated_at FROM ideas WHERE id=1").fetchone()[0]
    assert got != "t"                              # 被触发器改写了
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}", got)


def test_touch_trigger_respects_explicit_updated_at(conn):
    """显式写 updated_at 时不被覆盖——这正是 WHEN 子句的承重语义。"""
    conn.execute(
        "INSERT INTO ideas(title,status,created_at,updated_at)"
        " VALUES('a','idea','t','t')")
    conn.commit()
    conn.execute("UPDATE ideas SET title='b', updated_at='KEEP' WHERE id=1")
    conn.commit()
    got = conn.execute("SELECT updated_at FROM ideas WHERE id=1").fetchone()[0]
    assert got == "KEEP"


def test_check_constraints(conn):
    """15. 绕过 API 直接写非法值也被数据库拒绝。

    断言收紧到 sqlite3.IntegrityError：写成 Exception 时，列名拼错或表不存在
    造成的 OperationalError 会因错误的原因而"通过"（I3）。
    """
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ideas(title,status,created_at,updated_at)"
            " VALUES('x','nope','t','t')")
    conn.execute(
        "INSERT INTO ideas(title,status,created_at,updated_at)"
        " VALUES('ok','idea','t','t')")
    with pytest.raises(sqlite3.IntegrityError):   # kind 非法
        conn.execute(
            "INSERT INTO events(idea_id,kind,actor,created_at)"
            " VALUES(1,'bogus','human','t')")
    with pytest.raises(sqlite3.IntegrityError):   # actor 非法
        conn.execute(
            "INSERT INTO events(idea_id,kind,actor,created_at)"
            " VALUES(1,'note','robot','t')")


def test_valid_rows_are_writable(conn):
    """正向：合法 ideas 与合法 events 写得进去。

    只测"非法值被拒"不能说明 CHECK 列表本身可用——合法值必须也放行（I3）。
    """
    conn.execute(
        "INSERT INTO ideas(title,status,created_at,updated_at)"
        " VALUES('ok','idea','t','t')")
    conn.execute(
        "INSERT INTO events(idea_id,kind,actor,created_at)"
        " VALUES(1,'created','human','t')")
    conn.commit()
    assert conn.execute("SELECT status FROM ideas WHERE id=1").fetchone()[0] == "idea"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE idea_id=1").fetchone()[0] == 1
    assert conn.execute(
        "SELECT kind, actor FROM events WHERE idea_id=1").fetchone()[0:2] == (
            "created", "human")