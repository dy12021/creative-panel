"""创意面板后端回归测试。设计依据 SPEC 第 11 节。"""
import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

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
    """11/12. 新建 position 递增；同值按 id。"""
    a = create_idea("a"); c = create_idea("b"); e = create_idea("c")
    assert [x["id"] for x in list_ideas(status="idea")] == [a["id"], c["id"], e["id"]]
    # position 必须真正参与排序：99 比 2、3 都大，a 应当落到末尾
    update_idea(a["id"], {"position": 99.0})
    assert [x["id"] for x in list_ideas(status="idea")] == [c["id"], e["id"], a["id"]]
    # 三条同值时由 id 兜底，顺序必须稳定
    for x in (a, c, e):
        update_idea(x["id"], {"position": 0.0})
    assert [x["id"] for x in list_ideas(status="idea")] == [a["id"], c["id"], e["id"]]


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


IDEA_KEYS = {"id", "title", "body", "status", "blocked", "blocked_note",
             "tags", "source", "position", "created_at", "updated_at", "done_at"}


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
    assert set(create_idea("x")) == IDEA_KEYS          # create 不带 events
    a = create_idea("y")
    for d in (get_idea(a["id"]), update_idea(a["id"], {"title": "z"}),
              advance(a["id"], "clarifying"), add_note(a["id"], "n"),
              block(a["id"], "r"), unblock(a["id"])):
        assert set(d) == IDEA_KEYS | {"events"}


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


def test_list_ideas_groups_by_status_then_position(conn):
    """同列内按 position, id 排；跨列时同一列的卡片必须连续。

    注意 ORDER BY 里的 status 是**字典序**（building<clarified<clarifying<
    done<idea），不是看板列序 idea→clarifying→…→done。前端 web/app.js 自带
    STATUS_ORDER 自行分桶，所以不影响渲染——但下层不该被误读成"已按列序排好"。
    真要按看板列序排必须显式 CASE，不能依赖字典序。
    """
    a = create_idea("a")
    b = create_idea("b")
    c = create_idea("c")
    advance(a["id"], "building")
    advance(b["id"], "building")
    update_idea(a["id"], {"position": 5.0})          # 列内把 a 挪到 b 后面
    got = list_ideas()
    assert [x["id"] for x in got] == [b["id"], a["id"], c["id"]]
    assert [x["status"] for x in got] == ["building", "building", "idea"]