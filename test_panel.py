"""创意面板后端回归测试。设计依据 SPEC 第 11 节。"""
import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest
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
    assert r.json() == {"ok": True, "version": "0.1.0"}       # VERSION 是契约的一部分


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


def test_create_app_works_before_web_dir_exists(client):
    """web/ 要到 Task 5 才建；目录不存在时 StaticFiles 会当场抛错。"""
    assert client.get("/").status_code == 404


def test_web_dir_mount_does_not_shadow_api(tmp_path, monkeypatch):
    """挂载必须在最后：/api/* 仍走路由，/ 才落到静态目录。"""
    appm = importlib.import_module("app")
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "c.db"))
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>面板</h1>", encoding="utf-8")
    monkeypatch.setattr(appm, "WEB_DIR", web)
    c = TestClient(appm.create_app())
    assert c.get("/api/health").json() == {"ok": True, "version": "0.1.0"}
    assert "面板" in c.get("/").text
    assert c.get("/api/stats").status_code == 200


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
                 client.delete("/api/ideas/999")]
    # 只查错误响应：200 本来就没有 detail 字段。带上成功响应是为了顺带确认
    # 扫描逻辑本身没写错。
    errors = [r for r in responses if r.status_code >= 400]
    assert len(errors) >= 12, [r.status_code for r in responses]   # 防止全 200 变恒真
    for r in errors:
        detail = r.json().get("detail")
        assert isinstance(detail, str) and detail, (str(r.request.url),
                                                    r.status_code, detail)


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