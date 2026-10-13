"""创意面板后端回归测试。设计依据 SPEC 第 11 节。"""
import ast
import importlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient  # noqa: E402  仅 HTTP 层那几节用得到

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))

import db  # noqa: E402  供下方 fixture 使用；必须晚于 sys.path.insert

import store  # noqa: E402  供下方白名单断言使用
from store import (NotFound, Invalid, advance, add_note, block, create_idea,
                   delete_idea, get_idea, list_ideas, stats, unblock, update_idea)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """临时 db + 已建表的新连接。

    不需要 pop/reimport：db.connect() 每次调用都经 db_path() 重新读 PANEL_DB。
    """
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "t.db"))
    db.init_db()
    return db.connect()


@pytest.fixture(autouse=True)
def _isolate_panel_db(tmp_path, monkeypatch):
    """默认把每条测试的 PANEL_DB 指向临时文件。

    这一层是补的，不是设计时就有的：新增的文档测试忘了设 env，于是直接打到
    真实的 data/panel.db，报出 `table ideas has no column named project_dir`
    ——真实库还没迁移，跟被测代码毫无关系。危害有两层：一条测试能污染用户
    数据，而它的报错又会指向错误的方向。

    所以隔离改成默认开启、不依赖每个测试自觉。真要测 db_path() 的缺省分支，
    自己 monkeypatch.delenv("PANEL_DB", raising=False) 即可。

    这里只 setenv、**不** init_db：有些测试断言"取时间戳不该在磁盘留下任何
    东西"，替它们建库反而会让那条断言失效。需要表的测试照旧自己 init_db。
    """
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "auto.db"))


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


# --- 数据访问层 store.py ----------------------------------------------------


def _new(conn, title="想法", **kw):
    return create_idea(title, **kw)


def test_create_and_full_chain(conn):
    """1. 5 态全链路，events 条数与顺序正确。"""
    a = create_idea("AI 发展史")
    assert a["status"] == "idea"
    assert a["tags"] == ","
    for to in ("clarifying", "clarified", "building", "done"):
        advance(a["id"], to, note="推进到 " + to)
    got = get_idea(a["id"])
    assert got["status"] == "done"
    assert got["done_at"] is not None                      # 8.
    kinds = [e["kind"] for e in got["events"]]
    assert kinds == ["created", "status", "status", "status", "status"]
    assert [e["to_status"] for e in got["events"][1:]] == \
        ["clarifying", "clarified", "building", "done"]


def test_advance_to_same_status_raises(conn):
    """4. 到当前状态报错，不写噪声 event。"""
    a = create_idea("x")
    with pytest.raises(Invalid):
        advance(a["id"], "idea")
    assert len(get_idea(a["id"])["events"]) == 1


def test_advance_jump_allowed_and_done_at_cleared(conn):
    """跳跃合法；离开 done 清 done_at。"""
    a = create_idea("x")
    advance(a["id"], "building")
    assert get_idea(a["id"])["done_at"] is None
    advance(a["id"], "done")
    assert get_idea(a["id"])["done_at"] is not None
    advance(a["id"], "clarifying")
    assert get_idea(a["id"])["done_at"] is None


def test_advance_invalid_status(conn):
    with pytest.raises(Invalid):
        advance(create_idea("x")["id"], "nope")


def test_block_repeat_and_unblock_keeps_note(conn):
    """5. 重复 block 覆盖 note 并各写一条；unblock 保留 blocked_note。"""
    a = create_idea("x")
    block(a["id"], "等版权确认")
    block(a["id"], "改成等授权")
    b = get_idea(a["id"])
    assert b["blocked"] == 1 and b["blocked_note"] == "改成等授权"
    assert [e["kind"] for e in b["events"]].count("blocked") == 2
    unblock(a["id"])
    u = get_idea(a["id"])
    assert u["blocked"] == 0 and u["blocked_note"] == "改成等授权"


def test_patch_single_edited_event(conn):
    """9. 多字段只写一条 edited。"""
    a = create_idea("x")
    update_idea(a["id"], {"title": "y", "source": "对话"})
    evs = get_idea(a["id"])["events"]
    edited = [e for e in evs if e["kind"] == "edited"]
    assert len(edited) == 1
    assert "title" in edited[0]["message"] and "source" in edited[0]["message"]


def test_patch_noop_still_writes_event(conn):
    a = create_idea("same")
    update_idea(a["id"], {"title": "same"})
    assert any(e["kind"] == "edited" for e in get_idea(a["id"])["events"])


def test_filters(conn):
    a = create_idea("AI 短片", tags="视频,科普", body="六段 H3")
    b = create_idea("别的", tags="音乐")
    advance(b["id"], "building")
    block(b["id"], "卡住")
    assert [x["id"] for x in list_ideas(status="idea")] == [a["id"]]
    assert [x["id"] for x in list_ideas(tag="科普")] == [a["id"]]
    assert [x["id"] for x in list_ideas(tag="科")] == []          # 10. 非子串
    assert [x["id"] for x in list_ideas(q="ai")] == [a["id"]]     # LIKE 自带不敏感
    assert [x["id"] for x in list_ideas(q="h3")] == [a["id"]]     # 搜得到 body
    assert [x["id"] for x in list_ideas(blocked=1)] == [b["id"]]
    assert [x["id"] for x in list_ideas(blocked=0)] == [a["id"]]
    with pytest.raises(Invalid):
        list_ideas(status="bogus")


def test_position_ordering_and_tiebreak(conn):
    """11/12. 新建 position 递增；同值按 id。

    **同值那一段改用裸 SQL 摆位，理由不是绕开重排，是同值状态在走 API 时
    已经造不出来了。** SPEC 4.2 要求"插入位置与相邻卡片的 position 差值小于
    1e-10 就整列重排"，而"与邻居完全同值"的差值就是 0，0 < 1e-10 —— 所以
    写进去的瞬间就被重排掉了。这是 SPEC 自己的两条要求在"精确重复"这个输入
    上的碰撞，不是实现跑偏。

    那 id tie-breaker 还要不要锁？要。它锁的是**读数**口径
    （`ORDER BY position, id`），而 position 相等的状态在库里依然可能存在：
    旧数据、别的客户端、或直接写库。裸 SQL 摆出来的同值正是这个场景，
    断言的仍然是 SPEC 11 第 11 条本身。
    """
    a = create_idea("a"); c = create_idea("b"); e = create_idea("c")
    assert [x["id"] for x in list_ideas(status="idea")] == [a["id"], c["id"], e["id"]]
    # position 必须真正参与排序：99 比 2、3 都大，a 应当落到末尾
    update_idea(a["id"], {"position": 99.0})
    assert [x["id"] for x in list_ideas(status="idea")] == [c["id"], e["id"], a["id"]]
    # 三条同值时由 id 兜底，顺序必须稳定（裸 SQL 摆位，见 docstring）
    for x in (a, c, e):
        conn.execute("UPDATE ideas SET position=0.0 WHERE id=?", (x["id"],))
    conn.commit()
    assert [x["id"] for x in list_ideas(status="idea")] == [a["id"], c["id"], e["id"]]


def test_writing_an_exact_duplicate_now_renumbers(conn):
    """写一个与邻居**完全同值**的 position：不留同值，而是按 SPEC 4.2 重排。

    这条是 `test_position_ordering_and_tiebreak` 改成裸 SQL 摆位之后，
    必须另有一条把"为什么走 API 造不出同值"钉住——否则下一个人看到那条测试
    改了就以为同值是能通过接口写出来的。差值 0 属于"小于 1e-10"，所以触发重排。

    同时验证**相对顺序不变**：重排前后都是 a、c、e。邻居比较用的是
    (position, id) 两段键，和列内读数口径一致，所以不会把谁甩到后面去。
    """
    a = create_idea("a")["id"]; c = create_idea("b")["id"]; e = create_idea("c")["id"]
    _force_position(conn, a, 0.0)
    _force_position(conn, e, 0.0)
    # c 还拿着自动分配的 2.0；a 与 e 同为 0.0，按 id 兜底 a 在 e 之前，
    # 两张 0.0 都排在 2.0 前面 —— 顺序是 [a, e, c]
    assert [(x["id"], x["position"]) for x in list_ideas(status="idea")] == [
        (a, 0.0), (e, 0.0), (c, 2.0)]

    update_idea(c, {"position": 0.0})       # 与邻居差值 0 -> 触发重排
    got = list_ideas(status="idea")
    # c 的 id 落在 a、e 之间，重排后仍该留在原处：顺序不变，只是位置被拉开
    assert [x["id"] for x in got] == [a, c, e], "重排不得打乱相对顺序"
    assert len({x["position"] for x in got}) == 3, "重排后不得再有同值"


def test_delete_cascades_events(conn):
    """7. 靠 ON DELETE CASCADE + foreign_keys=ON。"""
    a = create_idea("x")
    add_note(a["id"], "备注")
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == 2
    assert delete_idea(a["id"]) is True
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_stats(conn):
    a = create_idea("a"); create_idea("b")
    block(a["id"], "x")
    s = stats()
    assert s["counts"]["idea"] == 2 and s["blocked"] == 1


def test_not_found(conn):
    for fn in (lambda: get_idea(999), lambda: advance(999, "idea"),
               lambda: delete_idea(999)):
        with pytest.raises(NotFound):
            fn()


# --- 全局约束：状态只能走 advance、白名单、null、detail ------------------------
# 任务书把这几条列为硬约束，但给定的测试块一条都没覆盖——行为虽已实现，
# 却没有任何回归保护。补上，否则下一个人删掉任一条校验都不会变红。


# ideas 表的列 + create 额外带的位置。docs / docs_count 不在这里：它们不是列
# （docs 是每次现查的关联表，docs_count 只挂在列表响应上），放进这个集合会让
# "create 不带 events/docs" 这条契约失去意义。
IDEA_KEYS = {"id", "title", "body", "status", "blocked", "blocked_note",
             "tags", "source", "position", "created_at", "updated_at", "done_at",
             "project_dir", "agent_session"}


@pytest.mark.parametrize("bad", [
    {},                             # 空对象
    {"title": None},                # 任一字段为 null
    {"body": "x", "source": None},  # 混合里夹一个 null
    {"status": "building"},         # 状态只能走 advance，不在白名单
    {"id": 5},                      # 主键等白名单外的键
    {"tags": " , "},                # 规范化后全空 -> Invalid（不是 ValueError）
])
def test_update_idea_rejects(bad, conn):
    a = create_idea("x")
    with pytest.raises(Invalid):
        update_idea(a["id"], bad)
    got = get_idea(a["id"])
    assert got["status"] == "idea"          # 被拒时不得留下任何副作用
    assert len(got["events"]) == 1          # 也不许写 edited event


def test_status_is_absent_from_whitelist():
    """白名单必须不含 status——这是"状态只能走 advance"的物理保证。"""
    assert "status" not in store.FIELDS
    assert "done_at" not in store.FIELDS
    assert "blocked" not in store.FIELDS


def test_invalid_carries_human_detail():
    """Invalid 带人话 detail——Task 3 直接把它当 400 响应体。"""
    with pytest.raises(Invalid) as ei:
        update_idea(1, {})
    assert isinstance(ei.value.detail, str) and ei.value.detail.strip()


def test_create_rejects_blank_title_and_blank_tags(conn):
    with pytest.raises(Invalid):
        create_idea("   ")
    with pytest.raises(Invalid):
        create_idea("x", tags=" , ")


def test_not_found_on_every_writer(conn):
    """四个写入口的 NotFound 都要覆盖，不能只有 advance/delete。"""
    for fn in (lambda: update_idea(999, {"title": "x"}),
               lambda: add_note(999, "x"),
               lambda: block(999, "x"),
               lambda: unblock(999)):
        with pytest.raises(NotFound):
            fn()


def test_returned_shape_is_stable(conn):
    """Task 3 依赖这个形状：返回 dict 的键必须齐全且可预测。"""
    assert set(create_idea("x")) == IDEA_KEYS          # create 不带 events/docs
    a = create_idea("y")
    for d in (get_idea(a["id"]), update_idea(a["id"], {"title": "z"}),
              advance(a["id"], "clarifying"), add_note(a["id"], "n"),
              block(a["id"], "r"), unblock(a["id"])):
        assert set(d) == IDEA_KEYS | {"events", "docs"}
    # 列表那条多一个 docs_count（看板角标要用），且它**不是** ideas 表的列
    row = list_ideas()[0]
    assert set(row) == IDEA_KEYS | {"docs_count"}


def test_stats_shape_has_all_five_columns(conn):
    """看板要按 5 列渲染，counts 缺一个键 UI 就少一列。"""
    s = stats()
    assert s["counts"] == {k: 0 for k in db.STATUS_KEYS}
    assert s["blocked"] == 0


def test_new_card_in_empty_column_position_is_one(conn):
    """12. 空列中新建卡片 position 为 1——验 COALESCE，不撞 NOT NULL。"""
    assert create_idea("x")["position"] == 1
    assert create_idea("y")["position"] == 2
    b = get_idea(create_idea("z")["id"])
    assert advance(b["id"], "building")["position"] == 1     # building 列是空的
    assert advance(b["id"], "done")["position"] == 1         # done 列也是空的


def test_status_event_records_from_and_to(conn):
    """审计流的意义在 from→to；只断言 to_status 会漏掉 from_status 写丢。"""
    a = create_idea("x")
    advance(a["id"], "building")
    advance(a["id"], "done")
    evs = get_idea(a["id"])["events"]
    moves = [(e["from_status"], e["to_status"]) for e in evs if e["kind"] == "status"]
    assert moves == [("idea", "building"), ("building", "done")]
    assert all(e["from_status"] is None and e["to_status"] is None
               for e in evs if e["kind"] == "created")


