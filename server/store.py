"""数据访问层。纯数据操作，不感知 HTTP。

设计依据 SPEC 第 5 节。异常：NotFound / Invalid（Invalid 带人话 detail）。
"""
import db


class NotFound(Exception):
    pass


class Invalid(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


FIELDS = ("title", "body", "tags", "source", "blocked_note", "position")


_FIELD_LABEL = {"title": "标题", "body": "描述", "source": "来源",
                "tags": "标签", "blocked_note": "阻塞备注"}


def _text(v, what):
    """写入 NOT NULL 文本列的入参统一收口。

    null 与类型错误是两回事，文案也必须分开（SPEC 5.1 / 5.4）：null 说"请传空串"，
    类型错说"必须是字符串"。混成一条的话，客户端拿到 422 也看不懂自己错在哪——
    Task 3 把 detail 原样返给客户端。

    类型这条不是防御性洁癖：list/dict 会漏出 sqlite3.ProgrammingError（500），
    而 int/float/bool 会被 TEXT 亲和性静默转成字符串（title=True 落库成 "1"）。
    """
    if v is None:
        raise Invalid("%s 不能为 null，请传空串" % what)
    if not isinstance(v, str):
        raise Invalid("%s 必须是字符串" % what)
    return v


def _parse_tags(raw):
    """把 db.parse_tags 的所有失败收敛成 Invalid。

    ValueError 是"提供了但规范化后全空"；TypeError/AttributeError 是
    传了非字符串（JSON 里 {"tags": 5} 就会走到后者）。
    """
    try:
        return db.parse_tags(raw)
    except ValueError as e:
        raise Invalid(str(e))
    except (TypeError, AttributeError):
        raise Invalid("标签必须是逗号分隔的字符串")


def _board_order():
    """看板列序取自 db.STATUS_KEYS，不硬编码；返回 (SQL 片段, 绑定参数)。

    不能直接 ORDER BY status——那是字典序，会把 building 排到 idea 前面，
    CLI 的 list 输出看起来是乱的。列序由 db.STATUS_KEYS 定义，这里现算而不是
    模块级缓存，免得改了看板列序这边静默失效。
    """
    return ("CASE status " + " ".join("WHEN ? THEN ?" for _ in db.STATUS_KEYS)
            + " END",
            [x for i, k in enumerate(db.STATUS_KEYS) for x in (k, i)])


def _row(c, r):
    return {k: r[k] for k in r.keys() if k != "events"}


def add_event(c, idea_id, kind, actor, message="", from_status=None, to_status=None):
    # actor/message 走这里的是所有事件类写入口，统一在收口处挡 null
    actor = _text(actor, "actor")
    message = _text(message, "事件内容")
    c.execute(
        "INSERT INTO events(idea_id,kind,from_status,to_status,actor,message,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (idea_id, kind, from_status, to_status, actor, message, db.now_iso()),
    )


def _next_position(c, status):
    """空列必须用 COALESCE，否则 MAX 返回 NULL 撞 NOT NULL（SPEC 4.2）。"""
    return c.execute(
        "SELECT COALESCE(MAX(position),0)+1 FROM ideas WHERE status=?", (status,)
    ).fetchone()[0]


def create_idea(title, body="", tags=None, source="", actor="human", position=None):
    if title is None:              # null 视同"没给标题"，走 SPEC 错误表那条文案
        raise Invalid("标题不能为空")
    title = _text(title, "标题")   # 非字符串在这里挡，否则下面 .strip() 抛 AttributeError
    if not title.strip():
        raise Invalid("标题不能为空")
    body = _text(body, "描述")
    source = _text(source, "来源")
    tags_v = _parse_tags(tags)
    c = db.connect()
    try:
        pos = _next_position(c, "idea") if position is None else position
        ts = db.now_iso()
        cur = c.execute(
            "INSERT INTO ideas(title,body,status,tags,source,position,created_at,updated_at)"
            " VALUES(?,?,'idea',?,?,?,?,?)",
            (title.strip(), body, tags_v, source, pos, ts, ts),
        )
        add_event(c, cur.lastrowid, "created", actor, title.strip())
        c.commit()
        return _row(c, c.execute("SELECT * FROM ideas WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        c.close()


def list_ideas(status=None, tag=None, q=None, blocked=None):
    if status is not None and status not in db.STATUS_KEYS:
        raise Invalid("未知状态：%s" % status)
    where, args = [], []
    if status:
        where.append("status=?"); args.append(status)
    if tag is not None:
        where.append("tags LIKE ?"); args.append("%%,%s,%%" % tag)
    if blocked is not None:
        where.append("blocked=?"); args.append(1 if blocked in (1, "1", True) else 0)
    if q:
        for w in q.split():
            where.append("(title LIKE ? OR body LIKE ?)")
            args += ["%%%s%%" % w, "%%%s%%" % w]
    sql = "SELECT * FROM ideas"
    if where:
        sql += " WHERE " + " AND ".join(where)
    case_sql, case_args = _board_order()
    sql += " ORDER BY " + case_sql + ", position, id"   # 11. tie-breaker
    args += case_args
    c = db.connect()
    try:
        return [_row(c, r) for r in c.execute(sql, args)]
    finally:
        c.close()


def get_idea(idea_id):
    c = db.connect()
    try:
        r = c.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
        if not r:
            raise NotFound("没有 #%s 这条创意" % idea_id)
        d = _row(c, r)
        d["events"] = [dict(e) for e in c.execute(
            "SELECT * FROM events WHERE idea_id=? ORDER BY id", (idea_id,))]
        return d
    finally:
        c.close()


def update_idea(idea_id, fields, actor="human"):
    if not fields:
        raise Invalid("没有可更新的字段")
    bad = [k for k in fields if k not in FIELDS]
    if bad:
        raise Invalid("不支持的字段：%s（状态请用 /advance）" % "、".join(bad))
    for k, v in fields.items():
        # position 是 REAL、tags 由 _parse_tags 独家处理，这俩不走文本校验
        if k not in ("position", "tags"):
            _text(v, _FIELD_LABEL.get(k, k))
    if "tags" in fields:
        fields = dict(fields, tags=_parse_tags(fields["tags"]))
    c = db.connect()
    try:
        if not c.execute("SELECT 1 FROM ideas WHERE id=?", (idea_id,)).fetchone():
            raise NotFound("没有 #%s 这条创意" % idea_id)
        sets = ",".join("%s=?" % k for k in fields)
        c.execute("UPDATE ideas SET %s WHERE id=?" % sets,
                  list(fields.values()) + [idea_id])
        add_event(c, idea_id, "edited", actor, "修改了：" + "、".join(fields))
        c.commit()
    finally:
        c.close()
    return get_idea(idea_id)


def advance(idea_id, to, note=None, position=None, actor="agent"):
    if to not in db.STATUS_KEYS:
        raise Invalid("未知状态：%s（合法值：%s）" % (to, "、".join(db.STATUS_KEYS)))
    note = "" if note is None else _text(note, "备注")   # note=None 是合法缺省，不是错
    c = db.connect()
    try:
        r = c.execute("SELECT status FROM ideas WHERE id=?", (idea_id,)).fetchone()
        if not r:
            raise NotFound("没有 #%s 这条创意" % idea_id)
        frm = r["status"]
        if frm == to:
            raise Invalid("该创意已经处于此状态")
        ts = db.now_iso()
        done_at = ts if to == "done" else None
        pos = _next_position(c, to) if position is None else position
        c.execute("UPDATE ideas SET status=?, position=?, done_at=? WHERE id=?",
                  (to, pos, done_at, idea_id))
        add_event(c, idea_id, "status", actor, note, frm, to)
        c.commit()
    finally:
        c.close()
    return get_idea(idea_id)


def add_note(idea_id, message, actor="agent"):
    c = db.connect()
    try:
        if not c.execute("SELECT 1 FROM ideas WHERE id=?", (idea_id,)).fetchone():
            raise NotFound("没有 #%s 这条创意" % idea_id)
        add_event(c, idea_id, "note", actor, message)
        c.commit()
    finally:
        c.close()
    return get_idea(idea_id)


def block(idea_id, reason, actor="agent"):
    """允许重复：覆盖 note，每次都留痕（SPEC 5.6）。"""
    reason = _text(reason, "阻塞原因")   # 必须在 UPDATE 之前挡，否则撞 NOT NULL
    c = db.connect()
    try:
        if not c.execute("SELECT 1 FROM ideas WHERE id=?", (idea_id,)).fetchone():
            raise NotFound("没有 #%s 这条创意" % idea_id)
        c.execute("UPDATE ideas SET blocked=1, blocked_note=? WHERE id=?", (reason, idea_id))
        add_event(c, idea_id, "blocked", actor, reason)
        c.commit()
    finally:
        c.close()
    return get_idea(idea_id)


def unblock(idea_id, actor="agent"):
    """保留 blocked_note，历史以 events 为准（SPEC 5.6）。"""
    c = db.connect()
    try:
        if not c.execute("SELECT 1 FROM ideas WHERE id=?", (idea_id,)).fetchone():
            raise NotFound("没有 #%s 这条创意" % idea_id)
        c.execute("UPDATE ideas SET blocked=0 WHERE id=?", (idea_id,))
        add_event(c, idea_id, "unblocked", actor, "")
        c.commit()
    finally:
        c.close()
    return get_idea(idea_id)


def delete_idea(idea_id):
    c = db.connect()
    try:
        n = c.execute("DELETE FROM ideas WHERE id=?", (idea_id,)).rowcount
        c.commit()
        if not n:
            raise NotFound("没有 #%s 这条创意" % idea_id)
        return True
    finally:
        c.close()


def stats():
    c = db.connect()
    try:
        counts = {k: 0 for k in db.STATUS_KEYS}
        for r in c.execute("SELECT status, COUNT(*) n FROM ideas GROUP BY status"):
            counts[r["status"]] = r["n"]
        blocked = c.execute("SELECT COUNT(*) FROM ideas WHERE blocked=1").fetchone()[0]
        return {"counts": counts, "blocked": blocked}
    finally:
        c.close()