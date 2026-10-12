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