def test_list_ideas_orders_by_board_column_then_position(conn):
    """跨列按看板列序 idea→…→done（不是字典序）；列内按 position, id。"""
    a = create_idea("a")
    b = create_idea("b")
    c = create_idea("c")
    advance(a["id"], "building")
    advance(b["id"], "building")
    update_idea(a["id"], {"position": 5.0})          # 列内把 a 挪到 b 后面
    got = list_ideas()
    assert [x["status"] for x in got] == ["idea", "building", "building"]
    assert [x["id"] for x in got] == [c["id"], b["id"], a["id"]]


def test_all_five_columns_come_back_in_board_order(conn):
    """字典序会把 building 排到 idea 前面——这条把看板列序钉死。

    done 最后建、idea 最先建，纯粹为了让"按 id 排"和"按列序排"给出不同答案。
    """
    first = create_idea("仅创意")["id"]
    rest = {}
    for to in ("clarifying", "clarified", "building", "done"):
        cid = create_idea(to)["id"]
        advance(cid, to)
        rest[to] = cid
    assert [x["id"] for x in list_ideas()] == [first] + [
        rest[t] for t in ("clarifying", "clarified", "building", "done")]


def test_board_order_follows_status_keys(conn, monkeypatch):
    """列序必须跟着 db.STATUS_KEYS 走，不能硬编码。

    否则改了看板列序，这里会静默按旧顺序返回——正好是字典序那条坑的复发形态。
    """
    a = create_idea("a")
    b = create_idea("b")
    advance(a["id"], "building")
    monkeypatch.setattr(db, "STATUS_KEYS", ["building", "idea"])
    assert [x["id"] for x in list_ideas()] == [a["id"], b["id"]]


# --- 修复轮 1：null 与非字符串入参不得漏出 sqlite3 异常 ----------------------
# NOT NULL 列漏下去是 IntegrityError -> Task 3 返 500 而不是 422。


@pytest.mark.parametrize("bad", [5, ["视频"], {"视频": 1}, True, b"v"])
def test_non_string_tags_raise_invalid(bad, conn):
    with pytest.raises(Invalid):
        create_idea("x", tags=bad)
    a = create_idea("y")
    with pytest.raises(Invalid):
        update_idea(a["id"], {"tags": bad})


def test_blank_tags_message_still_human(conn):
    """ValueError 那条不能被 I2 的新 except 吃掉——"标签不能为空"是 SPEC 文案。"""
    with pytest.raises(Invalid) as ei:
        create_idea("x", tags=" , ")
    assert "标签不能为空" in ei.value.detail


@pytest.mark.parametrize("call", [
    lambda i: create_idea("x", body=None),
    lambda i: create_idea("x", source=None),
    lambda i: create_idea("x", actor=None),
    lambda i: add_note(i, None),
    lambda i: add_note(i, "n", actor=None),
    lambda i: block(i, None),
    lambda i: block(i, "r", actor=None),
    lambda i: unblock(i, actor=None),
    lambda i: update_idea(i, {"title": "ok"}, actor=None),
    lambda i: advance(i, "clarifying", actor=None),
])
def test_null_params_raise_invalid(call, conn):
    a = create_idea("seed")["id"]
    with pytest.raises(Invalid):
        call(a)


def test_rejected_null_leaves_no_partial_state(conn):
    """block(reason=None) 必须在 UPDATE 之前就拒——不能先把 blocked 改了再报错。"""
    a = create_idea("x")
    with pytest.raises(Invalid):
        block(a["id"], None)
    got = get_idea(a["id"])
    assert got["blocked"] == 0 and got["blocked_note"] == ""
    assert [e["kind"] for e in got["events"]] == ["created"]
    with pytest.raises(Invalid):
        add_note(a["id"], None)
    assert len(get_idea(a["id"])["events"]) == 1


def test_empty_string_still_allowed(conn):
    """null 被拒不代表空串被拒——block(reason="") 必须照常成功。"""
    a = create_idea("x")
    assert block(a["id"], "")["blocked_note"] == ""
    assert add_note(a["id"], "")["events"][-1]["message"] == ""
    assert create_idea("y", body="", source="")["body"] == ""


# --- 修复轮 2：非字符串入参不得漏出 sqlite3 异常 ------------------------------
# 两种坏法：list/dict 漏 sqlite3.ProgrammingError；标量被 TEXT 亲和性静默强转
# （title=True 会落库成 "1"）。前者是 500，后者是无声的数据损坏，更坏。


@pytest.mark.parametrize("bad", [5, ["视频"], True])   # 标量静默强转 / list 绑定失败
@pytest.mark.parametrize("call", [
    lambda i, b: create_idea(b),
    lambda i, b: create_idea("x", body=b),
    lambda i, b: create_idea("x", source=b),
    lambda i, b: add_note(i, b),
    lambda i, b: block(i, b),
    lambda i, b: advance(i, "clarifying", note=b),
])
def test_non_string_text_params_raise_invalid(call, bad, conn):
    """title/body/source/message/reason/note 都是 JSON 可达的，必须 422 不是 500。"""
    a = create_idea("seed")["id"]
    with pytest.raises(Invalid):
        call(a, bad)


@pytest.mark.parametrize("bad", [5, ["x"]])
@pytest.mark.parametrize("field", ["title", "body", "source", "blocked_note"])
def test_update_idea_non_string_text_raises_invalid(field, bad, conn):
    a = create_idea("seed")
    with pytest.raises(Invalid):
        update_idea(a["id"], {field: bad})
    got = get_idea(a["id"])
    assert got["title"] == "seed"                   # 没落库
    assert [e["kind"] for e in got["events"]] == ["created"]


def test_position_requires_a_number(conn):
    """position 是 REAL 列：非数字必须拒，不能静默存 TEXT 破坏列内排序。

    SPEC 4.2 说的"只存不校验"指不校验**取值范围**，类型仍要卡。
    """
    a = create_idea("x")
    assert update_idea(a["id"], {"position": 3.5})["position"] == 3.5
    assert update_idea(a["id"], {"position": 7})["position"] == 7.0
    assert create_idea("y", position=2.5)["position"] == 2.5
    for bad in ("abc", "7", [1], True):
        with pytest.raises(Invalid):
            update_idea(a["id"], {"position": bad})
        with pytest.raises(Invalid):
            create_idea("z", position=bad)
        with pytest.raises(Invalid):            # 每次用新卡，避免撞"已经处于此状态"
            advance(create_idea("w")["id"], "building", position=bad)


def test_position_none_means_auto_for_create_and_advance(conn):
    """position=None 在建卡/推进时是"自动分配"的合法缺省，与 tags=None 同类。

    但在 PATCH 里 null 是"清空字段"的歧义输入，必须拒（test_null_position_*）。
    同一个 None 在两处含义不同，这条把它们钉死。
    """
    a = create_idea("x")                      # 不传 position
    assert a["position"] == 1
    assert create_idea("y", position=None)["position"] == 2     # 空列自动分配
    assert advance(a["id"], "clarifying")["position"] == 1     # clarifying 列也是空的
    with pytest.raises(Invalid):
        update_idea(a["id"], {"position": None})


def test_position_column_never_holds_text(conn):
    """TEXT 排在 REAL 之后，混进一列就足以让整列排序失真——库里不许出现。"""
    for bad in ("abc", [1], True):
        with pytest.raises(Invalid):
            create_idea("t", position=bad)
        with pytest.raises(Invalid):
            advance(create_idea("u")["id"], "building", position=bad)
    c = db.connect()
    try:
        kinds = [r[0] for r in c.execute("SELECT DISTINCT typeof(position) FROM ideas")]
    finally:
        c.close()
    assert kinds == ["real"]


def test_position_text_would_break_column_order(conn):
    """钉住"为什么非数字要拒"：一张 TEXT 卡片会被顶到该列所有数字卡片之后。"""
    for pos in (1.0, 2.0, 3.0):
        create_idea("p%s" % pos, position=pos)
    with pytest.raises(Invalid):
        create_idea("坏卡", position="abc")
    assert [x["title"] for x in list_ideas(status="idea")] == ["p1.0", "p2.0", "p3.0"]


def test_null_and_type_errors_are_not_one_message(conn):
    """null 与类型错误是两回事，Task 3 把 detail 原样返给客户端，不能混。"""
    a = create_idea("seed")["id"]
    with pytest.raises(Invalid) as n:
        block(a, None)
    with pytest.raises(Invalid) as t:
        block(a, 5)
    assert "请传空串" in n.value.detail
    assert "必须是字符串" in t.value.detail
    assert n.value.detail != t.value.detail


def test_null_message_matches_spec_wording(conn):
    """SPEC 5.4 错误表：422「字段不能为 null，请传空串」。旧文案已全部废止。"""
    a = create_idea("seed")["id"]
    for fn in (lambda: create_idea("x", body=None),
               lambda: add_note(a, None),
               lambda: block(a, None),
               lambda: advance(a, "clarifying", actor=None),
               lambda: update_idea(a, {"source": None})):
        with pytest.raises(Invalid) as ei:
            fn()
        assert "不能为 null，请传空串" in ei.value.detail
        assert "清空请传空字符串" not in ei.value.detail


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_title_null_and_blank_keep_spec_wording(bad, conn):
    """null 视同"没给标题"——必须仍走错误表那条，不能被类型校验抢走。"""
    with pytest.raises(Invalid) as ei:
        create_idea(bad)
    assert ei.value.detail == "标题不能为空"


def test_advance_note_none_is_still_default(conn):
    """note=None 是合法缺省（不写备注），不能被 null 校验误伤。"""
    a = create_idea("x")
    assert advance(a["id"], "clarifying")["events"][-1]["message"] == ""


def test_advance_note_error_names_the_field(conn):
    """advance 单独挡 note，目的就是报错指到"备注"而不是 add_event 的"事件内容"。

    不钉这条的话 advance 里那行 _text 就是死代码——add_event 的守卫照样会拦。
    """
    a = create_idea("seed")["id"]
    with pytest.raises(Invalid) as ei:
        advance(a, "clarifying", note=["x"])
    assert ei.value.detail == "备注 必须是字符串"


def test_whitelist_check_precedes_value_check(conn):
    """白名单外的键先报"不支持的字段"，哪怕它的值类型也不对（SPEC 错误表 443）。"""
    a = create_idea("seed")["id"]
    with pytest.raises(Invalid) as ei:
        update_idea(a, {"status": 5})
    assert "不支持的字段" in ei.value.detail


# --- 修复轮 3：null 守卫必须覆盖全字段，且独立于类型分档 ---------------------
# 上一轮我把全字段 null 守卫换成了"跳过 position/tags 的类型循环"，等于开了
# 两个洞：tags 静默清空（还写下"修改了 tags"的 event，账本说谎），position
# 直接漏 IntegrityError。这两条把当时的 22/22 假象打穿了。


@pytest.mark.parametrize("field", list(store.FIELDS))
def test_null_rejected_for_every_whitelisted_field(field, conn):
    """白名单里每一个字段传 null 都必须 Invalid——没有例外。"""
    a = create_idea("seed")["id"]
    with pytest.raises(Invalid) as ei:
        update_idea(a, {field: None})
    assert "不能为 null，请传空串" in ei.value.detail


def test_null_tags_does_not_silently_clear(conn):
    """回归锁：{"tags": null} 曾静默把标签清成 ","，还写下"修改了 tags"。"""
    a = create_idea("种子", tags="视频,科普")
    with pytest.raises(Invalid):
        update_idea(a["id"], {"tags": None})
    got = get_idea(a["id"])
    assert got["tags"] == ",视频,科普,"                      # 库里的标签没被动过
    assert [e["kind"] for e in got["events"]] == ["created"]   # 更不能写"修改了 tags"


def test_null_position_does_not_leak_integrity_error(conn):
    """回归锁：{"position": null} 曾漏出 NOT NULL constraint failed。"""
    a = create_idea("种子")
    before = get_idea(a["id"])["position"]
    with pytest.raises(Invalid):
        update_idea(a["id"], {"position": None})
    got = get_idea(a["id"])
    assert got["position"] == before
    assert [e["kind"] for e in got["events"]] == ["created"]


def test_null_guard_is_not_a_type_check(conn):
    """null 守卫独立于类型分档：tags/position 的类型错与 null 错是两条不同文案。"""
    a = create_idea("种子")["id"]
    with pytest.raises(Invalid) as nul:
        update_idea(a, {"tags": None})
    with pytest.raises(Invalid) as typ:
        update_idea(a, {"tags": 5})
    assert "请传空串" in nul.value.detail
    assert "请传空串" not in typ.value.detail


def test_list_ideas_rejects_non_string_filters(conn):
    """tag/q 传非字符串：tag 曾经拼成 "%,5,%" 静默返空，q 曾在 split() 抛异常。"""
    for bad in (5, ["科普"], True):
        with pytest.raises(Invalid):
            list_ideas(tag=bad)
        with pytest.raises(Invalid):
            list_ideas(q=bad)
    assert list_ideas(tag=None) == list_ideas()      # None = 不过滤，仍然合法


