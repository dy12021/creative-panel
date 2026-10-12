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

# SPEC 4.2 的浮点耗尽阈值。落点与同列相邻卡片的 position 差值**严格小于**它就触发整列
# 重排（措辞与 SPEC 一致：不是小于等于）。
POSITION_EPSILON = 1e-10

# create 路径上待插入的卡还没有 id。AUTOINCREMENT 的 id 单调且永不复用，所以拿一个
# 比任何现有 id 都大的哨兵去比大小即可——同位置的既有卡必然排在它前面。
_PENDING_ID = 1 << 62


_FIELD_LABEL = {"title": "标题", "body": "描述", "source": "来源",
                "tags": "标签", "blocked_note": "阻塞备注", "position": "排序位置"}


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


def _num(v, what):
    """position 是 REAL 列：非数字会被 SQLite 静默存成 TEXT。

    而 TEXT 在 ORDER BY 里排在 REAL 之后，列内排序就此失真——实测一张
    position='abc' 的卡片会被顶到该列所有数字卡片之后。bool 是 int 的子类，
    position=True 会被当成 1，一并拒掉。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Invalid("%s 必须是数字" % what)
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


def _renumber(c, status, exclude_id):
    """按 ORDER BY position, id 把该列 position 重写为 1,2,3,…（SPEC 4.2）。

    exclude_id 是正在移动的那张卡：它不在这一列的最终序列里（跨列时压根还没进来，
    列内时它带着旧 position 待改写），不排除就会被写两次 position，落点又被冲掉——
    这正是"新插入的卡必须落在正确位置"要防的事。

    两处刻意的克制：
    - 只重写**这一列**（WHERE status=?）。连坐别的列等于把无关卡片的位置也动了。
    - 不写 edited event：SPEC 4.4 的 kind 里没有"重排"，凭空造一条会污染时间线。

    已知副作用：这一列被重排的卡 updated_at 会跳一下。trg_ideas_touch 的 WHEN 条件是
    `NEW.updated_at = OLD.updated_at`——想压掉它就得让两者不等，而"不等"本身就意味着
    updated_at 变了，绕不开。改 db.py 不在本任务范围内，且 SPEC 没要求重排不动时间戳。

    返回 {id: 新 position}，调用方据此给移动中的卡重新落位。
    """
    rows = c.execute(
        "SELECT id FROM ideas WHERE status=? AND id<>? ORDER BY position, id",
        (status, exclude_id)).fetchall()
    newpos = {}
    for i, r in enumerate(rows, 1):
        c.execute("UPDATE ideas SET position=? WHERE id=?", (i, r["id"]))
        newpos[r["id"]] = i
    return newpos


def _settle_position(c, status, pos, idea_id=None):
    """SPEC 4.2 的浮点耗尽重排，返回最终落点。

    触发条件：落点与**相邻**卡片（同列排序键的前一张和后一张）的 position 差值
    严格小于 POSITION_EPSILON。与 SPEC 措辞一致：小于，不是小于等于。

    为什么放服务端而不是前端（SPEC 4.2 说"position 由前端计算，服务端只存不校验"）：
    重排不是**校验**是**维护动作**。放服务端才绕不过去——create / PATCH / advance
    三条写 position 的路径全部经过这一个函数；放前端等于每个写入点抄一遍，漏一处
    就失效，而漏掉的那处恰好不会报错，只是排序悄悄变怪。

    相邻取 (position, id) 两段键，和列内读数口径一致，否则 id tie-breaker 参与
    比较的时刻与实际排序对不上。
    """
    exclude = -1 if idea_id is None else idea_id
    key = _PENDING_ID if idea_id is None else idea_id
    rows = c.execute(
        "SELECT id, position FROM ideas WHERE status=? AND id<>?"
        " ORDER BY position, id", (status, exclude)).fetchall()
    if not rows:
        return pos                                   # 空列无从耗尽
    prev = nxt = None
    for r in rows:
        if (r["position"], r["id"]) < (pos, key):
            prev = r
        elif nxt is None:
            nxt = r
    gaps = [abs(pos - r["position"]) for r in (prev, nxt) if r is not None]
    if not gaps or min(gaps) >= POSITION_EPSILON:
        return pos                                   # 精度还有富余，不动这一列
    newpos = _renumber(c, status, exclude)
    if prev is not None and nxt is not None:
        return (newpos[prev["id"]] + newpos[nxt["id"]]) / 2.0
    if prev is not None:
        return float(newpos[prev["id"]] + 1)
    return float(newpos[nxt["id"]] - 1)


def create_idea(title, body="", tags=None, source="", actor="human", position=None):
    if title is None:              # null 视同"没给标题"，走 SPEC 错误表那条文案
        raise Invalid("标题不能为空")
    title = _text(title, "标题")   # 非字符串在这里挡，否则下面 .strip() 抛 AttributeError
    if not title.strip():
        raise Invalid("标题不能为空")
    body = _text(body, "描述")
    source = _text(source, "来源")
    tags_v = _parse_tags(tags)
    if position is not None:
        position = _num(position, _FIELD_LABEL["position"])
    c = db.connect()
    try:
        # 新卡必落在 idea 列；position 缺省是 MAX+1（永不含糊），显式传入才可能
        # 是前端算的落点。两条都过 _settle_position，差值耗尽时整列重排。
        pos = _settle_position(c, "idea",
                                _next_position(c, "idea") if position is None
                                else position)
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
        # 不校验的话 tag=5 会拼成 "%,5,%" 静默返回空结果——无异常、无提示
        where.append("tags LIKE ?"); args.append("%%,%s,%%" % _text(tag, "标签筛选"))
    if blocked is not None:
        where.append("blocked=?"); args.append(1 if blocked in (1, "1", True) else 0)
    if q is not None:
        q = _text(q, "搜索词")          # 否则 q=5 会在 q.split() 抛 AttributeError
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
    # null 守卫覆盖全字段、无例外，且独立于下面的类型分档（SPEC 5.5）。
    # tags/position 同样是 NOT NULL：放进类型循环里跳过它们等于开了两个洞——
    # {"tags": null} 会静默清空标签并写下"修改了 tags"的 event（账本说谎），
    # {"position": null} 则直接漏 IntegrityError。类型分档只对非 null 值生效。
    nulls = [k for k, v in fields.items() if v is None]
    if nulls:
        raise Invalid("%s 不能为 null，请传空串" % _FIELD_LABEL.get(nulls[0], nulls[0]))
    for k, v in fields.items():
        if k == "position":
            _num(v, _FIELD_LABEL["position"])      # REAL 列，不是文本字段
        elif k != "tags":
            _text(v, _FIELD_LABEL.get(k, k))      # tags 由 _parse_tags 独家处理
    if "tags" in fields:
        fields = dict(fields, tags=_parse_tags(fields["tags"]))
    c = db.connect()
    try:
        row = c.execute("SELECT status FROM ideas WHERE id=?", (idea_id,)).fetchone()
        if not row:
            raise NotFound("没有 #%s 这条创意" % idea_id)
        if "position" in fields:
            # 列内移动：卡带着旧 position 在这一列里，必须把自己排除在重排之外
            fields = dict(fields, position=_settle_position(
                c, row["status"], fields["position"], idea_id))
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
    if position is not None:
        position = _num(position, _FIELD_LABEL["position"])
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
        # 跨列移动：目标列里还没有这张卡，exclude 让重排不会把它写两次
        pos = _settle_position(c, to,
                               _next_position(c, to) if position is None
                               else position, idea_id)
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