# --- Task 3：HTTP 层（路由、校验、422/404 文案）-----------------------------
# 这一节全部经 fastapi.testclient 走真实 HTTP 栈（路由、请求体解析、异常
# 处理器），而不是直接调 store——store 层已经 109 项覆盖，这里只测"从线上看
# 到的行为对不对"：状态码、detail 文案、以及有没有漏成 500。


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时 db + 每次全新的 app 实例。

    不 pop/reimport：db.connect() 每次调用都经 db_path() 重读 PANEL_DB，
    重导入反而会把模块级 `from store import ...` 绑到旧对象上去。
    环境变量必须先于 import 设置——app.py 模块级有 `app = create_app()`，
    它会 init_db()，顺序反了就会在真实 data/panel.db 上建表。
    """
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "c.db"))
    appm = importlib.import_module("app")
    return TestClient(appm.create_app())


def _body_endpoints(i):
    """SPEC 第 5 节里所有收 JSON body 的端点，i 为创意 id。"""
    return [("POST", "/api/ideas"), ("PATCH", "/api/ideas/%d" % i),
            ("POST", "/api/ideas/%d/advance" % i), ("POST", "/api/ideas/%d/note" % i),
            ("POST", "/api/ideas/%d/block" % i)]


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.2.0"}       # VERSION 是契约的一部分


@pytest.mark.parametrize("payload", [["title"], "title", 5, True])
def test_json_body_must_be_object(payload, client):
    """body 必须是对象：数组会让 store.update_idea 拿 list 调 .items() 炸 500。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()
    for method, url in _body_endpoints(i["id"]):
        r = client.request(method, url, json=payload)
        assert r.status_code == 422, (url, payload, r.status_code, r.text)


@pytest.mark.parametrize("raw", [b"null", b"[1,2]", b'"s"', b"5", b"{oops", b""])
def test_raw_body_that_is_not_an_object_is_422(raw, client):
    """字面量 null / 坏 JSON / 空 body 同样得 422，不能漏成 500。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()
    for method, url in _body_endpoints(i["id"]):
        r = client.request(method, url, content=raw,
                           headers={"content-type": "application/json"})
        assert r.status_code == 422, (url, raw, r.status_code, r.text)


def test_absent_body_is_422(client):
    i = client.post("/api/ideas", json={"title": "a"}).json()
    for method, url in _body_endpoints(i["id"]):
        assert client.request(method, url).status_code == 422, url


def test_patch_status_uses_the_wording_spec_names(client):
    """SPEC 5.1 点名的字面量，不是 store 的通用"不支持的字段"。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.patch("/api/ideas/%d" % i, json={"status": "done"})
    assert r.status_code == 422
    assert r.json()["detail"] == "请使用 /advance 接口变更状态"


@pytest.mark.parametrize("key", ["blocked", "id", "created_at", "status2"])
def test_patch_unknown_field_names_the_field(key, client):
    """白名单外的键要指出是哪个，并提示状态走 advance（SPEC 第 9 节）。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.patch("/api/ideas/%d" % i, json={key: "x"})
    assert r.status_code == 422
    assert key in r.json()["detail"] and "/advance" in r.json()["detail"]


def test_patch_empty_object_422(client):
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.patch("/api/ideas/%d" % i, json={})
    assert r.status_code == 422 and r.json()["detail"] == "没有可更新的字段"


def test_patch_whitelisted_fields_still_work(client):
    """正向对照：别把 PATCH 整个写死成 422。一次 PATCH 只写一条 edited。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.patch("/api/ideas/%d" % i,
                     json={"title": "新标题", "body": "描述", "tags": "视频, 科普",
                           "source": "刷到的", "blocked_note": "", "position": 2.5},
                     headers={"X-Panel-Actor": "agent"})
    assert r.status_code == 200
    got = r.json()
    assert got["title"] == "新标题" and got["body"] == "描述"
    assert got["tags"] == ",视频,科普," and got["source"] == "刷到的"
    assert got["position"] == 2.5
    assert [e["kind"] for e in got["events"]] == ["created", "edited"]
    assert got["events"][-1]["actor"] == "agent"


@pytest.mark.parametrize("payload,detail", [
    ({"title": None}, "标题 不能为 null，请传空串"),
    ({"body": None}, "描述 不能为 null，请传空串"),
    ({"tags": None}, "标签 不能为 null，请传空串"),
    ({"position": None}, "排序位置 不能为 null，请传空串"),
    ({"source": None}, "来源 不能为 null，请传空串"),
    ({"blocked_note": None}, "阻塞备注 不能为 null，请传空串"),
    ({"title": ["a"]}, "标题 必须是字符串"),
    ({"body": 5}, "描述 必须是字符串"),
    ({"source": True}, "来源 必须是字符串"),
    ({"tags": ["a"]}, "标签必须是逗号分隔的字符串"),
    ({"position": "1"}, "排序位置 必须是数字"),
    ({"position": True}, "排序位置 必须是数字"),
])
def test_patch_null_and_type_use_chinese_field_names(payload, detail, client):
    """文案带中文字段名，且被拒的 PATCH 不能落库、不能写 edited。"""
    i = client.post("/api/ideas", json={"title": "原始", "tags": "视频"}).json()["id"]
    r = client.patch("/api/ideas/%d" % i, json=payload)
    assert r.status_code == 422 and r.json()["detail"] == detail
    after = client.get("/api/ideas/%d" % i).json()
    assert after["title"] == "原始" and after["tags"] == ",视频,"
    assert [e["kind"] for e in after["events"]] == ["created"]


def test_advance_same_status_422(client):
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.post("/api/ideas/%d/advance" % i, json={"to": "idea"})
    assert r.status_code == 422 and r.json()["detail"] == "该创意已经处于此状态"


def test_advance_with_position_single_call(client):
    """跨列拖拽一次调用同时改状态和位置（SPEC 5.2）。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.post("/api/ideas/%d/advance" % i,
                    json={"to": "building", "position": 3.5, "note": "开工"},
                    headers={"X-Panel-Actor": "agent"})
    assert r.status_code == 200
    got = r.json()
    assert got["status"] == "building" and got["position"] == 3.5
    ev = got["events"][-1]
    assert ev["kind"] == "status" and ev["from_status"] == "idea"
    assert ev["to_status"] == "building" and ev["message"] == "开工"
    assert ev["actor"] == "agent"
    assert len([e for e in got["events"] if e["kind"] == "status"]) == 1


def test_advance_unknown_status_lists_legal_names(client):
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.post("/api/ideas/%d/advance" % i, json={"to": "foobar"})
    assert r.status_code == 422
    for k in db.STATUS_KEYS:
        assert k in r.json()["detail"]


def test_advance_without_to_is_422(client):
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    assert client.post("/api/ideas/%d/advance" % i, json={}).status_code == 422


def test_advance_writes_done_at_and_clears_it_on_leaving(client):
    """SPEC 5.1：进 done 写 done_at，离开清空。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    assert client.post("/api/ideas/%d/advance" % i,
                       json={"to": "done"}).json()["done_at"]
    assert client.post("/api/ideas/%d/advance" % i,
                       json={"to": "building"}).json()["done_at"] is None


def test_actor_header_rules(client):
    """SPEC 5.4：非法值忽略记 human，不报错；三个合法值都照记。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    url = "/api/ideas/%d/note" % i
    assert client.post(url, json={"message": "默认"}).status_code == 200
    assert client.post(url, json={"message": "人写的"},
                       headers={"X-Panel-Actor": "robot"}).status_code == 200
    client.post(url, json={"message": "agent 写的"}, headers={"X-Panel-Actor": "agent"})
    client.post(url, json={"message": "系统写的"}, headers={"X-Panel-Actor": "system"})
    evs = [e for e in client.get("/api/ideas/%d" % i).json()["events"] if e["kind"] == "note"]
    assert [e["actor"] for e in evs] == ["human", "human", "agent", "system"]
    assert client.get("/api/ideas/%d" % i).json()["events"][0]["actor"] == "human"


@pytest.mark.parametrize("payload,detail", [
    ({"title": None}, "标题不能为空"),
    ({"title": "   "}, "标题不能为空"),
    ({"title": 5}, "标题 必须是字符串"),
    ({"title": "a", "body": None}, "描述 不能为 null，请传空串"),
    ({"title": "a", "body": 0}, "描述 必须是字符串"),
    ({"title": "a", "body": ["x"]}, "描述 必须是字符串"),
    ({"title": "a", "source": None}, "来源 不能为 null，请传空串"),
    ({"title": "a", "tags": ["x"]}, "标签必须是逗号分隔的字符串"),
    ({"title": "a", "tags": "  , "}, "标签不能为空"),
])
def test_create_rejects_bad_fields_with_spec_wording(payload, detail, client):
    """app 层不能把 null/非字符串洗成空串——那是不报错的数据损坏。"""
    r = client.post("/api/ideas", json=payload)
    assert r.status_code == 422 and r.json()["detail"] == detail
    assert client.get("/api/ideas").json() == []          # 没落库


def test_create_defaults_and_tag_rules(client):
    """SPEC 第 9 节：未提供 tags -> ","；提供但全空 -> 422。"""
    ok = client.post("/api/ideas", json={"title": "a"})
    assert ok.status_code == 201
    idea = ok.json()
    assert idea["tags"] == "," and idea["status"] == "idea"
    assert idea["blocked"] == 0 and idea["blocked_note"] == ""
    assert idea["done_at"] is None and idea["position"] == 1   # 空列用 COALESCE
    assert client.post("/api/ideas", json={"title": "b", "tags": "视频, 科普"}).json()["tags"] \
        == ",视频,科普,"
    assert client.post("/api/ideas", json={"title": "c", "tags": None}).json()["tags"] == ","


def test_create_ignores_client_supplied_position(client):
    """SPEC 4.2：新建卡片的 position 恒为 COALESCE(MAX,0)+1，不由客户端指定。"""
    a = client.post("/api/ideas", json={"title": "a"}).json()
    b = client.post("/api/ideas", json={"title": "b", "position": 99}).json()
    assert a["position"] == 1 and b["position"] == 2


@pytest.mark.parametrize("raw", ["5", "2", "true", "-1", "abc", "0.5"])
def test_blocked_query_param_rejects_anything_but_0_or_1(raw, client):
    """回归锁：?blocked=5 曾被当成 0，静默返回一整批错的卡片还不报错。"""
    client.post("/api/ideas", json={"title": "a"})
    r = client.get("/api/ideas?blocked=%s" % raw)
    assert r.status_code == 422, (raw, r.status_code, r.text)
    assert "阻塞筛选" in r.json()["detail"]        # 中文字段名，不打原始键


def test_blocked_filter_0_and_1_pick_the_right_cards(client):
    a = client.post("/api/ideas", json={"title": "卡住的"}).json()["id"]
    b = client.post("/api/ideas", json={"title": "正常的"}).json()["id"]
    client.post("/api/ideas/%d/block" % a, json={"reason": "等素材"})
    assert [x["id"] for x in client.get("/api/ideas?blocked=1").json()] == [a]
    assert [x["id"] for x in client.get("/api/ideas?blocked=0").json()] == [b]
    assert len(client.get("/api/ideas").json()) == 2         # 省略 = 全部


def test_status_filter_rejects_unknown_value(client):
    r = client.get("/api/ideas?status=foobar")
    assert r.status_code == 422 and r.json()["detail"] == "未知状态：foobar"


def test_tag_filter_is_whole_label_not_substring(client):
    """中文查询参数要能穿过 URL 编码；tag 按整标签匹配不是子串。"""
    a = client.post("/api/ideas", json={"title": "a", "tags": "视频, 科普"}).json()["id"]
    client.post("/api/ideas", json={"title": "b"})
    assert [x["id"] for x in client.get("/api/ideas?tag=科普").json()] == [a]
    assert client.get("/api/ideas?tag=科").json() == []


def test_q_words_are_and_across_title_and_body(client):
    a = client.post("/api/ideas", json={"title": "AI 短片", "body": "先做大纲"}).json()["id"]
    client.post("/api/ideas", json={"title": "AI 短片", "body": "别的"})
    assert [x["id"] for x in client.get("/api/ideas?q=AI 短片 先做").json()] == [a]
    assert client.get("/api/ideas?q=AI 短片 缺词").json() == []


def test_query_params_are_text_so_numeric_q_is_a_real_search(client):
    """?q=5 是搜字面量 "5"，不是类型错误——加"纯数字即 422"会砸掉搜年份。"""
    client.post("/api/ideas", json={"title": "第 5 集脚本"})
    client.post("/api/ideas", json={"title": "别的创意"})
    assert [x["title"] for x in client.get("/api/ideas?q=5").json()] == ["第 5 集脚本"]


def test_list_follows_board_column_order(client):
    """app 层不重排，排序归 store——这里锁住看板列序而不是字典序。"""
    ids = {k: client.post("/api/ideas", json={"title": k}).json()["id"]
           for k in db.STATUS_KEYS}
    for k in reversed(db.STATUS_KEYS):          # 故意乱序推进
        client.post("/api/ideas/%d/advance" % ids[k], json={"to": k})
    assert [x["title"] for x in client.get("/api/ideas").json()] == db.STATUS_KEYS


@pytest.mark.parametrize("call", [
    lambda c: c.get("/api/ideas/999"),
    lambda c: c.patch("/api/ideas/999", json={"title": "x"}),
    lambda c: c.post("/api/ideas/999/advance", json={"to": "building"}),
    lambda c: c.post("/api/ideas/999/note", json={"message": "x"}),
    lambda c: c.post("/api/ideas/999/block", json={"reason": "x"}),
    lambda c: c.post("/api/ideas/999/unblock"),
    lambda c: c.delete("/api/ideas/999"),
])
def test_missing_idea_is_404_with_human_detail(call, client):
    r = call(client)
    assert r.status_code == 404
    assert r.json()["detail"] == "没有 #999 这条创意"


def test_delete_removes_card_and_its_events(client):
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    client.post("/api/ideas/%d/note" % i, json={"message": "x"})
    r = client.delete("/api/ideas/%d" % i)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/ideas/%d" % i).status_code == 404
    assert client.get("/api/ideas").json() == []


def test_block_unblock_over_http(client):
    """SPEC 5.6：block 重复调用覆盖 note 且每次留痕；unblock 不清空 blocked_note。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    hdr = {"X-Panel-Actor": "agent"}
    first = client.post("/api/ideas/%d/block" % i, json={"reason": "等素材"}, headers=hdr)
    assert first.json()["blocked"] == 1 and first.json()["blocked_note"] == "等素材"
    client.post("/api/ideas/%d/block" % i, json={"reason": "等版权"}, headers=hdr)
    unblocked = client.post("/api/ideas/%d/unblock" % i, headers=hdr).json()
    assert unblocked["blocked"] == 0 and unblocked["blocked_note"] == "等版权"
    got = client.get("/api/ideas/%d" % i).json()
    assert [e["kind"] for e in got["events"]] == ["created", "blocked", "blocked", "unblocked"]
    assert all(e["actor"] == "agent" for e in got["events"][1:])


def test_block_reason_none_is_422(client):
    """null 阻塞原因不能被洗成空串，否则等于无声清掉原因。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    r = client.post("/api/ideas/%d/block" % i, json={"reason": None})
    assert r.status_code == 422 and "不能为 null" in r.json()["detail"]
    assert client.get("/api/ideas/%d" % i).json()["blocked"] == 0


def test_stats_endpoint(client):
    a = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    client.post("/api/ideas", json={"title": "b"})
    client.post("/api/ideas/%d/advance" % a, json={"to": "clarifying"})
    client.post("/api/ideas/%d/block" % a, json={"reason": "卡"})
    assert client.get("/api/stats").json() == {
        "counts": {"idea": 1, "clarifying": 1, "clarified": 0,
                   "building": 0, "done": 0},
        "blocked": 1}


def test_create_app_survives_missing_web_dir(tmp_path, monkeypatch):
    """web/ 缺失时 create_app() 不能炸——StaticFiles 会对不存在的目录当场抛错。

    原断言靠"仓库里还没有 web/"来满足前提，Task 5 把 web/ 建出来之后它就永远
    不成立了（`GET /` 会变成 200）。改用 monkeypatch 指向一个不存在的目录，
    锁的仍然是 app.py 里那行 exists() 守卫本身，覆盖面没变窄。
    """
    appm = importlib.import_module("app")
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "m.db"))
    monkeypatch.setattr(appm, "WEB_DIR", tmp_path / "nope")
    c = TestClient(appm.create_app())
    assert c.get("/api/health").status_code == 200   # 服务照常起来
    assert c.get("/").status_code == 404              # 只是没有页面可发


def test_web_dir_mount_does_not_shadow_api(tmp_path, monkeypatch):
    """挂载必须在最后：/api/* 仍走路由，/ 才落到静态目录。"""
    appm = importlib.import_module("app")
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "c.db"))
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>面板</h1>", encoding="utf-8")
    monkeypatch.setattr(appm, "WEB_DIR", web)
    c = TestClient(appm.create_app())
    assert c.get("/api/health").json() == {"ok": True, "version": "0.2.0"}
    assert "面板" in c.get("/").text
    assert c.get("/api/stats").status_code == 200


def test_static_responses_are_no_store(tmp_path, monkeypatch):
    """静态资源必须带 no-store。

    2026-10-13 实测踩过：改完亮色主题和横向轨道，磁盘与 HTTP 都已是新文件，
    用户浏览器里却还是旧的暗色界面——它缓存着重做之前的 style.css / app.js。
    而 app.js 是旧版也照样能跑、/api 数据照旧是新的，于是症状变成"壳是旧的、
    数据是新的"，看起来像代码根本没改成功，非常难自查。

    这里锁的是响应头本身。断言写成"含 no-store"而不是全等，是为了允许以后
    往里加 max-age 之类的指令；但去掉 no-store 会让这条测试变红。
    """
    appm = importlib.import_module("app")
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "s.db"))
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>面板</h1>", encoding="utf-8")
    (web / "style.css").write_text(":root{--bg:#EDF0F4}", encoding="utf-8")
    monkeypatch.setattr(appm, "WEB_DIR", web)
    c = TestClient(appm.create_app())
    for path in ("/", "/style.css"):
        cc = c.get(path).headers.get("cache-control", "")
        assert "no-store" in cc, "%s 少了 no-store：%r" % (path, cc)


# --- 修复轮 1：detail 契约统一 + 筛选参数空串归一 ---------------------------
# 上一轮两种 detail 形态混在同一个 {"detail": ...} 字段下：store 的 Invalid 是
# 字符串，pydantic 的框架层错误是数组。CLI 直接 r.json()["detail"] 打印、
# 前端 throw new Error(d.detail)，两边都假定它是字符串——数组透出去，CLI 打出一
# 串 python 字典、前端拿到 "[object Object]"，SPEC 6 要求的"翻译成
# 「#12 已经在『正在明确』了」那种匹配根本写不出来。


@pytest.mark.parametrize("method,url,payload,fragment", [
    ("GET", "/api/ideas/abc", None, "创意 id"),        # 路径参数非整数
    ("POST", "/api/ideas", ["title"], "JSON 对象"),
    ("PATCH", "/api/ideas/1", ["title"], "JSON 对象"),
    ("POST", "/api/ideas/1/advance", ["to"], "JSON 对象"),
    ("POST", "/api/ideas/1/note", "x", "JSON 对象"),
    ("POST", "/api/ideas/1/block", 5, "JSON 对象"),
    ("POST", "/api/ideas/1/block", True, "JSON 对象"),
])
def test_framework_errors_return_a_human_detail_string(method, url, payload, fragment,
                                                       client):
    """SPEC 第 9 节：框架层错误也得是人话字符串，不是 pydantic 数组。

    光断言 422 是恒真的——两种形态都是 422。必须同时断言类型和文案，
    并且明确断言 pydantic 原文没有漏出来。
    """
    r = client.request(method, url, json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str), (url, payload, type(detail), detail)
    assert fragment in detail
    assert "Input should be" not in detail         # 别把 pydantic 原文甩给人看


def test_path_id_error_names_the_parameter(client):
    r = client.get("/api/ideas/abc")
    assert r.status_code == 422
    assert r.json()["detail"] == "创意 id 必须是整数"


def test_missing_body_detail_is_a_human_string(client):
    r = client.post("/api/ideas")
    assert r.status_code == 422
    assert r.json()["detail"] == "缺少请求体"


def test_bad_json_detail_is_a_human_string(client):
    r = client.request("POST", "/api/ideas", content=b"{oops",
                       headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"] == "请求体不是合法的 JSON"


def test_literal_null_body_detail_is_a_human_string(client):
    r = client.request("POST", "/api/ideas", content=b"null",
                       headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"] == "缺少请求体"


def test_every_endpoint_error_detail_is_a_string(client):
    """契约扫描：detail 全局只有一种形态。逐条走一遍 4xx 路径。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()["id"]
    responses = [client.get("/api/health"),
                 client.get("/api/ideas"),
                 client.get("/api/ideas/%d" % i),
                 client.get("/api/ideas/999"),
                 client.get("/api/ideas/abc"),
                 client.get("/api/nope"),
                 client.post("/api/ideas", json={"title": "   "}),
                 client.post("/api/ideas", json=["x"]),
                 client.post("/api/ideas", json={"title": "b", "tags": " , "}),
                 client.patch("/api/ideas/%d" % i, json={}),
                 client.patch("/api/ideas/%d" % i, json={"status": "done"}),
                 client.patch("/api/ideas/%d" % i, json={"title": None}),
                 client.post("/api/ideas/%d/advance" % i, json={"to": "idea"}),
                 client.post("/api/ideas/%d/advance" % i, json={"to": "foobar"}),
                 client.get("/api/ideas?status=foobar"),
                 client.get("/api/ideas?blocked=5"),
                 client.delete("/api/ideas/999"),
                 # 方法不对：框架层默认 detail 是英文 "Method Not Allowed"，
                 # 漏了这一条就等于 SPEC 第 9 节「detail 恒为人话」留着口子。
                 client.delete("/api/ideas"),
                 client.put("/api/health")]
    # 只查错误响应：200 本来就没有 detail 字段。带上成功响应是为了顺带确认
    # 扫描逻辑本身没写错。
    errors = [r for r in responses if r.status_code >= 400]
    assert len(errors) >= 12, [r.status_code for r in responses]   # 防止全 200 变恒真
    for r in errors:
        detail = r.json().get("detail")
        assert isinstance(detail, str) and detail, (str(r.request.url),
                                                    r.status_code, detail)


def test_method_not_allowed_speaks_chinese_not_the_framework_english(client):
    """405 曾是全项目唯一一句英文 detail，CLI 会把它原样甩给 agent。

    只断言"是字符串"不够——"Method Not Allowed" 也是字符串。必须钉住中文文案。
    """
    r = client.delete("/api/ideas")
    assert r.status_code == 405
    assert r.json()["detail"] == "这个接口不支持这个请求方法"


def test_unknown_path_detail_is_chinese_too(client):
    """静态目录挂不上时的 404 同走框架层，一并保证是人话。"""
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["detail"] == "没有这个页面"


@pytest.mark.parametrize("param", ["status", "tag", "q", "blocked"])
def test_blank_filter_means_no_filter(param, client):
    """SPEC 5.3：空串表达"没选"，归一成不过滤，不报 422。"""
    client.post("/api/ideas", json={"title": "有标签", "tags": "视频"})
    client.post("/api/ideas", json={"title": "无标签"})
    r = client.get("/api/ideas?%s=" % param)
    assert r.status_code == 200
    assert len(r.json()) == 2                     # 与省略该参数完全一致


@pytest.mark.parametrize("param", ["status", "tag", "q", "blocked"])
def test_whitespace_filter_means_no_filter(param, client):
    """纯空白和空串一样没有表达任何意图，一起归一。"""
    client.post("/api/ideas", json={"title": "有标签", "tags": "视频"})
    client.post("/api/ideas", json={"title": "无标签"})
    r = client.get("/api/ideas?%s=%%20%%20" % param)
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.parametrize("param,bad", [("status", "foobar"), ("blocked", "5")])
def test_blank_does_not_mask_an_illegal_value(param, bad, client):
    """归一只作用于空串：?status=&blocked=5 里的真值仍须 422。"""
    client.post("/api/ideas", json={"title": "a"})
    r = client.get("/api/ideas?%s=&%s=%s" % (param, "blocked" if param == "status"
                                              else "status", bad))
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)


def test_blank_filter_does_not_widen_the_other_filters(client):
    """?q= 不能把 ?status=building 变成不过滤——归一是逐参数的。"""
    client.post("/api/ideas", json={"title": "甲"})
    b = client.post("/api/ideas", json={"title": "乙"}).json()["id"]
    client.post("/api/ideas/%d/advance" % b, json={"to": "building"})
    assert [x["title"] for x in client.get("/api/ideas?q=&status=building").json()] == ["乙"]
    assert len(client.get("/api/ideas?q=").json()) == 2


def test_blocked_error_uses_chinese_field_name(client):
    """SPEC 第 9 节：文案用中文字段名，不打原始键。"""
    r = client.get("/api/ideas?blocked=5")
    assert r.status_code == 422
    assert r.json()["detail"] == "未知取值：5（阻塞筛选只接受 0 或 1）"


# --- Task 4：panel.py CLI（真起 uvicorn，子进程走真实 HTTP）--------------------
# CLI 的失败方式（一句人话 + 退出码 1、不打 Python 栈）是产品契约的一部分，
# 只能端到端验：在本进程里起真 uvicorn，再用子进程真调 skill/panel.py。
# 不 pop/reimport——app/db/store 每次调用都重读 PANEL_DB，模块缓存不是问题。

PANEL_PY = Path(__file__).parent / "skill" / "panel.py"


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    """真起 uvicorn，随机空闲端口，CLI 走真实 HTTP。

    端口先 bind(0) 拿到再 close 交给 uvicorn：存在 TOCTOU 窗口，但本机单进程
    测试下可接受（SPEC 11）；固定端口会和正在运行的面板抢。
    """
    sock = socket.socket()
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("PANEL_URL", "http://127.0.0.1:%d" % port)
    appm = importlib.import_module("app")
    cfg = uvicorn.Config(appm.create_app(), host="127.0.0.1", port=port,
                         log_level="critical")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "uvicorn 没能起来"
    yield "http://127.0.0.1:%d" % port
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture()
def stub_server():
    """假服务端：按给定的 (status, content-type, body) 应答任意 GET/POST。

    存在的理由是**活的服务端造不出这些响应**：未捕获异常的 500 是纯文本而不是
    {"detail": ...}（不在 detail 契约内），框架层处理器也管不到它；字段类型
    对不上的 200 同理，本面板的列都是 NOT NULL。
    """
    started = []

    def _start(*responses):
        """应答依次消费 (status, ctype, body)，用完停在最后一条。

        单条时可以按旧的 `stub_server(200, ctype, body)` 传，也可以直接传一个
        元组序列；给多条是为了验"同一个子命令连打两次"的路径（advance 推到
        当前状态时会先 POST 吃 422、再 GET 补读状态）。
        """
        if responses and not isinstance(responses[0], tuple):
            responses = [responses]         # 旧的三元组调用
        seq = list(responses) or [(200, "application/json", b"{}")]
        hits = [0]

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                status, ctype, body = seq[min(hits[0], len(seq) - 1)]
                hits[0] += 1
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._reply()

            def do_POST(self):
                # 请求体必须读掉：留在缓冲区里，下一个请求会把它当协议行解析。
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                self._reply()

            def do_PATCH(self):
                # PATCH 同样带 body，也同样必须读掉——漏了它，返回的响应会被
                # 当成下一个请求的协议行，表现为莫名其妙的连接错误而不是断言失败。
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                self._reply()

            def log_message(self, *a):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.hits = hits
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    yield _start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def _cli(*args):
    """跑真 CLI 子进程。

    env 显式透传：PANEL_URL 必须进子进程（服务端在父进程的线程里，地址只能靠
    环境变量过去）。靠隐式继承也能跑通，但这条链路是这里要断言的东西之一。
    """
    return subprocess.run(
        [sys.executable, str(PANEL_PY), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ})


def _new_id(title):
    """建一张卡并回传 id——CLI 只打人话，id 得从输出里抠。"""
    r = _cli("new", title)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.split("#")[1].split("「")[0]


def test_cli_full_cycle(live_server):
    """9 个子命令全走一遍：少一个就有一条永远没人走过的路。

    断言同时钉住两件事：输出是人话，以及子进程的 stdout 是 UTF-8——
    Windows 上 stdout 默认跟 locale 走 cp936，`✔` 在里面编不出来。
    """
    import re
    r = _cli("new", "AI 发展史", "--tags", "视频,科普")
    assert r.returncode == 0 and "✔ 已创建 #" in r.stdout
    iid = r.stdout.split("#")[1].split("「")[0]
    r = _cli("advance", iid, "正在明确", "--note", "开始聊需求")
    assert r.returncode == 0 and "正在明确" in r.stdout
    r = _cli("advance", iid, "clarifying")
    # SPEC 6 点名要翻的：推到当前状态得说成"#1 已经在『正在明确』了"，
    # 而不是把"该创意已经处于此状态"原样甩出去——后者没说是哪个状态，
    # 对 agent 没用（4）。
    assert r.returncode == 1 and "#%s 已经在『正在明确』了" % iid in r.stdout
    r = _cli("note", iid, "脚本已定稿")
    assert r.returncode == 0 and "已加备注" in r.stdout
    r = _cli("list", "--status", "clarifying")
    assert "AI 发展史" in r.stdout
    r = _cli("stats")
    assert "正在明确" in r.stdout and "阻塞中" in r.stdout
    r = _cli("show", iid)
    assert "开始聊需求" in r.stdout and "脚本已定稿" in r.stdout
    # 时间线也是人话：中文 kind、"10-12 15:20" 而不是 ISO 串里那个 T。
    # 少了这条，上面两行只验了 message 透传，改坏格式照样绿。
    assert "备注 脚本已定稿" in r.stdout
    assert "创建 AI 发展史" in r.stdout
    assert re.search(r"^\s*\d\d-\d\d \d\d:\d\d \[agent\] 仅有创意 → 正在明确"
                     r" 开始聊需求$", r.stdout, re.M)
    r = _cli("block", iid, "等授权")
    assert "已阻塞" in r.stdout
    r = _cli("unblock", iid)
    assert "已解除阻塞" in r.stdout
    r = _cli("delete", iid)
    assert r.returncode == 0
    assert _cli("show", iid).returncode == 1


def test_cli_missing_id_is_human(live_server):
    r = _cli("show", "999")
    assert r.returncode == 1 and "没有 #999" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_bad_id_type_is_passed_through(live_server):
    """手输 `advance abc building` 是日常操作。

    要的是后端那句「创意 id 必须是整数」，不是 argparse 甩一屏 usage。
    """
    r = _cli("advance", "abc", "building")
    assert r.returncode == 1 and "创意 id 必须是整数" in r.stdout
    assert "usage:" not in r.stdout


def test_cli_unknown_status_is_left_to_the_server(live_server):
    """CLI 侧不维护状态表：原样发出去，让服务端回 422 + 合法值列表。

    自己那份表会和 db.STATUS_KEYS 分叉，面板改列序时它还按旧表翻译。
    """
    iid = _new_id("待推进")
    r = _cli("advance", iid, "瞎写的状态")
    assert r.returncode == 1 and "未知状态" in r.stdout
    for k in db.STATUS_KEYS:
        assert k in r.stdout


def test_cli_status_argument_takes_chinese_and_key(live_server):
    """SPEC 6：advance / list 的状态参数中英文都合法。"""
    iid = _new_id("中文状态卡")
    assert _cli("advance", iid, "正在明确").returncode == 0
    assert "中文状态卡" in _cli("list", "--status", "正在明确").stdout
    assert "中文状态卡" in _cli("list", "--status", "clarifying").stdout


def test_cli_output_survives_characters_the_console_cannot_encode(live_server):
    """标题里的 emoji 在 cp936 控制台上编不出来。

    宁可被替换成 ?，也不能因为编码崩掉——崩掉就意味着 SPEC 第 9 节禁止的
    Python 栈被甩给 agent。
    """
    r = _cli("new", "🎬 分镜表")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "🎬" in r.stdout


def test_cli_server_down(monkeypatch):
    monkeypatch.setenv("PANEL_URL", "http://127.0.0.1:1")
    r = _cli("stats")
    assert r.returncode == 1 and "面板未启动" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


@pytest.mark.parametrize("status,ctype,body,want,unwanted", [
    # 真·500：未捕获异常时服务端返纯文本，不是 {"detail": ...}
    (500, "text/plain; charset=utf-8", b"Internal Server Error",
     "Internal Server Error", "Traceback"),
    # detail 不是字符串（契约外）：也不许把 pydantic 原文甩给 agent
    (500, "application/json", b'{"detail": [{"loc": ["body", "to"]}]}',
     "500", "loc"),
    # 网关/代理错误页：HTML 对 agent 没意义
    (500, "text/html; charset=utf-8", b"<html><body>500</body></html>",
     "500", "<html>"),
    # 200 但不是 JSON：PANEL_URL 指到了网页
    (200, "text/html; charset=utf-8", b"<html>ok</html>",
     "不是 JSON", "<html>"),
])
def test_cli_unreadable_response_is_still_one_human_line(
        stub_server, monkeypatch, status, ctype, body, want, unwanted):
    """响应体读不出来时也必须是一句人话 + 退出码 1，不能是解析异常。"""
    monkeypatch.setenv("PANEL_URL", stub_server(status, ctype, body))
    r = _cli("stats")
    assert r.returncode == 1
    assert want in r.stdout
    assert unwanted not in r.stdout + r.stderr


# --- Task 4 修复轮 1/5：漂移上锁 + 反向断言 + 成功路径的形状兜底 ---------------
# 上一轮的 CLI 测试几乎全是"该出现的出现了"，没有一条"不该出现的没出现"——
# 于是把 `list --status` 从 params 里整个删掉（筛选静默失效）、把 stats 的
# 数字全打成 0、把计数取错，现有测试照样全绿。这一节补的是**红得了**的断言。


def _panel_assign(name):
    """从 skill/panel.py 静态读一个模块级赋值语句。

    刻意**不 import panel.py**：那个模块 import httpx，白盒断言不该被 CLI 的
    依赖可用性带着走。ast 只解析字面量，不执行任何代码。
    """
    tree = ast.parse(PANEL_PY.read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == name for t in n.targets))


def test_cli_label_table_matches_the_server():
    """两份拷贝必须逐字相同——CLI 测试都是黑盒，抓不到这张表的漂移。

    漂移最毒的形态是**标签被复用给另一个 key**：`_to_key` 会把 agent 输入翻成
    错误的 key，服务端照单全收——exit 0、无提示、账本记下错状态。面板的全部
    价值就是账本，所以这张表得锁死（拷贝本身没法消除：安装目录里没有 server/）。
    """
    lbl = ast.literal_eval(_panel_assign("LABEL").value)
    assert lbl == db.STATUS_LABEL
    assert list(lbl) == db.STATUS_KEYS          # 顺序也是契约（stats 列序照它走）


def test_cli_default_url_is_the_panel_port():
    """PANEL_URL 不设时必须是 8472（SPEC 6 明写），改成 8000 得有人喊一声。

    覆盖开关本身的另一半是**行为**的、且已被覆盖：每条 live_server 测试都得
    靠 PANEL_URL 才找得到那个随机端口的 uvicorn，去掉它就会连去 8472 而全红。
    """
    assert ast.literal_eval(_panel_assign("DEFAULT_URL").value) == \
        "http://127.0.0.1:8472"
    base = _panel_assign("BASE_URL").value      # os.environ.get(...).rstrip("/")
    assert getattr(base, "func", None) is not None
    assert getattr(base.func, "attr", None) == "rstrip"
    get = base.func.value                       # os.environ.get("PANEL_URL", …)
    assert get.args[0].value == "PANEL_URL"
    assert getattr(get.args[1], "id", None) == "DEFAULT_URL"


@pytest.mark.parametrize("body,want,unwanted,want_blocked", [
    # 服务端少给一个状态键：不能拿 LABEL 的列序补一列假数出来
    (b'{"counts": {"idea": 2, "brandnew": 1}, "blocked": 1}',
     "仅有创意 2 | brandnew 1", "正在明确", "阻塞中：1"),
    # 服务端连 blocked 都没给（s.get 的缺省那半边）
    (b'{"counts": {"idea": 2}}', "仅有创意 2", "正在明确", "阻塞中：0"),
])
def test_cli_stats_follows_the_servers_columns(
        stub_server, monkeypatch, body, want, unwanted, want_blocked):
    """服务端少给一个状态键时，不能吐栈，也不能拿 LABEL 的列序凑数。

    照 LABEL 遍历是当场 KeyError（SPEC 第 9 节禁止的满屏栈）；而"补个 0 蒙混
    过去"更坏——面板上会多出一列账本里根本没有的数，且没人知道它从哪来。
    第二个变体盯的是 `s.get("blocked", 0)`：写成 `s["blocked"]` 而桩又总带着
    blocked 的话，那半边就永远红不了。
    """
    monkeypatch.setenv("PANEL_URL", stub_server(200, "application/json", body))
    r = _cli("stats")
    assert r.returncode == 0, r.stdout + r.stderr
    assert want in r.stdout                # 列序跟服务端；翻不出来的显示原 key
    assert unwanted not in r.stdout        # 反向：服务端没给的就不该出现
    assert want_blocked in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


@pytest.mark.parametrize("args,body", [
    (("stats",), b"[1, 2]"),                   # 顶层是数组：s["counts"] 抛 TypeError
    (("stats",), b'{"blocked": 1}'),            # 少 counts
    (("stats",), b'{"counts": ["x"], "blocked": 0}'),   # counts 不是对象
    (("stats",), b'{"counts": {"idea": "abc"}, "blocked": 0}'),  # 数字位置上是文本
    (("list",), b'{"items": []}'),              # 顶层是对象：for raw in items 拿到的是键
    (("list",), b'[{"id": 1}]'),                # 少了 status/title/blocked
    (("show", "1"), b'{"id": 1, "title": "x"}'),
    (("show", "1"), b'{"id": 1, "title": "x", "status": "idea", "blocked": 0,'
                    b' "blocked_note": "", "tags": ",", "body": "", "events": {}}'),
])
def test_cli_wrong_shape_on_the_success_path_is_still_one_human_line(
        stub_server, monkeypatch, args, body):
    """**成功**路径的形状不对也必须是人话，不是解析异常。

    这是"错误响应读不出来"的另一半：只修了 4xx/5xx 那一半，200 里顶着一个
    数组照样满屏栈——SPEC 第 9 节禁的是栈，不管它从哪条路漏出来。
    """
    monkeypatch.setenv("PANEL_URL", stub_server(200, "application/json", body))
    r = _cli(*args)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "请确认 PANEL_URL 指向面板" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def _idea_json(**over):
    d = {"id": 1, "title": "分镜表", "status": "idea", "blocked": 0,
         "blocked_note": "", "tags": ",", "body": "", "events": []}
    d.update(over)
    return json.dumps(d).encode("utf-8")


def _row_json(**over):
    d = {"id": 1, "status": "idea", "title": "分镜表", "blocked": 0}
    d.update(over)
    return json.dumps([d]).encode("utf-8")


@pytest.mark.parametrize("args,body", [
    # 进 "#%d" 的必须是整数——键在、类型不对是最隐蔽的一种漏栈
    (("show", "1"), _idea_json(id="abc")),
    (("list",), _row_json(id="abc")),
    (("new", "分镜表"), _idea_json(id="abc")),
    # 进 .strip() / .replace() 的必须是文本
    (("show", "1"), _idea_json(tags=5)),
    (("show", "1"), _idea_json(body=7)),
    (("show", "1"), _idea_json(title=["分镜表"])),
    # 进 label() 的键要是文本：list 当键会抛 unhashable
    (("show", "1"), _idea_json(status=[])),
    (("list",), _row_json(status=[])),
    (("advance", "1", "building"), _idea_json(status=[])),
    # 时间线上的同一批敏感位置：created_at 要能切片，actor/是 label() 那类
    # dict.get 的键（list 当键是 unhashable）
    (("show", "1"), _idea_json(events=[{"actor": "agent", "kind": "note",
                                        "created_at": 5, "message": "备注",
                                        "from_status": None,
                                        "to_status": None}])),
    (("show", "1"), _idea_json(events=[{"actor": [], "kind": "note",
                                        "created_at": "2026-10-12T15:18:32.1",
                                        "message": "备注",
                                        "from_status": None,
                                        "to_status": None}])),
])
def test_cli_wrong_field_type_on_the_success_path_is_still_one_human_line(
        stub_server, monkeypatch, args, body):
    """字段**在**但类型不对，也得是人话——上一轮只查了键在不在。

    这些不是假想：%d、.strip()、.replace()、dict.get(不可哈希的键) 全都会
    抛 TypeError/AttributeError，和漏栈是一回事。
    """
    monkeypatch.setenv("PANEL_URL", stub_server(200, "application/json", body))
    r = _cli(*args)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "请确认 PANEL_URL 指向面板" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


@pytest.mark.parametrize("args,body,absent", [
    (("show", "1"), _idea_json(tags=None), "标签："),
    (("show", "1"), _idea_json(body=None), "描述："),
    (("show", "1"), _idea_json(blocked=1, blocked_note=None), "已阻塞：None"),
])
def test_cli_null_optional_text_is_shown_as_empty(
        stub_server, monkeypatch, args, body, absent):
    """tags/body 这类可选文本收到 null：**当作没填**，不是当成错。

    本面板的列是 NOT NULL，正常走不到 null；但"没有标签"在显示层就是没有，
    不该因为对面少个键就甩一句错给 agent。
    """
    monkeypatch.setenv("PANEL_URL", stub_server(200, "application/json", body))
    r = _cli(*args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "仅有创意" in r.stdout
    assert absent not in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


@pytest.mark.parametrize("field", ["from_status", "to_status", "message"])
def test_cli_timeline_never_prints_python_none(stub_server, monkeypatch, field):
    """时间线里可空字段为 null 时，不能把 Python 的 None 打进输出。

    这三个字段在 schema 里都可能是 null（新建的创意没有来源状态），
    但它们**同属一条时间线记录**，策略必须一致：message 归一成空串，
    from/to 也得归一，否则同一屏里一处是空、一处是「None」。
    本项目 store 恒写非 null，所以这条是拿桩喂外来响应才走得到——
    恰恰因为走不到，才必须用测试钉住，不能靠「跑一遍没看见」。
    """
    event = {"actor": "agent", "kind": "status",
             "created_at": "2026-10-12T15:18:32.1", "message": "推进",
             "from_status": "idea", "to_status": "building"}
    event[field] = None
    monkeypatch.setenv("PANEL_URL", stub_server(
        200, "application/json", _idea_json(events=[event])))
    r = _cli("show", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "None" not in r.stdout, "把 Python 的 None 打进给人看的输出了"
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_advance_status_field_type_is_checked(stub_server, monkeypatch):
    """推进**成功**那条路上的 status：CLI 自己打回的不合格键一样要拦。

    形态是"推进到当前状态"：POST 吃 422，再 GET 补读——补读回来的 status
    同样要查类型，否则 label() 会拿它当 dict 键，unhashable 就漏栈。
    """
    monkeypatch.setenv("PANEL_URL", stub_server(200, "application/json",
                                               _idea_json(status=[])))
    r = _cli("advance", "1", "building")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "请确认 PANEL_URL 指向面板" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_already_in_status_lookup_shape_is_checked(stub_server, monkeypatch):
    """同上，但走的是 422 补读那条路（POST 先吃 422，再 GET 拿状态）。"""
    monkeypatch.setenv("PANEL_URL", stub_server(
        (422, "application/json", b'{"detail": "\\u8be5\\u521b\\u610f\\u5df2\\u7ecf\\u5904\\u4e8e\\u6b64\\u72b6\\u6001"}'),
        (200, "application/json", _idea_json(status=[]))))
    r = _cli("advance", "1", "building")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "请确认 PANEL_URL 指向面板" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_bad_base_url_is_still_one_human_line(monkeypatch):
    """PANEL_URL 手输成 `http://[::1` 抛的是 InvalidURL——它**不继承**
    RequestError，漏掉就又是满屏栈。"""
    monkeypatch.setenv("PANEL_URL", "http://[::1")
    r = _cli("stats")
    assert r.returncode == 1 and "面板未启动" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_stats_prints_the_numbers_in_server_column_order(live_server):
    """只断言"某个标签在输出里"的话，数字全 0、列序错位、计数取错都不会红。

    这里整行相等：相邻两列的数字两两不同（1/2、2/0、0/1、1/0），所以列序
    错一位、或某列取成别人的数，字符串就变。
    """
    _new_id("甲")
    for t in ("乙", "丙"):
        assert _cli("advance", _new_id(t), "clarifying").returncode == 0
    b = _new_id("丁")
    assert _cli("advance", b, "building").returncode == 0
    assert _cli("block", b, "等授权").returncode == 0
    r = _cli("stats")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("  仅有创意 1 | 正在明确 2 | 已经明确 0 | 正在制作 1 | 完成成品 0"
            in r.stdout)
    assert "阻塞中：1" in r.stdout


def test_cli_list_status_filter_actually_filters(live_server):
    """`list --status` 是 9 个子命令里最容易被静默改坏的一个。

    把 status 从 params 里整个删掉就是筛选失效：退出码照样 0，输出照样"看着
    对"，只是多了几条不该在的卡。上一轮只验了"该出现的出现"。
    """
    a = _new_id("在建的片子")
    b = _new_id("还在想法池")
    assert _cli("advance", a, "building").returncode == 0
    r = _cli("list", "--status", "building")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "在建的片子" in r.stdout
    assert "还在想法池" not in r.stdout and "#%s" % b not in r.stdout
    # 反向断言的对照组：不带 --status 时两条都得在——否则"没出现"可能只是
    # 因为压根没建出来，这句断言就恒真了。
    both = _cli("list")
    assert "在建的片子" in both.stdout and "还在想法池" in both.stdout


def test_cli_list_marks_blocked_rows_and_filters_them(live_server):
    """`■` 阻塞标记与 `--blocked` 筛选：上一轮一个都没验过。"""
    a = _new_id("卡住的活")
    _new_id("没卡的活")
    assert _cli("block", a, "等授权").returncode == 0
    rows = {ln.rsplit(" ", 1)[1]: ln
            for ln in _cli("list").stdout.splitlines() if ln.startswith("#")}
    assert re.match(r"^#\d+\s+■ \[仅有创意\] 卡住的活$", rows["卡住的活"])
    assert re.match(r"^#\d+\s+\[仅有创意\] 没卡的活$", rows["没卡的活"])
    only = _cli("list", "--blocked")
    assert only.returncode == 0 and "卡住的活" in only.stdout
    assert "没卡的活" not in only.stdout


def test_cli_new_body_and_source_reach_the_ledger(live_server):
    """--body / --source 从没被走过，`show` 的「描述」分支也就没人验证。

    body/source 不回显在 show 里，所以回读账本本身（store.get_idea）才算数——
    断言 CLI 的回显只能证明它把自己打了一遍。
    """
    r = _cli("new", "分镜表", "--body", "第一幕：夜车外景", "--source", "群里聊的")
    assert r.returncode == 0, r.stdout + r.stderr
    iid = int(r.stdout.split("#")[1].split("「")[0])
    row = store.get_idea(iid)
    assert row["body"] == "第一幕：夜车外景"
    assert row["source"] == "群里聊的"
    assert "描述：第一幕：夜车外景" in _cli("show", str(iid)).stdout


def test_cli_empty_title_is_human(live_server):
    """SPEC 第 9 节点名的「标题不能为空」：CLI 透传 + 退出码 1 + 无栈。"""
    r = _cli("new", "")
    assert r.returncode == 1 and "标题不能为空" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


# ==========================================================================
# Task 6.5：审计用变异测试实证出的契约缺口，逐条补上回归锁
# ==========================================================================


DB_PY = Path(__file__).parent / "server" / "db.py"


def test_pragmas_are_written_in_source(conn):
    """G1 + G5：三条 PRAGMA 语句必须在 db.py 源码里真的写着。

    为什么值断言不够（这正是"读回断言天然证明不了语句存在"的原因）：

    - `journal_mode=WAL`：连 journal_mode 都不查，就没有值可断言。整行删掉，
      254 条测试全绿——它只在并发写时才起作用，本机单进程测试根本触发不到。
    - `recursive_triggers=OFF`：**SQLite 的默认值本来就是 OFF**。所以
      `PRAGMA recursive_triggers` 读回 0 这条断言，语句删掉照样成立——它是被
      默认值满足的。反方向才有锁：改成 ON 会红。也就是说这条断言锁住的是"值"，
      不是"写了这一句"。而 SPEC 4.3.1 说它是**硬性前提，必须有回归锁**。

    所以这里补一条源码级断言。**这是有意为之，不是偷懒**：值断言对
    recursive_triggers 存在一个方向上的盲区（默认值替它兜住了），
    源码断言补的正是那个盲区。两者并存，不要因为"有源码断言了"就删掉值断言——
    值断言锁的是运行时真的生效（连接可能被别处改过），源码断言锁的是语句在。
    """
    src = DB_PY.read_text(encoding="utf-8")
    # 只认真正执行的语句：db.connect() 里 execute 的那个字符串字面量。
    # 用 ast 而不是正则，免得注释里提一句"PRAGMA xxx"就蒙混过关。
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "connect")
    executed = {n.value.strip() for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value.strip().upper().startswith("PRAGMA")}
    assert "PRAGMA foreign_keys=ON" in executed
    assert "PRAGMA recursive_triggers=OFF" in executed
    assert "PRAGMA journal_mode=WAL" in executed


def test_recursive_triggers_statement_removing_would_be_caught(conn):
    """G5 的对照组：证明上面那条源码断言真的会因为语句消失而变红。

    与其相信它会红，不如当场验一次。做法是把 db.py 的源码原样复制到一个临时目录
    跑一遍解析——**不碰真 db.py**（那是唯一不许改字节的文件）。
    """
    import tempfile
    src = DB_PY.read_text(encoding="utf-8")
    # 删掉 recursive_triggers 那一行，其余原样
    mutated = re.sub(r"^\s*c\.execute\(\"PRAGMA recursive_triggers=OFF\"\).*$\n",
                     "", src, flags=re.M)
    assert mutated != src, "变异本身没生效，测的是原文件——先怀疑这行正则"

    def executed_in(text):
        fn = next(n for n in ast.walk(ast.parse(text))
                  if isinstance(n, ast.FunctionDef) and n.name == "connect")
        return {n.value.strip() for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value.strip().upper().startswith("PRAGMA")}

    assert "PRAGMA recursive_triggers=OFF" in executed_in(src)
    assert "PRAGMA recursive_triggers=OFF" not in executed_in(mutated)


# --- G2：position 浮点耗尽重排（SPEC 4.2，全项目既无代码也无测试）---------


def _positions(conn):
    """回读全表 (id, status, position)，按落点排序。"""
    return [(r["id"], r["status"], r["position"]) for r in conn.execute(
        "SELECT id, status, position FROM ideas ORDER BY position, id")]


def _force_position(conn, idea_id, pos):
    """绕过 store 直接摆 position——**造"耗尽"地形必须这么干**。

    走 API 是造不出来的：把一张卡设到离邻居不足 1e-10 的位置，那次写入自己就
    触发了重排，地形当场被抹平（第一次写测试时就踩了这个：设 b 的过程先把
    a、b 一起重排成 1、2，断言读到的是重排后的值）。
    真实世界里这个地形是长期反复插中点拖出来的，不是某一次写入的结果，
    所以用裸 SQL 摆出来才是忠实的模拟。
    """
    conn.execute("UPDATE ideas SET position=? WHERE id=?", (pos, idea_id))
    conn.commit()


def test_position_midpoint_triggers_renumber(conn):
    """落点与相邻卡差值 < 1e-10 时，整列重写为 1,2,3,…（SPEC 4.2）。

    先用裸 SQL 摆出"耗尽"地形：一张卡 position=0，另一张 5e-11——人眼看是同一个
    点，但按 (position, id) 排序它们仍然是两张有先后的卡。
    """
    a = create_idea("前卡")["id"]
    b = create_idea("后卡")["id"]
    _force_position(conn, a, 0.0)
    _force_position(conn, b, 5e-11)

    # 从 0.0 起算 2.5e-11：与两张卡的差值都 < 1e-10，触发重排
    c = create_idea("插进来的", position=2.5e-11)["id"]
    conn.commit()

    got = _positions(conn)
    assert [g[0] for g in got] == [a, c, b], "落点必须在两张卡中间，不能被重写冲掉"
    # 重排把**既有**两卡写成 1、2，插进来的这张取新邻居的中点 1.5。
    # 注意不是 1,2,3：新卡的 position 是算出来的，不是排出来的——
    # 它一旦也写成整数，下次插中点又得重排，白折腾一轮。
    assert [g[2] for g in got] == [1.0, 1.5, 2.0], "该列应被重写为 1,2，新卡取中点"


def test_position_gap_exactly_at_threshold_does_not_renumber(conn):
    """阈值是**严格小于**（SPEC 4.2 原文「小于 1e-10」），等于不触发。

    造相等差值时不能写 1.0 + 1e-10 - 1.0：浮点下那个减法会得到
    1.000000082740371e-10，比 1e-10 大一点点，于是"没重排"是因为差值真的大了，
    而不是因为等于——这条测试就是恒真的假绿。从 0.0 起算才精确：
    abs(0.0 - 1e-10) == 1e-10，一位不差。

    邻居只留一个在阈值上：另一张放远点（3.0）。两张都贴 1e-10 时，落点与其中
    一张的差值是 0（不是 1e-10），min 取到 0，重排照样该触发——那是另一回事。
    """
    a = create_idea("前卡")["id"]
    b = create_idea("后卡")["id"]
    _force_position(conn, a, 0.0)
    _force_position(conn, b, 3.0)          # 远卡：只让 a 构成"恰好等于阈值"的邻居
    assert abs(1e-10 - 0.0) == 1e-10, "前提不成立：这两个值在浮点下不相等"

    # 落点与 a 的差值恰好等于阈值，与 b 的差值很大 -> 不该重排
    c = create_idea("插进来的", position=1e-10)["id"]
    conn.commit()
    got = {g[0]: g[2] for g in _positions(conn)}
    assert got[a] == 0.0 and got[b] == 3.0, "恰好等于阈值时不得重排"
    assert got[c] == 1e-10


def test_renumber_only_touches_its_own_column(conn):
    """只重写该列——别的列的 position 一位都不能动。"""
    a = create_idea("A")["id"]
    b = create_idea("B")["id"]
    _force_position(conn, a, 0.0)
    _force_position(conn, b, 5e-11)
    # 另一列：先摆成耗尽地形，再放一张无关卡记下它的 position
    other = create_idea("别的列")["id"]
    advance(other, "building")
    o2 = create_idea("别的列2")["id"]
    advance(o2, "building", position=7.25)
    conn.commit()

    create_idea("插进来的", position=2.5e-11)
    conn.commit()

    got = {g[0]: (g[1], g[2]) for g in _positions(conn)}
    assert got[other][1] == 1, "被重排的是目标列"
    assert got[o2][1] == 7.25, "无关列的 position 不能被连坐"


def test_renumber_writes_no_extra_events(conn):
    """重排是内部维护动作，不是用户编辑——时间线上不该凭空多出记录。"""
    a = create_idea("A")["id"]
    b = create_idea("B")["id"]
    _force_position(conn, a, 0.0)
    _force_position(conn, b, 5e-11)
    conn.commit()
    before = len(get_idea(a)["events"]) + len(get_idea(b)["events"])

    create_idea("插进来的", position=2.5e-11)
    conn.commit()

    after = len(get_idea(a)["events"]) + len(get_idea(b)["events"])
    assert after == before, "重排不得给别人的卡片写 event"


def test_patch_position_triggers_renumber(conn):
    """列内移动（PATCH {position}）同样要走重排——三条写入路径缺一条就等于没做。

    这条是补变异验证发现的：把 update_idea 里的 _settle_position 调用删掉，
    原有四条重排测试**全绿**（它们都走 create 路径）。前端列内拖拽发的正是
    PATCH，漏掉它等于 SPEC 4.2 的承诺在最常用的路径上是空的。
    """
    a = create_idea("A")["id"]
    b = create_idea("B")["id"]
    c = create_idea("C")["id"]
    _force_position(conn, a, 0.0)
    _force_position(conn, b, 5e-11)
    _force_position(conn, c, 9.0)      # C 挪到最前面，落点与 a 的差值 2.5e-11

    update_idea(c, {"position": 2.5e-11})
    conn.commit()

    got = _positions(conn)
    assert [g[0] for g in got] == [a, c, b], "列内移动后 C 应落在 a 与 b 之间"
    assert [g[2] for g in got] == [1.0, 1.5, 2.0], "PATCH 路径同样要触发重排"


def test_renumber_excludes_the_card_being_moved(conn):
    """重排必须把"正在移动的那张"排除在外，否则它会把自己算成自己的邻居。

    关键在于场景：**被移动的卡自己就躺在耗尽区里**（c 在 0.0 与 b 的 5e-11 之间）。
    这时若不排除 c，找"落点两侧邻居"会选中 c 自己，而重排结果表里偏偏没有 c
    （它马上要按新落点写），于是查表炸掉——顺序错乱之外还多一个 500。

    换句话说：排除不是为了"重排时别动它"，而是为了**邻居不能是它自己**。
    这条也是补变异验证时才发现写错的——第一版把 c 摆在 9.0（耗尽区之外），
    那种场景下排除与否结果完全相同，测了等于没测。
    """
    a = create_idea("A")["id"]              # 0.0
    c = create_idea("C")["id"]              # 5e-11，正落在耗尽区里
    b = create_idea("B")["id"]              # 1.0
    _force_position(conn, a, 0.0)
    _force_position(conn, c, 5e-11)
    _force_position(conn, b, 1.0)

    # 把 c 挪到 0.0 与 5e-11 之间的落点上——它的旧邻居正是耗尽区
    update_idea(c, {"position": 2.5e-11})
    conn.commit()

    got = {g[0]: g[2] for g in _positions(conn)}
    assert got[a] == 1.0 and got[b] == 2.0, "重排只该改写除移动卡以外的卡"
    assert got[c] == 1.5, "移动中的卡取新邻居的中点"


# --- G3：CLI list --tag / --q 零覆盖 -------------------------------------


def _new_with_tags(title, tags):
    """建一张带标签的卡并回传 id——没有单独的改标签命令，标签只能在 new 时给。"""
    r = _cli("new", title, "--tags", tags)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.split("#")[1].split("「")[0]


def test_cli_list_tag_filter_actually_filters(live_server):
    """`--tag` 与 `--status` 是同一类静默失效：把 "tag": a.tag 整个删掉，
    退出码照样 0、输出照样"看着对"，只是多了几条不该在的卡。

    对照组不可省：不带 --tag 时两张卡都必须在，否则"没出现"可能只是因为
    压根没建出来，整句断言就恒真了。
    """
    keep = _new_with_tags("构图那张", "构图,动效")
    drop = _new_with_tags("动效那张", "音频")
    r = _cli("list", "--tag", "构图")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "构图那张" in r.stdout
    assert "动效那张" not in r.stdout

    both = _cli("list")
    assert "构图那张" in both.stdout and "动效那张" in both.stdout
    assert store.get_idea(keep)["tags"] == ",构图,动效,"   # 前提：标签真落库了


def test_cli_list_q_filter_actually_filters(live_server):
    """`--q` 同理：删掉 "q": a.q 六条 CLI 测试全绿，筛选静默失效。

    特意挑一个只出现在标题里的词：--q 是 title/body 各做一次子串匹配
    （SPEC 5.3），只命中标题才算真的传到了服务端。
    """
    keep = _new_id("分镜表初稿")
    drop = _new_id("配乐调研")
    r = _cli("list", "--q", "分镜")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "分镜表初稿" in r.stdout
    assert "配乐调研" not in r.stdout

    both = _cli("list")
    assert "分镜表初稿" in both.stdout and "配乐调研" in both.stdout


# --- Task 9 / Part A：CLI `edit`（SPEC 7.2 第 2 步唯一的落笔路径）--------------
# SPEC 7.2 的 agent 标准流程第 2 步是「brainstorming 结论写进 body」，而
# brainstorming 发生在建卡**之后**——`new` 只能在建卡那一刻带 body。所以没有
# `edit` 时那一步根本执行不了，agent 只能请人去网页抽屉里改。


def test_cli_edit_writes_the_concluded_body(live_server):
    """SPEC 7.2 第 2 步的正面路径：聊完需求把结论写回 body。"""
    iid = _new_id("AI 发展史")
    r = _cli("edit", iid, "--body", "6 段 H3，每段 90 秒，面向高中生")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "已修改" in r.stdout and "描述" in r.stdout
    assert store.get_idea(int(iid))["body"] == "6 段 H3，每段 90 秒，面向高中生"
    assert "描述：6 段 H3，每段 90 秒，面向高中生" in _cli("show", iid).stdout


def test_cli_edit_empty_string_clears_and_absent_field_is_untouched(live_server):
    """**`--body ""` 与不传 `--body` 必须是两件事**——本块最要紧的一条。

    判据若是真值判断（`if a.body:`）而不是 `is not None`，"传空串想清空"会
    和"没传"一起被丢掉：CLI 退出码 0、输出"已修改"，描述原封不动躺在账本里。
    退出码和 stdout 都对，**只有回读账本才抓得到**——所以这条断言不查输出。
    """
    iid = int(_new_id("分镜表"))
    before = store.create_idea(title="第二张", body="原始描述",
                               tags="视频", source="群里")
    assert store.get_idea(before["id"])["body"] == "原始描述"

    r = _cli("edit", str(before["id"]), "--body", "")
    assert r.returncode == 0, r.stdout + r.stderr
    cleared = store.get_idea(before["id"])
    assert cleared["body"] == ""            # 空串 = 清空

    # 反向断言的对照组：只改 source 时，已清空的 body 不能再被"顺手重置"回去，
    # 没传的三个字段也一律不许动。这半句不可省——没有它，上面那句恒真。
    r = _cli("edit", str(before["id"]), "--source", "换了来源")
    assert r.returncode == 0, r.stdout + r.stderr
    after = store.get_idea(before["id"])
    assert after["body"] == ""              # 没传 --body，所以没被动
    assert after["title"] == "第二张"
    assert after["tags"] == ",视频,"
    assert after["source"] == "换了来源"


def test_cli_edit_sends_only_the_fields_given_on_the_command_line(live_server):
    """反向断言（正面）：payload 里不能混进没传的字段。

    SPEC 5 的 PATCH 语义是"只有你传了的字段才改"。实现最容易犯的懒是把
    payload 凑成全部六项、没传的用 `a.title or 初始值` 之类的兜底填进去——
    那会把用户没打算改的字段覆盖掉。这里看 **event 的 message**：store 记的
    就是 CLI 发过去的键，逐字对得上才算没多发。
    """
    iid = int(_new_id("配乐调研"))
    _cli("edit", str(iid), "--title", "配乐调研（改）", "--source", "群里")
    events = store.get_idea(iid)["events"]
    edited = [e for e in events if e["kind"] == "edited"]
    assert len(edited) == 1
    # EDIT_FIELDS 序 → title、source。body/tags/blocked_note/position 一个都不许在。
    assert edited[0]["message"] == "修改了：title、source"
    for absent in ("body", "tags", "blocked_note", "position"):
        assert absent not in edited[0]["message"]


def test_cli_edit_does_not_claim_fields_it_did_not_change(live_server):
    """输出念的必须是**发了的**字段，不是响应里的全部字段。

    反向断言。变异测试实证：把 `names` 改成 `list(EDIT_FIELDS)`（即无条件念全
    六个字段名）时，上面那条 event 断言照样绿——因为账本记的是对的，**只有
    stdout 在撒谎**。

    这条谎报的代价很实：agent 看到"已修改：标题、描述、标签、来源、阻塞备注、
    排序位置"，会认为六项都落库了，而实际只有一项被碰过。面板的全部价值就是
    账本可信，所以嘴上说的和账上记的必须一致。
    """
    iid = int(_new_id("只改标题的卡"))
    r = _cli("edit", str(iid), "--title", "只改标题的卡（改）")
    assert r.returncode == 0, r.stdout + r.stderr
    said = r.stdout.splitlines()[0]
    assert "标题" in said
    for never_given, cn in (("描述", None), ("标签", None), ("来源", None),
                            ("阻塞备注", None), ("排序位置", None)):
        assert never_given not in said, "念了没发过的字段：%s" % never_given


def test_cli_edit_several_fields_in_one_call_write_one_event(live_server):
    """SPEC 5.5：一次 PATCH 无论改几个字段，只写一条 edited。"""
    iid = int(_new_id("分镜"))
    r = _cli("edit", str(iid), "--title", "分镜表", "--body", "第一幕夜车",
             "--tags", "视频,分镜")
    assert r.returncode == 0, r.stdout + r.stderr
    row = store.get_idea(iid)
    assert (row["title"], row["body"], row["tags"]) == ("分镜表", "第一幕夜车",
                                                        ",视频,分镜,")
    assert [e["kind"] for e in row["events"]] == ["created", "edited"]
    assert row["events"][-1]["message"] == "修改了：title、body、tags"


def test_cli_edit_echoes_the_new_title(live_server):
    """改了标题要说清新标题是什么，否则 agent 得再 show 一次才知道。"""
    iid = _new_id("旧标题")
    r = _cli("edit", iid, "--title", "新标题")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "新标题" in r.stdout


@pytest.mark.parametrize("args,fragment", [
    (("edit", "1"), "没有可更新的字段"),              # 空对象 → 空 payload
    (("edit", "999", "--body", "x"), "没有 #999"),   # id 不存在
    (("edit", "1", "--position", "abc"), "排序位置 必须是数字"),
])
def test_cli_edit_server_errors_are_human(live_server, args, fragment):
    """服务端 detail 原样透传 + 退出码 1 + 无栈（SPEC 第 9 节）。"""
    iid = _new_id("占位卡")
    args = [str(iid) if a == "1" else a for a in args]
    r = _cli(*args)
    assert r.returncode == 1, r.stdout + r.stderr
    assert fragment in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_edit_never_sends_null_because_it_cannot_express_it(
        stub_server, monkeypatch):
    """**CLI 结构上发不出 null**——所以 null 守卫（SPEC 5.5）从 CLI 侧够不着。

    这是 `edit` 设计的结果而不是漏洞：argparse 的一切都是文本，`--body None`
    传出去是字符串 `"None"` 而不是 JSON null。断言的是"这条路上没有 null"，
    所以服务端那条 422 只能由网页端触发。

    顺带钉住透传：detail 原样打给人看，不加壳、不吞掉。
    """
    monkeypatch.setenv("PANEL_URL", stub_server(
        422, "application/json",
        json.dumps({"detail": "描述 不能为 null，请传空串"},
                   ensure_ascii=False).encode("utf-8")))
    r = _cli("edit", "1", "--body", "x")
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.strip() == "描述 不能为 null，请传空串"
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_edit_has_no_status_option(live_server):
    """status 的唯一入口是 advance（SPEC 5.1）。CLI 层就得没有 --status，
    否则等于给"改状态"开了一个会被服务端 422 挡住的旁门。"""
    r = _cli("edit", "1", "--status", "done")
    assert r.returncode != 0
    assert "已修改" not in r.stdout
    assert store.get_idea(int(_new_id("不该被动")))["status"] == "idea"


def test_cli_edit_field_table_matches_the_server():
    """EDIT_FIELDS / FIELD_LABEL 两份拷贝必须与 store 逐字相同。

    同 LABEL 的理由（install_skill.bat 只同步 skill/，那边 import 不到
    server/store.py）：字段表漂移的后果是 CLI 把一个字段改名/漏掉，用户改了
    半天发现没改到，或者把 A 字段的值写进 B 字段——账本上还是 200 + 一条
    "已修改"，正是这个面板最不能有的静默错误。
    """
    fields = ast.literal_eval(_panel_assign("EDIT_FIELDS").value)
    labels = ast.literal_eval(_panel_assign("FIELD_LABEL").value)
    assert tuple(fields) == tuple(store.FIELDS)        # 顺序也是契约（输出念它）
    assert labels == store._FIELD_LABEL
    assert set(labels) == set(fields)                  # 少一个就是"念不出来"


def test_cli_edit_wrong_shape_on_the_success_path_is_still_one_human_line(
        stub_server, monkeypatch):
    """PATCH 回的响应形状不对时同样是人话，不是解析异常（SPEC 第 9 节）。"""
    monkeypatch.setenv("PANEL_URL", stub_server(200, "application/json",
                                                 b'[{"id": 1}]'))
    r = _cli("edit", "1", "--body", "x")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "请确认 PANEL_URL 指向面板" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_edit_field_type_on_the_success_path_is_checked(
        stub_server, monkeypatch):
    """id 进 "%d"、title 要能拼串——类型不对时收口成人话。

    少了这条：把 `_obj(...)` 的 types= 整个删掉，六条 edit 测试照样全绿，
    只在 PANEL_URL 指到别的服务时才炸成一屏栈。
    """
    monkeypatch.setenv("PANEL_URL", stub_server(
        200, "application/json", _idea_json(id="abc")))
    r = _cli("edit", "1", "--body", "x")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "类型不对" in r.stdout and "请确认 PANEL_URL 指向面板" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr


# --- 项目文档 / 项目主文件夹 / agent 对话名（SPEC 10）----------------------
# 起因：一件作品的信息量远超"标题 + 一句话描述"。真正的上下文在文档里、在工作
# 目录里、在那次对话里，面板要能指回去——但**只指**，不复制：库里的副本一定会
# 和磁盘上的真文档对不上。

_V1_SCHEMA = """
CREATE TABLE ideas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'idea',
  blocked INTEGER NOT NULL DEFAULT 0,
  blocked_note TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT ',',
  source TEXT NOT NULL DEFAULT '',
  position REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  done_at TEXT,
  CHECK (status IN ('idea','clarifying','clarified','building','done'))
);
"""


def test_init_db_migrates_v1_library_without_losing_rows(tmp_path, monkeypatch):
    """存量库必须原地长出新列，旧行一个不少。

    ALTER TABLE ADD COLUMN 在 SQLite 里对已有行填的是 DEFAULT，所以老卡拿到的是
    空串而不是 NULL——它们本来就是"没填项目文件夹"，空串才是对的语义。
    """
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "old.db"))
    c = sqlite3.connect(str(tmp_path / "old.db"))
    c.executescript(_V1_SCHEMA)
    c.execute("INSERT INTO ideas(title,created_at,updated_at) VALUES('老卡','t','t')")
    c.commit()
    c.close()

    db.init_db()

    c2 = db.connect()
    try:
        cols = {r["name"] for r in c2.execute("PRAGMA table_info(ideas)")}
        assert {"project_dir", "agent_session"} <= cols
        row = c2.execute("SELECT title, project_dir, agent_session FROM ideas").fetchone()
        assert row["title"] == "老卡"
        assert row["project_dir"] == "" and row["agent_session"] == ""
        assert c2.execute("SELECT user_version FROM pragma_user_version").fetchone()[0] == 2
    finally:
        c2.close()


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """连跑两次不能炸。

    判存在性靠 PRAGMA table_info 而不是 user_version，所以"版本号对不上但列已经
    在了"的库也能起来——只信版本号会撞 duplicate column 而整个面板起不来。
    """
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "twice.db"))
    db.init_db()
    db.init_db()
    c = db.connect()
    try:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(ideas)")]
        assert cols.count("project_dir") == 1
    finally:
        c.close()


def test_project_fields_roundtrip_and_null_guard(conn):
    i = create_idea("作品", project_dir="D:/coding/作品", agent_session="和 agent 聊分镜")
    assert i["project_dir"] == "D:/coding/作品"
    assert i["agent_session"] == "和 agent 聊分镜"
    assert get_idea(i["id"])["agent_session"] == "和 agent 聊分镜"

    updated = update_idea(i["id"], {"project_dir": "D:/coding/别处"})
    assert updated["project_dir"] == "D:/coding/别处"
    with pytest.raises(Invalid) as e:
        update_idea(i["id"], {"project_dir": None})
    assert "项目主文件夹" in str(e.value) and "不能为 null" in str(e.value)


def test_doc_add_list_read_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "d.db"))
    db.init_db()
    md = tmp_path / "分镜.md"
    md.write_text("# 第一集\n\n开场。", encoding="utf-8")
    i = create_idea("作品")
    d = store.add_doc(i["id"], str(md))
    assert d["title"] == "分镜"          # 标题缺省用文件名，不留空
    assert d["exists"] is True
    assert d["path"] == str(md.resolve())

    lst = store.list_docs(i["id"])
    assert [x["id"] for x in lst] == [d["id"]]
    assert store.read_doc(i["id"], d["id"])["text"].startswith("# 第一集")

    assert store.remove_doc(i["id"], d["id"]) is True
    assert store.list_docs(i["id"]) == []


def test_doc_rejects_non_document_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "x.db"))
    db.init_db()
    """只放文档。挡住 .env / .db 之类不是因为这里构成安全边界（面板绑
    127.0.0.1，本地进程都打得进来），是防止手滑把密钥文件挂进面板。"""
    i = create_idea("作品")
    bad = tmp_path / "secret.env"
    bad.write_text("TOKEN=1", encoding="utf-8")
    with pytest.raises(Invalid) as e:
        store.add_doc(i["id"], str(bad))
    assert "markdown" in str(e.value)
    with pytest.raises(Invalid) as e2:
        store.add_doc(i["id"], "   ")
    assert "不能为空" in str(e2.value)


def test_doc_duplicate_rejected_even_via_relative_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "q.db"))
    db.init_db()
    """判重按归一化后的绝对路径。

    同一份文档写成绝对路径和相对路径是同一个意思，形态不同不能当成两篇——
    否则清单里会出现两条一模一样的东西。"""
    md = tmp_path / "spec.md"
    md.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    i = create_idea("作品")
    store.add_doc(i["id"], str(md))
    with pytest.raises(Invalid) as e:
        store.add_doc(i["id"], "spec.md")
    assert "已经挂" in str(e.value)


def test_missing_file_and_missing_record_are_different(tmp_path, monkeypatch):
    """两种"不存在"必须分开：文档记录没有 -> NotFound；文件被删 -> exists=False。

    合在一起的话，"文档被人删了"会显示成接口坏了，人会以为面板出错，而引用
    其实还在、还能补回来。"""
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "g.db"))
    db.init_db()
    md = tmp_path / "gone.md"
    md.write_text("临时", encoding="utf-8")
    i = create_idea("作品")
    d = store.add_doc(i["id"], str(md))
    md.unlink()

    got = store.read_doc(i["id"], d["id"])
    assert got["exists"] is False and got["text"] == ""

    with pytest.raises(NotFound):
        store.read_doc(i["id"], 9999)
    with pytest.raises(NotFound):
        store.remove_doc(i["id"], 9999)


def test_docs_cascade_on_idea_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "z.db"))
    db.init_db()
    """删卡片必须连带删文档引用——靠 FK ON DELETE CASCADE，不靠手动清理。

    留着孤儿行的话，AUTOINCREMENT 不回收 id，将来重建一张卡可能撞上旧文档
    记录，而这种错不会报错，只会让新卡的文档莫名其妙多出来。"""
    md = tmp_path / "a.md"
    md.write_text("x", encoding="utf-8")
    i = create_idea("作品")
    store.add_doc(i["id"], str(md))
    delete_idea(i["id"])
    c = db.connect()
    try:
        assert c.execute("SELECT COUNT(*) FROM idea_docs").fetchone()[0] == 0
    finally:
        c.close()


def test_doc_leaves_timeline_trace(tmp_path, monkeypatch):
    """挂载/移除要留痕。复用 edited kind 而不是新增——events.kind 有 CHECK
    约束，加 kind 得整表重建，为一条时间线文案不值得。"""
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "e.db"))
    db.init_db()
    md = tmp_path / "设定.md"
    md.write_text("x", encoding="utf-8")
    i = create_idea("作品")
    d = store.add_doc(i["id"], str(md), actor="agent")
    kinds = [(e["kind"], e["message"]) for e in get_idea(i["id"])["events"]]
    assert ("edited", "挂载文档：%s" % d["title"]) in kinds
    store.remove_doc(i["id"], d["id"], actor="agent")
    kinds = [(e["kind"], e["message"]) for e in get_idea(i["id"])["events"]]
    assert ("edited", "移除文档：%s" % d["title"]) in kinds


def test_docs_count_on_list_is_not_n_plus_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "n.db"))
    db.init_db()
    md = tmp_path / "a.md"
    md.write_text("x", encoding="utf-8")
    a = create_idea("甲")
    b = create_idea("乙")
    store.add_doc(a["id"], str(md))
    rows = {r["title"]: r for r in list_ideas()}
    assert rows["甲"]["docs_count"] == 1
    assert rows["乙"]["docs_count"] == 0


def test_read_doc_truncates_and_survives_gbk(tmp_path, monkeypatch):
    """大文档截断不报错；GBK 文档不 500。

    直接 decode("utf-8") 会在 GBK 中文文档上抛 UnicodeDecodeError，整个抽屉打
    不开——比给一段带替换符的文本糟糕得多。"""
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "r.db"))
    db.init_db()
    big = tmp_path / "big.md"
    big.write_text("字" * (store.DOC_MAX_BYTES // 3 + 10), encoding="utf-8")
    i = create_idea("作品")
    d = store.add_doc(i["id"], str(big))
    assert store.read_doc(i["id"], d["id"])["truncated"] is True

    gbk = tmp_path / "gbk.md"
    gbk.write_bytes("标题\n正文".encode("gbk"))
    d2 = store.add_doc(i["id"], str(gbk))
    assert "正文" in store.read_doc(i["id"], d2["id"])["text"]


def test_doc_http_endpoints_and_static_route_not_shadowed(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "h.db"))
    appm = importlib.import_module("app")
    monkeypatch.setattr(appm, "WEB_DIR", Path(__file__).resolve().parent / "web")
    c = TestClient(appm.create_app())

    md = tmp_path / "手册.md"
    md.write_text("# 手册", encoding="utf-8")
    i = c.post("/api/ideas", json={"title": "作品"}).json()

    r = c.post("/api/ideas/%d/docs" % i["id"], json={"path": str(md)})
    assert r.status_code == 201
    doc = r.json()
    assert doc["title"] == "手册" and doc["exists"] is True

    assert c.get("/api/ideas/%d/docs" % i["id"]).json()["docs"][0]["id"] == doc["id"]

    body = c.get("/api/ideas/%d/docs/%d/content" % (i["id"], doc["id"]))
    assert body.status_code == 200 and body.json()["text"] == "# 手册"

    # 卡片不存在 -> 404，而不是 500
    assert c.get("/api/ideas/999/docs").status_code == 404
    # 文件没了 -> 200 + exists:false，不该是 404
    md.unlink()
    gone = c.get("/api/ideas/%d/docs/%d/content" % (i["id"], doc["id"]))
    assert gone.status_code == 200 and gone.json()["exists"] is False

    assert c.delete("/api/ideas/%d/docs/%d" % (i["id"], doc["id"])).status_code == 200
    assert c.get("/api/ideas/%d/docs/%d/content" % (i["id"], doc["id"])).status_code == 404
    # /api/* 没被挂在 "/" 的静态目录盖掉
    assert c.get("/api/stats").status_code == 200


def test_patch_project_fields_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "p.db"))
    appm = importlib.import_module("app")
    c = TestClient(appm.create_app())
    i = c.post("/api/ideas", json={"title": "作品",
                                   "project_dir": "D:/work",
                                   "agent_session": "第 3 轮对话"}).json()
    assert i["project_dir"] == "D:/work" and i["agent_session"] == "第 3 轮对话"
    r = c.patch("/api/ideas/%d" % i["id"], json={"agent_session": "第 4 轮对话"})
    assert r.json()["agent_session"] == "第 4 轮对话"
    assert c.patch("/api/ideas/%d" % i["id"], json={"project_dir": None}).status_code == 422


def test_get_idea_carries_docs_for_the_drawer(tmp_path, monkeypatch):
    """抽屉一次 GET 就要画全，docs 必须在详情里——少一个字段前端就得为打开
    抽屉多打一发请求，而那种"少打了一发也没事"的错最难发现。"""
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "d.db"))
    db.init_db()
    md = tmp_path / "x.md"
    md.write_text("x", encoding="utf-8")
    i = create_idea("作品")
    store.add_doc(i["id"], str(md))
    d = get_idea(i["id"])
    assert isinstance(d["docs"], list) and len(d["docs"]) == 1
