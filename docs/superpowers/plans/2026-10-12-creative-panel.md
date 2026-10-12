# 创意面板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个本机创意面板：网页五列看板记录「创意」与「完成状态」，agent 通过随项目附带的 `creative-panel` skill 与 `panel.py` CLI 读写同一份数据。

**Architecture:** FastAPI + SQLite 单进程本机服务；原生 JS 单页看板拖拽调用同一套 REST API；`panel.py` CLI 是 API 薄封装，输出人话而非 JSON。SQLite 是唯一真相源，两端共用。

**Tech Stack:** Python 3.13.9 / FastAPI 0.136 / uvicorn 0.49 / SQLite(标准库) / httpx / pytest / 原生 JS + HTML5 Drag & Drop（零 CDN、零构建）

**设计依据:** `docs/superpowers/specs/2026-10-12-creative-panel-design.md`（下称 SPEC）。冲突时以 SPEC 为准，本计划不重新设计。

---

## Global Constraints

以下约束适用于每一个任务，不再逐任务重复：

- **端口 8472**；监听 `127.0.0.1`
- **数据库路径**：`db.py` 读环境变量 `PANEL_DB`，缺省 `<项目根>/data/panel.db`
- **建连必设 PRAGMA**：`foreign_keys=ON`（每连接）、`recursive_triggers=OFF`（每连接，**硬性前提**）、`journal_mode=WAL`（文件属性，首次建库设一次即可）
- **状态只能走 `/advance`**：`PATCH` 请求体出现 `status` 必须 422
- **`PATCH` 白名单**：`title` `body` `tags` `source` `blocked_note` `position`；出现其他键（含 `status`/`blocked`/`id`）一律 422
- **任一字段传 `null`** → 422；清空请传 `""`
- **`advance` 到当前状态** → 422
- **排序键**：`ORDER BY <按看板列序的 status>, position, id`。`status` 用 `CASE` 按 `idea→clarifying→clarified→building→done` 排（不用字典序，否则 CLI `list` 输出会乱），`CASE` 取值从 `db.STATUS_KEYS` 生成不硬编码；`id` 是 tie-breaker
- **tags 逗号语义**：逗号是分隔符，"标签含逗号"不可表达，不为它写校验分支
- **tags 存储形式**：首尾补逗号，如 `",视频,科普,"`；标签禁止含逗号；未提供 → `","`；提供了但规范化后全空 → 422
- **`position` 由前端计算，服务端只存不校验**；新建缺省 `COALESCE(MAX(position),0)+1`
- **时间戳**：`strftime('%Y-%m-%dT%H:%M:%f','now','localtime')`，毫秒、无时区
- **`actor`**：`X-Panel-Actor` 头，只接受 `human`/`agent`/`system`，**其他值忽略记 `human`，不报错**
- **所有前端资源本地化**，不引用任何 CDN
- **提交信息用中文**，格式 `feat:` / `fix:` / `test:` / `docs:`
- 提交前跑 `python -m pytest test_panel.py -q`，必须全绿

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `server/db.py` | schema、三条 PRAGMA、`updated_at` 触发器、tags 规范化、时间戳、状态常量 |
| `server/store.py` | 11 个数据访问函数 + `add_event`（纯数据层，不碰 HTTP） |
| `server/app.py` | FastAPI 路由、请求校验、422 文案、`X-Panel-Actor` 解析 |
| `skill/panel.py` | CLI，agent 的入口 |
| `skill/SKILL.md` | `creative-panel` 技能说明 |
| `web/index.html` | 页面骨架（看板容器、抽屉、工具条） |
| `web/style.css` | 设计令牌 + 全部样式 |
| `web/app.js` | 取数、渲染、拖拽、抽屉、流转轨 |
| `start.bat` | 双击启动 |
| `install_skill.bat` | 单向同步 skill 到 `%USERPROFILE%\.minimax\skills\` |
| `test_panel.py` | 后端 + CLI 全部回归测试（SPEC 第 11 节 18 条） |
| `README.md` | 怎么启动、怎么用 |

**为什么多一个 `store.py`**：SPEC 第 10 节列的是 `app.py` + `db.py`。此处拆出 `store.py` 是为了让 `db.py` 只承载"容易踩坑的规则"（PRAGMA、触发器、规范化），保持小而稳定；11 个 CRUD 函数放 `store.py` 便于独立测试。这不改变任何对外行为。

---

### Task 1: 数据库层与存储约定

**Files:**
- Create: `server/db.py`
- Create: `test_panel.py`
- Test: `test_panel.py`

**Interfaces:**
- Consumes: 无（起点）
- Produces:
  - `STATUS_KEYS: list[str]` = `["idea","clarifying","clarified","building","done"]`
  - `STATUS_LABEL: dict[str,str]` 状态 key → 中文显示名
  - `STATUS_INDEX: dict[str,int]` 状态 key → 0..4
  - `ZERO_TAG: str` = `","`
  - `db_path() -> Path`
  - `connect() -> sqlite3.Connection`（每次新连接，三条 PRAGMA 已设）
  - `init_db() -> None`（建表建索引建触发器，幂等）
  - `now_iso() -> str`
  - `normalize_tags(raw: str) -> str`
  - `parse_tags(raw: str | None) -> str`（`None` → `ZERO_TAG`；规范化后全空 → `ValueError`）

- [ ] **Step 1: 写失败测试**

创建 `test_panel.py`：

```python
"""创意面板后端回归测试。设计依据 SPEC 第 11 节。"""
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest test_panel.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: 写 `server/db.py`**

```python
"""数据库连接、schema 与存储层约定。

设计依据 SPEC 第 4 节。本文件只放"容易踩坑的规则"，不放业务查询。
"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STATUSES = [
    ("idea", "仅有创意"),
    ("clarifying", "正在明确"),
    ("clarified", "已经明确"),
    ("building", "正在制作"),
    ("done", "完成成品"),
]
STATUS_KEYS = [k for k, _ in STATUSES]
STATUS_LABEL = dict(STATUSES)
STATUS_INDEX = {k: i for i, k in enumerate(STATUS_KEYS)}

ZERO_TAG = ","

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  body         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'idea',
  blocked      INTEGER NOT NULL DEFAULT 0,
  blocked_note TEXT NOT NULL DEFAULT '',
  tags         TEXT NOT NULL DEFAULT ',',
  source       TEXT NOT NULL DEFAULT '',
  position     REAL NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  done_at      TEXT,
  CHECK (status IN ('idea','clarifying','clarified','building','done'))
);
CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id     INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  actor       TEXT NOT NULL,
  message     TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  CHECK (kind  IN ('created','status','note','blocked','unblocked','edited')),
  CHECK (actor IN ('human','agent','system'))
);
CREATE INDEX IF NOT EXISTS idx_events_idea ON events(idea_id, id);

-- 自动维护 updated_at。WHEN 子句在承重：去掉它连跨秒场景也会递归
-- （见 SPEC 4.3.1 实测矩阵）。recursive_triggers=OFF 是硬性前提。
CREATE TRIGGER IF NOT EXISTS trg_ideas_touch AFTER UPDATE ON ideas
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE ideas SET updated_at = strftime('%Y-%m-%dT%H:%M:%f','now','localtime')
  WHERE id = NEW.id;
END;
"""


def db_path():
    """SPEC 4.2：读 PANEL_DB，缺省 <项目根>/data/panel.db。"""
    env = os.environ.get("PANEL_DB")
    return Path(env) if env else (ROOT / "data" / "panel.db")


def connect():
    """每次调用返回新连接，三条 PRAGMA 已就位。"""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")        # 每连接：不开则级联删除失效
    c.execute("PRAGMA recursive_triggers=OFF")  # 每连接：不开则 touch 触发器递归
    c.execute("PRAGMA journal_mode=WAL")       # 文件属性：建库时设一次即可
    return c


def init_db():
    connect().executescript(SCHEMA)


def now_iso():
    """毫秒精度、无时区（SPEC 4.3）。"""
    c = connect()
    try:
        return c.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%f','now','localtime')").fetchone()[0]
    finally:
        c.close()


def normalize_tags(raw: str) -> str:
    """切分 -> strip -> 丢空 -> 去重 -> 首尾补逗号。"""
    seen, out = set(), []
    for part in raw.split(","):
        t = part.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return ("," + ",".join(out) + ",") if out else ZERO_TAG


def parse_tags(raw) -> str:
    """None 表示未提供；提供了但规范化后全空则报错（SPEC 4.2）。"""
    if raw is None:
        return ZERO_TAG
    result = normalize_tags(raw)
    if result == ZERO_TAG:
        raise ValueError("标签不能为空")
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest test_panel.py -q`
Expected: PASS（全部）

若 `test_check_constraints` 未如预期失败，检查第二个 `with pytest.raises` 是否因第一个连接已失效——必要时把两条拆成独立连接。

- [ ] **Step 5: 提交**

```bash
git add server/db.py test_panel.py
git commit -m "feat: 数据库层——schema、三条 PRAGMA、tags 规范化、时间戳"
```

---

### Task 2: 数据访问层 store.py

**Files:**
- Create: `server/store.py`
- Modify: `test_panel.py`
- Test: `test_panel.py`

**Interfaces:**
- Consumes: Task 1 的 `db.STATUS_KEYS` `db.STATUS_LABEL` `db.now_iso()` `db.parse_tags()` `db.connect()`
- Produces（Task 3 全部依赖这些签名）：
  - `create_idea(title, body="", tags=None, source="", actor="human", position=None) -> dict`
  - `list_ideas(status=None, tag=None, q=None, blocked=None) -> list[dict]`
  - `get_idea(idea_id) -> dict | None`（含 `events` 键）
  - `update_idea(idea_id, fields: dict, actor="human") -> dict`  # fields 只含白名单键
  - `advance(idea_id, to, note=None, position=None, actor="agent") -> dict`
  - `add_note(idea_id, message, actor="agent") -> dict`
  - `block(idea_id, reason, actor="agent") -> dict`
  - `unblock(idea_id, actor="agent") -> dict`
  - `delete_idea(idea_id) -> bool`
  - `stats() -> dict`
  - `NotFound(Exception)` / `Invalid(Exception)`（`Invalid` 带人话 `detail`）
  - 所有返回的 idea 都是 `dict`，含 `id,title,body,status,blocked,blocked_note,tags,source,position,created_at,updated_at,done_at`

- [ ] **Step 1: 写失败测试**

追加到 `test_panel.py`：

```python
from store import (NotFound, Invalid, advance, add_note, block, create_idea,
                   delete_idea, get_idea, list_ideas, stats, unblock, update_idea)


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
    update_idea(a["id"], {"position": 99.0})
    assert list_ideas(status="idea")[0]["id"] == a["id"]
    assert [x["id"] for x in list_ideas(status="idea")][1:] == [c["id"], e["id"]]


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest test_panel.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'store'`

- [ ] **Step 3: 写 `server/store.py`**

> ⚠️ **Task 2 已完成并关闭。下面这段代码块是当初的参考实现，是历史快照，不是当前规格。**
> 实际交付经过 3 轮修复（null 守卫、类型分档、CASE 列序），已与本块**不一致**——
> 例如本块的 `list_ideas` 仍写着 `"%,%s,%%"`，那正是第一轮修掉的必崩 bug。
> **以仓库里已提交的 `server/store.py` 为准**，并以 SPEC 第 4/5 节为契约。
> 本块仅供追溯"当初打算怎么做"，**不要照抄**。

```python
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


def _row(c, r):
    return {k: r[k] for k in r.keys() if k != "events"}


def add_event(c, idea_id, kind, actor, message="", from_status=None, to_status=None):
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
    if not (title or "").strip():
        raise Invalid("标题不能为空")
    try:
        tags_v = db.parse_tags(tags)
    except ValueError as e:
        raise Invalid(str(e))
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
        where.append("tags LIKE ?"); args.append("%,%s,%%" % tag)
    if blocked is not None:
        where.append("blocked=?"); args.append(1 if blocked in (1, "1", True) else 0)
    if q:
        for w in q.split():
            where.append("(title LIKE ? OR body LIKE ?)")
            args += ["%%%s%%" % w, "%%%s%%" % w]
    sql = "SELECT * FROM ideas"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # status 按看板列序（CASE），不用字典序；id 是 tie-breaker
    order_case = " ".join("WHEN ? THEN ?" for _ in db.STATUS_KEYS)
    sql += (" ORDER BY CASE status %s END, position, id" % order_case)
    sql_params = args + [x for i, k in enumerate(db.STATUS_KEYS) for x in (k, i)]
    c = db.connect()
    try:
        return [_row(c, r) for r in c.execute(sql, sql_params)]
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
    # null 守卫全字段无例外（含 tags/position），独立于下面的类型分档
    for k, v in fields.items():
        if v is None:
            raise Invalid("%s 不能为 null，请传空串" % _FIELD_LABEL.get(k, k))
    # 类型分档只对已确认非 null 的值生效
    for k, v in fields.items():
        if k == "position":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise Invalid("排序位置 必须是数字")
        elif not isinstance(v, str):
            raise Invalid("%s 必须是字符串" % _FIELD_LABEL.get(k, k))
    if "tags" in fields:
        try:
            fields = dict(fields, tags=db.parse_tags(fields["tags"]))
        except ValueError as e:
            raise Invalid(str(e))
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
        add_event(c, idea_id, "status", actor, note or "", frm, to)
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest test_panel.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/store.py test_panel.py
git commit -m "feat: 数据访问层——11 个 CRUD + events 审计流"
```

---

### Task 3: FastAPI 路由与校验

**Files:**
- Create: `server/app.py`
- Modify: `test_panel.py`
- Test: `test_panel.py`

**Interfaces:**
- Consumes: Task 2 的全部 store 函数、`db.STATUS_*`
- Produces:
  - `VERSION: str` = `"0.1.0"`
  - `app`（FastAPI 实例），`create_app()` 便于测试
  - 全部 REST 端点，见 SPEC 第 5 节
  - 异常处理器：`NotFound` → 404，`Invalid` → 422，均以 `{"detail": <人话>}` 返回
  - `resolve_actor(request) -> str`，非法值落 `human`

- [ ] **Step 1: 写失败测试**

在 `test_panel.py` 追加（用 `fastapi.testclient.TestClient`）：

```python
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "c.db"))
    appm = importlib.import_module("app")
    return TestClient(appm.create_app())


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_patch_rejects_status_and_unknown_fields(client):
    """2/3. 白名单外的键一律 422。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()
    assert client.patch("/api/ideas/%d" % i["id"], json={"status": "done"}).status_code == 422
    assert client.patch("/api/ideas/%d" % i["id"], json={"blocked": 1}).status_code == 422
    assert client.patch("/api/ideas/%d" % i["id"], json={}).status_code == 422


def test_patch_null_rejected(client):
    """16."""
    i = client.post("/api/ideas", json={"title": "a"}).json()
    assert client.patch("/api/ideas/%d" % i["id"], json={"body": None}).status_code == 422


def test_advance_same_status_422(client):
    i = client.post("/api/ideas", json={"title": "a"}).json()
    r = client.post("/api/ideas/%d/advance" % i["id"], json={"to": "idea"})
    assert r.status_code == 422 and "已经处于" in r.json()["detail"]


def test_advance_with_position_single_call(client):
    """跨列拖拽一次调用同时改状态和位置。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()
    r = client.post("/api/ideas/%d/advance" % i["id"],
                    json={"to": "building", "position": 3.5, "note": "开工"})
    assert r.status_code == 200
    assert r.json()["status"] == "building" and r.json()["position"] == 3.5


def test_actor_header_rules(client):
    """17. 非法 actor 值忽略记 human，不报错。"""
    i = client.post("/api/ideas", json={"title": "a"}).json()
    client.post("/api/ideas/%d/note" % i["id"], json={"message": "人写的"},
                headers={"X-Panel-Actor": "robot"})
    client.post("/api/ideas/%d/note" % i["id"], json={"message": "agent 写的"},
                headers={"X-Panel-Actor": "agent"})
    evs = client.get("/api/ideas/%d" % i["id"]).json()["events"]
    actors = {e["actor"] for e in evs if e["kind"] == "note"}
    assert actors == {"human", "agent"}


def test_tags_rules(client):
    """10/18. 未提供 -> ","；全空 -> 422。"""
    assert client.post("/api/ideas", json={"title": "a"}).json()["tags"] == ","
    assert client.post("/api/ideas", json={"title": "a", "tags": "  , "}).status_code == 422
    ok = client.post("/api/ideas", json={"title": "a", "tags": "视频, 科普"}).json()
    assert ok["tags"] == ",视频,科普,"


def test_blank_title_422(client):
    assert client.post("/api/ideas", json={"title": "  "}).status_code == 422


def test_404_detail(client):
    assert client.get("/api/ideas/999").status_code == 404


def test_stats_endpoint(client):
    client.post("/api/ideas", json={"title": "a"})
    s = client.get("/api/stats").json()
    assert s["counts"]["idea"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest test_panel.py -q -k client`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: 写 `server/app.py`**

```python
"""FastAPI 路由。设计依据 SPEC 第 5 节。"""
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db          # noqa: E402
import store       # noqa: E402

VERSION = "0.1.0"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ACTORS = ("human", "agent", "system")


def resolve_actor(request: Request) -> str:
    """SPEC 5.4：只接受三个值，非法值忽略按 human 处理，不报错。"""
    v = request.headers.get("X-Panel-Actor")
    return v if v in ACTORS else "human"


def create_app():
    db.init_db()
    app = FastAPI(title="创意面板", version=VERSION)

    @app.exception_handler(store.NotFound)
    async def _nf(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(store.Invalid)
    async def _iv(request, exc):
        return JSONResponse(status_code=422, content={"detail": exc.detail})

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION}

    @app.get("/api/ideas")
    def ideas(request: Request, status=None, tag=None, q=None, blocked=None):
        return store.list_ideas(status=status, tag=tag, q=q, blocked=blocked)

    @app.post("/api/ideas", status_code=201)
    def create(body: dict, request: Request):
        return store.create_idea(
            title=body.get("title", ""), body=body.get("body") or "",
            tags=body.get("tags"), source=body.get("source") or "",
            actor=resolve_actor(request))

    @app.get("/api/ideas/{idea_id}")
    def one(idea_id: int):
        return store.get_idea(idea_id)

    @app.patch("/api/ideas/{idea_id}")
    def patch(idea_id: int, body: dict, request: Request):
        if "status" in body:
            raise store.Invalid("请使用 /advance 接口变更状态")
        return store.update_idea(idea_id, body, actor=resolve_actor(request))

    @app.post("/api/ideas/{idea_id}/advance")
    def adv(idea_id: int, body: dict, request: Request):
        return store.advance(idea_id, body.get("to"), note=body.get("note"),
                             position=body.get("position"),
                             actor=resolve_actor(request))

    @app.post("/api/ideas/{idea_id}/note")
    def note(idea_id: int, body: dict, request: Request):
        return store.add_note(idea_id, body.get("message", ""),
                              actor=resolve_actor(request))

    @app.post("/api/ideas/{idea_id}/block")
    def blk(idea_id: int, body: dict, request: Request):
        return store.block(idea_id, body.get("reason", ""),
                           actor=resolve_actor(request))

    @app.post("/api/ideas/{idea_id}/unblock")
    def unblk(idea_id: int, request: Request):
        return store.unblock(idea_id, actor=resolve_actor(request))

    @app.delete("/api/ideas/{idea_id}")
    def dele(idea_id: int):
        store.delete_idea(idea_id)
        return {"ok": True}

    @app.get("/api/stats")
    def stats():
        return store.stats()

    # web/ 由 Task 5 创建；此前不挂载，避免 StaticFiles 因目录不存在而报错
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


app = create_app()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest test_panel.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/app.py test_panel.py
git commit -m "feat: FastAPI 路由与校验——PATCH 白名单、422 文案、actor 解析"
```

---

### Task 4: panel.py CLI

**Files:**
- Create: `skill/panel.py`
- Modify: `test_panel.py`
- Test: `test_panel.py`

**Interfaces:**
- Consumes: Task 3 的 REST API
- Produces: 可执行 CLI，9 个子命令 `new/list/show/advance/note/block/unblock/stats/delete`
  - `BASE_URL` 由环境变量 `PANEL_URL` 覆盖，缺省 `http://127.0.0.1:8472`
  - 固定带 `X-Panel-Actor: agent`
  - 退出码：0 成功；1 服务未启动 / HTTP 错误
  - 输出中文人话，不输出 JSON

- [ ] **Step 1: 写失败测试**

在 `test_panel.py` 追加：

```python
import subprocess
import threading

import uvicorn


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    """真起 uvicorn，随机空闲端口，CLI 走真实 HTTP。"""
    import socket
    sock = socket.socket(); sock.bind(("", 0))
    port = sock.getsockname()[1]; sock.close()
    monkeypatch.setenv("PANEL_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("PANEL_URL", "http://127.0.0.1:%d" % port)
    appm = importlib.import_module("app")
    cfg = uvicorn.Config(appm.create_app(), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started:
            break
        __import__("time").sleep(0.05)
    yield "http://127.0.0.1:%d" % port
    srv.should_exit = True; t.join(timeout=5)


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(Path(__file__).parent / "skill" / "panel.py"), *args],
        capture_output=True, text=True, encoding="utf-8")


def test_cli_full_cycle(live_server):
    r = _cli("new", "AI 发展史", "--tags", "视频,科普")
    assert r.returncode == 0 and "已创建 #" in r.stdout
    iid = r.stdout.split("#")[1].split("「")[0]
    r = _cli("advance", iid, "正在明确", "--note", "开始聊需求")
    assert r.returncode == 0 and "正在明确" in r.stdout
    r = _cli("advance", iid, "clarifying")
    assert r.returncode == 1 and "已经处于" in r.stdout      # 4.
    r = _cli("list", "--status", "clarifying")
    assert "AI 发展史" in r.stdout
    r = _cli("stats")
    assert "clarifying" in r.stdout
    r = _cli("show", iid)
    assert "开始聊需求" in r.stdout
    r = _cli("block", iid, "等授权")
    assert "已阻塞" in r.stdout
    r = _cli("unblock", iid)
    assert "已解除阻塞" in r.stdout
    r = _cli("delete", iid)
    assert r.returncode == 0


def test_cli_bad_id(live_server):
    r = _cli("show", "999")
    assert r.returncode == 1 and "没有 #999" in r.stdout


def test_cli_server_down(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_URL", "http://127.0.0.1:1")
    r = _cli("stats")
    assert r.returncode == 1 and "面板未启动" in r.stdout
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest test_panel.py -q -k cli`
Expected: FAIL —— 文件不存在

- [ ] **Step 3: 写 `skill/panel.py`**

```python
"""创意面板 CLI —— agent 的操作入口。

设计依据 SPEC 第 6 节。输出给人看，不输出 JSON。
"""
import argparse
import os
import sys

import httpx

BASE_URL = os.environ.get("PANEL_URL", "http://127.0.0.1:8472").rstrip("/")
HEADERS = {"X-Panel-Actor": "agent"}
LABEL = {"idea": "仅有创意", "clarifying": "正在明确", "clarified": "已经明确",
         "building": "正在制作", "done": "完成成品"}


def _to_key(text):
    for k, v in LABEL.items():
        if text == k or text == v:
            return k
    return text


class Fail(Exception):
    pass


def call(method, path, **kw):
    try:
        r = httpx.request(method, BASE_URL + path, headers=HEADERS, timeout=10, **kw)
    except httpx.RequestError:
        raise Fail("面板未启动，请双击 start.bat")
    if r.status_code >= 400:
        detail = r.json().get("detail", "未知错误")
        if r.status_code == 404:
            raise Fail(detail)
        raise Fail(detail)
    return r.json()


def main(argv=None):
    p = argparse.ArgumentParser(description="创意面板 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new");      n.add_argument("title")
    n.add_argument("--body", default=""); n.add_argument("--tags")
    n.add_argument("--source", default="")
    l = sub.add_parser("list")
    l.add_argument("--status"); l.add_argument("--tag"); l.add_argument("--q")
    l.add_argument("--blocked", action="store_true")
    s = sub.add_parser("show");     s.add_argument("id")
    a = sub.add_parser("advance");  a.add_argument("id"); a.add_argument("status")
    a.add_argument("--note", default="")
    nt = sub.add_parser("note");    nt.add_argument("id"); nt.add_argument("message")
    b = sub.add_parser("block");    b.add_argument("id"); b.add_argument("reason")
    sub.add_parser("unblock").add_argument("id")
    sub.add_parser("stats")
    sub.add_parser("delete").add_argument("id")

    args = p.parse_args(argv)
    try:
        return dispatch(args)
    except Fail as e:
        print(str(e))
        return 1


def dispatch(a):
    c = a.cmd
    if c == "new":
        d = call("POST", "/api/ideas", json={"title": a.title, "body": a.body,
                                             "tags": a.tags, "source": a.source})
        print("✔ 已创建 #%d「%s」  状态=%s" % (d["id"], d["title"], LABEL[d["status"]]))
    elif c == "list":
        items = call("GET", "/api/ideas", params={
            "status": _to_key(a.status) if a.status else None,
            "tag": a.tag, "q": a.q, "blocked": "1" if a.blocked else None})
        if not items:
            print("（没有符合条件的创意）")
        for d in items:
            mark = "▣" if d["blocked"] else " "
            print("#%-4d %s [%s] %s" % (d["id"], mark, LABEL[d["status"]], d["title"]))
    elif c == "show":
        d = call("GET", "/api/ideas/%s" % a.id)
        print("#%d「%s」  状态=%s%s" % (d["id"], d["title"], LABEL[d["status"]],
                                        "  已阻塞：%s" % d["blocked_note"] if d["blocked"] else ""))
        if d["tags"] != ",":
            print("  标签：%s" % "、".join(t for t in d["tags"].strip(",").split(",")))
        if d["body"]:
            print("  描述：%s" % d["body"].replace("\n", "\n        "))
        print("  ── 时间线 ──")
        for e in d["events"]:
            who = {"human": "你", "agent": "agent"}.get(e["actor"], e["actor"])
            if e["kind"] == "status":
                print("  %s [%s] %s → %s %s" % (e["created_at"][5:16], who,
                        LABEL[e["from_status"]], LABEL[e["to_status"]], e["message"]))
            else:
                print("  %s [%s] %s %s" % (e["created_at"][5:16], who, e["kind"], e["message"]))
    elif c == "advance":
        d = call("POST", "/api/ideas/%s/advance" % a.id,
                 json={"to": _to_key(a.status), "note": a.note})
        print("✔ #%s 已推进，当前状态：%s" % (a.id, LABEL[d["status"]]))
        if a.note:
            print("    %s" % a.note)
    elif c == "note":
        call("POST", "/api/ideas/%s/note" % a.id, json={"message": a.message})
        print("✔ #%s 已加备注" % a.id)
    elif c == "block":
        call("POST", "/api/ideas/%s/block" % a.id, json={"reason": a.reason})
        print("✔ #%s 已阻塞：%s" % (a.id, a.reason))
    elif c == "unblock":
        call("POST", "/api/ideas/%s/unblock" % a.id)
        print("✔ #%s 已解除阻塞" % a.id)
    elif c == "stats":
        s = call("GET", "/api/stats")
        print("  " + " | ".join("%s %d" % (LABEL[k], s["counts"][k])
                                 for k in ("idea", "clarifying", "clarified", "building", "done")))
        print("  阻塞中：%d" % s["blocked"])
    elif c == "delete":
        call("DELETE", "/api/ideas/%s" % a.id)
        print("✔ 已删除 #%s" % a.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest test_panel.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add skill/panel.py test_panel.py
git commit -m "feat: panel.py CLI——agent 操作面板的入口"
```

---

### Task 5: 前端骨架与设计令牌

**Files:**
- Create: `web/index.html`
- Create: `web/style.css`
- Create: `web/app.js`
- Test: 手工（见验收步骤）+ `python -m pytest test_panel.py -q` 不回归

**Interfaces:**
- Consumes: Task 3 的 `GET /api/ideas`、`GET /api/stats`
- Produces:
  - `web/style.css` 中定义 CSS 变量：`--bg` `--panel` `--card` `--lift` `--border` `--fg` `--fg-dim`
  - `web/app.js` 暴露给 Task 6/7 的函数：`api(method, path, body)`、`state`、`render()`、`STATUS_LABEL`、`STATUS_ORDER`
  - DOM 结构：`.toolbar` `.board`（5 个 `.col`）`.drawer`

- [ ] **Step 1: 写 `web/index.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>创意面板</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
  <header class="toolbar">
    <h1>创意面板</h1>
    <input id="q" type="search" placeholder="搜索标题或描述" autocomplete="off">
    <select id="tag"><option value="">全部标签</option></select>
    <label class="toggle"><input id="only-blocked" type="checkbox"> 只看卡住的</label>
    <button id="new-btn" class="primary">＋ 记一个想法</button>
  </header>

  <main class="board" id="board"></main>

  <aside class="drawer" id="drawer" hidden>
    <div class="drawer-inner">
      <button class="drawer-close" id="drawer-close" aria-label="关闭">✕</button>
      <div id="drawer-body"></div>
    </div>
  </aside>

  <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 写 `web/style.css` 的令牌部分**

```css
/* 设计令牌：取自本机 SnowLuma 的打包 CSS，SPEC 8.1/8.2 */
:root{
  --bg:      oklch(13% .008 250);
  --panel:   oklch(15% .01  250);
  --card:    oklch(17% .012 250);
  --lift:    oklch(19% .012 250);
  --border:  oklch(20% .03  250 / .28);
  --fg:      oklch(98% .005 240);
  --fg-dim:  oklch(66% .02  240);

  --st-idea:       oklch(66% .02 240);
  --st-clarifying: oklch(75% .14 230);
  --st-clarified:  oklch(70% .18 293);
  --st-building:   oklch(80% .16 80);
  --st-done:       oklch(72% .15 160);
  --blocked:       oklch(62% .2  27);

  --r: 1rem;
  --shadow: 0 1px 2px rgba(0,0,0,.15);
  --font: system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  --mono: ui-monospace,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font-family:var(--font); font-size:.84rem; line-height:1.45;
  -webkit-font-smoothing:antialiased;
}
h1{font-size:1.05rem; letter-spacing:-.025em; margin:0}
button{font:inherit; cursor:pointer}
:focus-visible{outline:2px solid var(--st-clarified); outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important; animation:none!important}}
```

- [ ] **Step 3: 写 `web/app.js` 的数据层与看板渲染**

```javascript
// SPEC 8.4：统计数字直接做列头，不单占一行
const STATUS_ORDER = ["idea","clarifying","clarified","building","done"];
const STATUS_LABEL = {
  idea:"仅有创意", clarifying:"正在明确", clarified:"已经明确",
  building:"正在制作", done:"完成成品"
};

const state = { items:[], filter:{q:"", tag:"", blocked:false}, openId:null };

async function api(method, path, body){
  const opt = { method, headers:{} };
  if(body){ opt.headers["Content-Type"]="application/json"; opt.body=JSON.stringify(body); }
  const r = await fetch(path, opt);
  if(!r.ok){ const d = await r.json().catch(()=>({detail:"请求失败"})); throw new Error(d.detail); }
  return r.json();
}

async function load(){
  const p = new URLSearchParams();
  if(state.filter.q) p.set("q", state.filter.q);
  if(state.filter.tag) p.set("tag", state.filter.tag);
  if(state.filter.blocked) p.set("blocked","1");
  state.items = await api("GET", "/api/ideas?" + p);
  render();
}

function el(tag, cls, text){
  const n = document.createElement(tag);
  if(cls) n.className = cls;
  if(text != null) n.textContent = text;
  return n;
}

function render(){
  const board = document.getElementById("board");
  board.textContent = "";
  for(const st of STATUS_ORDER){
    const col = el("section","col"); col.dataset.status = st;
    const head = el("div","col-head");
    const items = state.items.filter(i => i.status === st);
    head.append(el("span","col-count", items.length));
    head.append(el("span","col-name", STATUS_LABEL[st]));
    col.append(head);

    const list = el("div","col-list");
    items.sort((a,b)=> a.position-b.position || a.id-b.id).forEach(i => list.append(card(i)));
    col.append(list);

    if(st === "idea"){
      const add = el("button","col-add","＋ 快捷登记");
      add.onclick = () => quickNew(col);
      col.append(add);
    }
    board.append(col);
  }
  renderTagFilter();
}

// 流转轨：Task 6 填充
function card(i){ return el("article","card", i.title); }

function renderTagFilter(){
  const sel = document.getElementById("tag");
  const tags = new Set();
  state.items.forEach(i => i.tags.split(",").filter(Boolean).forEach(t => tags.add(t)));
  const cur = sel.value;
  sel.textContent = "";
  sel.append(new Option("全部标签",""));
  [...tags].sort().forEach(t => sel.append(new Option(t,t)));
  sel.value = cur;
}

document.getElementById("q").oninput = e => { state.filter.q = e.target.value.trim(); load(); };
document.getElementById("tag").onchange = e => { state.filter.tag = e.target.value; load(); };
document.getElementById("only-blocked").onchange = e => { state.filter.blocked = e.target.checked; load(); };
document.getElementById("new-btn").onclick = () => quickNew();
document.getElementById("drawer-close").onclick = () => closeDrawer();
load();
```

- [ ] **Step 4: 写剩余样式**

追加到 `web/style.css`：

```css
.toolbar{display:flex;gap:.75rem;align-items:center;padding:.75rem 1rem;
  background:var(--panel);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
.toolbar input[type=search]{flex:0 0 16rem;background:var(--card);color:var(--fg);
  border:1px solid var(--border);border-radius:var(--r);padding:.4rem .6rem}
.toolbar select{background:var(--card);color:var(--fg);border:1px solid var(--border);
  border-radius:var(--r);padding:.4rem .6rem}
.toolbar .toggle{display:flex;align-items:center;gap:.35rem;color:var(--fg-dim)}
.toolbar .primary{margin-left:auto;background:var(--st-clarified);color:var(--bg);
  border:0;border-radius:var(--r);padding:.45rem .9rem;font-weight:600}

.board{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:.75rem;
  padding:.75rem;align-items:start;min-height:calc(100vh - 3.4rem)}
.col{background:var(--panel);border:1px solid var(--border);border-radius:var(--r);
  padding:.6rem;min-height:8rem}
.col-head{display:flex;align-items:baseline;gap:.5rem;margin-bottom:.6rem}
.col-count{font-family:var(--mono);font-size:1.5rem;line-height:1;color:var(--fg)}
.col-name{color:var(--fg-dim)}
.col-list{display:flex;flex-direction:column;gap:.5rem;min-height:2rem}
.col-add{width:100%;margin-top:.5rem;background:transparent;color:var(--fg-dim);
  border:1px dashed var(--border);border-radius:var(--r);padding:.35rem}
.col.drop-hint{outline:2px dashed var(--fg-dim);outline-offset:-2px}
.col-add:hover{color:var(--fg);border-color:var(--fg-dim)}
```

- [ ] **Step 5: 启动并目视验证**

Run: `python -m uvicorn app:app --app-dir server --port 8472`
（另开终端）`start http://127.0.0.1:8472`
确认：五列出现、列头显示中文与数字、无 404（style.css / app.js）、控制台无报错。

- [ ] **Step 6: 提交**

```bash
git add web/index.html web/style.css web/app.js
git commit -m "feat: 前端骨架——设计令牌与五列看板渲染"
```

---

### Task 6: 流转轨与阻塞态

**Files:**
- Modify: `web/app.js`（`card()` 函数）
- Modify: `web/style.css`
- Test: 手工 + 浏览器控制台无报错

**Interfaces:**
- Consumes: Task 5 的 `card(i)` 占位实现
- Produces: `buildRail(status, blocked) -> HTMLElement`，在 `card()` 内调用

- [ ] **Step 1: 在 `web/app.js` 中替换 `card()`**

```javascript
// SPEC 8.3：按索引画，不按"走过的路径"画；跳过的态算已过。
// 阻塞时只染当前态圆点 + 卡片红描边，整轨不变红（否则丢失进度信息）。
function buildRail(status, blocked){
  const rail = el("div","rail");
  const idx = STATUS_ORDER.indexOf(status);
  STATUS_ORDER.forEach((st,i)=>{
    const seg = el("span","rail-seg " + (i<idx?"past":i===idx?"now":"future"));
    seg.style.setProperty("--seg", "var(--st-"+st+")");
    if(i===idx && blocked) seg.classList.add("is-blocked");
    seg.title = STATUS_LABEL[st];
    rail.append(seg);
  });
  return rail;
}

function card(i){
  const c = el("article","card");
  c.dataset.id = i.id;
  c.append(buildRail(i.status, i.blocked));
  c.append(el("div","card-title", i.title));
  const meta = el("div","card-meta");
  meta.append(el("span","card-id", "#"+i.id));
  i.tags.split(",").filter(Boolean).forEach(t => meta.append(el("span","tag",t)));
  c.append(meta);
  if(i.blocked) c.classList.add("is-blocked");
  c.addEventListener("click", () => openDrawer(i.id));
  return c;
}

async function quickNew(){
  const title = prompt("这个创意是什么？");
  if(!title || !title.trim()) return;
  await api("POST","/api/ideas",{title:title.trim()});
  await load();
}
```

- [ ] **Step 2: 在 `web/style.css` 追加流转轨与卡片样式**

```css
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);
  padding:.55rem .6rem;box-shadow:var(--shadow);cursor:grab;display:flex;flex-direction:column;gap:.4rem}
.card:hover{background:var(--lift)}
.card.is-blocked{border-color:var(--blocked)}
.card.is-blocked .card-title{color:var(--fg-dim)}
.card.dragging{opacity:.4}

.rail{display:flex;flex-direction:column;gap:2px;width:6px;flex:0 0 6px}
.rail-seg{height:6px;border-radius:1px;background:var(--seg)}
.rail-seg.past{background:var(--seg);opacity:.55}          /* SPEC 8.3 对比度要求 */
.rail-seg.future{background:transparent;border-top:1px dashed var(--fg-dim);height:0}
.rail-seg.now{background:var(--seg);box-shadow:0 0 6px var(--seg);border-radius:50%}
.rail-seg.now.is-blocked{background:var(--blocked);box-shadow:0 0 6px var(--blocked)}

.card-title{font-size:.9rem}
.card-meta{display:flex;flex-wrap:wrap;gap:.3rem;align-items:center}
.card-id{font-family:var(--mono);color:var(--fg-dim);font-size:.75rem}
.tag{font-size:.7rem;color:var(--fg-dim);background:var(--panel);
  border-radius:999px;padding:.05rem .45rem}

.drawer{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;justify-content:flex-end;z-index:40}
.drawer[hidden]{display:none}
.drawer-inner{width:min(30rem,92vw);background:var(--panel);border-left:1px solid var(--border);
  padding:1rem;overflow:auto}
.drawer-close{position:absolute;top:1rem;right:1rem;background:none;border:0;color:var(--fg-dim);font-size:1rem}
```

- [ ] **Step 3: 手工验证流转轨**

启动服务，在浏览器控制台执行：

```javascript
// 1 idea / 2 clarifying / 3 clarified / 4 building / 5 done 各建一条
["仅有","明确中","明确了","制作中","成品"].forEach((t,k)=>fetch("/api/ideas",{
  method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({title:"轨测试-"+t})}).then(r=>r.json()).then(d=>
  ["idea","clarifying","clarified","building","done"].slice(0,k+1).forEach(s=>
    fetch(`/api/ideas/${d.id}/advance`,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({to:s})}))));
```

刷新后目视确认：
- `轨测试-制作中` 的轨 = **3 段实心 + 第 4 段发光圆点 + 第 5 段虚线**
- `轨测试-仅有` 的轨 = **只有第 1 段发光，其余全虚线**
- 对 "轨测试-制作中" 执行 `block`，确认**只有当前态圆点变红、卡片红描边、进度条不整条变红**

- [ ] **Step 4: 提交**

```bash
git add web/app.js web/style.css
git commit -m "feat: 流转轨与阻塞态——按索引渲染，跳过的态算已过"
```

---

### Task 7: 抽屉与拖拽

**Files:**
- Modify: `web/app.js`
- Modify: `web/style.css`
- Test: 手工 + `python -m pytest test_panel.py -q` 不回归

**Interfaces:**
- Consumes: Task 5 的 `api()` `state` `render()`、Task 6 的 `buildRail()`
- Produces: `openDrawer(id)` `closeDrawer()` `moveCard(id, from, to, beforeId)`

- [ ] **Step 1: 在 `web/app.js` 追加抽屉实现**

```javascript
// SPEC 8.5：推进控件必须带可选备注框，否则 human 路径的 events 全是空 message
function openDrawer(id){
  state.openId = id;
  const d = document.getElementById("drawer");
  const body = document.getElementById("drawer-body");
  body.textContent = "";
  api("GET","/api/ideas/"+id).then(idea => {
    const h = el("h2","drawer-title", idea.title);
    body.append(h);
    body.append(buildRail(idea.status, idea.blocked));

    const form = el("div","advance-form");
    const sel = el("select");
    STATUS_ORDER.forEach(s => sel.append(new Option(STATUS_LABEL[s], s, false, s===idea.status)));
    const note = el("input"); note.placeholder="这步做了什么（可留空）";
    const go = el("button","primary","推进");
    go.onclick = async () => {
      if(sel.value === idea.status){ alert("已经处于此状态"); return; }  // 与服务端 422 对齐
      await api("POST","/api/ideas/"+id+"/advance",{to:sel.value,note:note.value});
      closeDrawer(); await load();
    };
    form.append(sel, note, go);
    body.append(form);

    const bTxt = el("p","blocked-note", "卡住原因：" + idea.blocked_note);
    if(idea.blocked) body.append(bTxt);        // SPEC 5.6：只在阻塞时渲染
    const bBtn = el("button", idea.blocked?"secondary":"warn",
                    idea.blocked?"解除阻塞":"标记卡住");
    bBtn.onclick = async () => {
      if(idea.blocked) await api("POST","/api/ideas/"+id+"/unblock");
      else { const r = prompt("卡在哪了？"); if(!r) return;
             await api("POST","/api/ideas/"+id+"/block",{reason:r}); }
      await load(); openDrawer(id);
    };
    body.append(bBtn);

    body.append(el("h3","timeline-h","时间线"));
    const tl = el("ol","timeline");
    idea.events.forEach(e => {
      const li = el("li");
      li.append(el("span","ev-when", e.created_at.slice(5,16)));
      li.append(el("span","ev-actor", e.actor));
      li.append(el("span","ev-text",
        e.kind === "status"
          ? STATUS_LABEL[e.from_status] + " → " + STATUS_LABEL[e.to_status] + (e.message?"  "+e.message:"")
          : e.message || e.kind));
      tl.append(li);
    });
    body.append(tl);
  });
  d.hidden = false;
}

function closeDrawer(){ document.getElementById("drawer").hidden = true; state.openId = null; }

// 跨列拖拽：先比源列与目标列，相同走 PATCH，不同才 advance（SPEC 8.5）
// 落回原列发 advance 会撞服务端 422，冒出莫名错误
async function moveCard(id, fromStatus, toStatus, beforeId){
  if(fromStatus === toStatus){
    const siblings = state.items
      .filter(i => i.status === toStatus && i.id !== id)
      .sort((a,b)=> a.position-b.position || a.id-b.id);
    const before = beforeId ? siblings.find(s=>s.id===beforeId) : null;
    const prev = before ? siblings[siblings.indexOf(before)-1] : null;
    const pos = before ? (prev ? (prev.position+before.position)/2 : before.position-1)
                       : (siblings.length ? siblings[siblings.length-1].position+1 : 1);
    await api("PATCH","/api/ideas/"+id,{position:pos});
  }else{
    await api("POST","/api/ideas/"+id+"/advance",{to:toStatus});
  }
  await load();
}

let dragId = null, dragFrom = null;
document.addEventListener("dragstart", e => {
  const c = e.target.closest(".card"); if(!c) return;
  dragId = +c.dataset.id; dragFrom = c.closest(".col").dataset.status;
  c.classList.add("dragging"); e.dataTransfer.effectAllowed = "move";
});
document.addEventListener("dragend", e => {
  const c = e.target.closest(".card"); if(c) c.classList.remove("dragging");
  document.querySelectorAll(".col.drop-hint").forEach(x=>x.classList.remove("drop-hint"));
});
document.addEventListener("dragover", e => {
  const col = e.target.closest(".col"); if(!col) return;
  e.preventDefault(); col.classList.add("drop-hint");
});
document.addEventListener("dragleave", e => {
  const col = e.target.closest(".col"); if(col) col.classList.remove("drop-hint");
});
document.addEventListener("drop", async e => {
  const col = e.target.closest(".col"); if(!col || dragId === null) return;
  e.preventDefault(); col.classList.remove("drop-hint");
  const beforeCard = e.target.closest(".card");
  await moveCard(dragId, dragFrom, col.dataset.status,
                 beforeCard && +beforeCard.dataset.id !== dragId ? +beforeCard.dataset.id : null);
  dragId = null;
});
```

同时把 `card()` 里的 `c.addEventListener("click", ...)` 保持不变，并给 `.card` 加 `draggable="true"`：

```javascript
c.draggable = true;   // 加在 card() 内 c.classList.add("is-blocked") 之前
```

- [ ] **Step 2: 在 `web/style.css` 追加抽屉样式**

```css
.drawer-title{font-size:1rem;margin:0 0 .5rem;padding-right:2rem}
.advance-form{display:flex;gap:.4rem;flex-wrap:wrap;margin:.75rem 0}
.advance-form select,.advance-form input{background:var(--card);color:var(--fg);
  border:1px solid var(--border);border-radius:var(--r);padding:.35rem .5rem}
.advance-form input{flex:1;min-width:8rem}
.advance-form .primary{background:var(--st-clarified);color:var(--bg);border:0;
  border-radius:var(--r);padding:.35rem .8rem;font-weight:600}
.blocked-note{color:var(--fg-dim);font-size:.8rem;margin:.4rem 0}
.warn{background:transparent;color:var(--blocked);border:1px solid var(--blocked);
  border-radius:var(--r);padding:.35rem .8rem}
.secondary{background:transparent;color:var(--fg-dim);border:1px solid var(--border);
  border-radius:var(--r);padding:.35rem .8rem}
.timeline-h{font-size:.8rem;color:var(--fg-dim);margin:1.2rem 0 .4rem;font-weight:600}
.timeline{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.3rem}
.timeline li{display:grid;grid-template-columns:4.2rem 3rem 1fr;gap:.4rem;
  font-size:.78rem;align-items:baseline}
.ev-when{font-family:var(--mono);color:var(--fg-dim)}
.ev-actor{color:var(--st-clarifying)}
.ev-text{color:var(--fg)}
```

- [ ] **Step 3: 手工验证**

1. 把一张 `idea` 卡拖到 `building` 列 → 卡片状态改变，刷新后仍在新列，时间线多一条 `status`（actor=`human`）
2. 在同一列内上下拖动 → 顺序变化，刷新后保持，**时间线不新增 status**
3. 把卡拖出再放回原列 → **不报错**
4. 点卡片开抽屉 → 推进控件含"状态下拉 + 备注框"，填备注推进后时间线显示该备注
5. 点"推进"时若目标态=当前态 → 提示"已经处于此状态"，**不发请求**
6. 点"标记卡住"填原因 → 卡片压暗 + 红描边 + 圆点变红，抽屉显示原因；"解除阻塞"后卡片恢复但原因仍在详情（刷新后 `blocked_note` 未清）

- [ ] **Step 4: 提交**

```bash
git add web/app.js web/style.css
git commit -m "feat: 抽屉与拖拽——跨列一次 advance、落回原列走 PATCH"
```

---

### Task 8: 启动脚本、skill 与 README

**Files:**
- Create: `start.bat`
- Create: `install_skill.bat`
- Create: `skill/SKILL.md`
- Create: `skill/_meta.json`
- Create: `README.md`

**Interfaces:**
- Consumes: Task 3 的 `app:app`、Task 4 的 `panel.py`
- Produces: 可交付的启动与安装路径

- [ ] **Step 1: 写 `start.bat`**

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "data" mkdir "data"
echo 创意面板  http://127.0.0.1:8472
start "" "http://127.0.0.1:8472"
"C:\ProgramData\anaconda3\python.exe" -m uvicorn app:app --app-dir server --port 8472 --host 127.0.0.1
pause
```

- [ ] **Step 2: 写 `install_skill.bat`**

```bat
@echo off
chcp 65001 >nul
rem 单向同步：以项目内 skill\ 为准，覆盖安装目录（SPEC 10）
set "SRC=%~dp0skill"
set "DST=%USERPROFILE%\.minimax\skills\creative-panel"
if not exist "%DST%" mkdir "%DST%"
copy /Y "%SRC%\SKILL.md" "%DST%\" >nul
copy /Y "%SRC%\_meta.json" "%DST%\" >nul
copy /Y "%SRC%\panel.py" "%DST%\" >nul
echo 已安装到 %DST%
pause
```

- [ ] **Step 3: 写 `skill/SKILL.md`**

```markdown
---
name: creative-panel
description: "创意面板——登记并推进创意的完成状态。用户冒出新想法、要推进某个创意、或要看面板现状（哪些卡住了、哪些在制作中）时使用。触发词：创意面板, 记个想法, 推进创意, 面板现状, idea board."
---

# 创意面板

用 `panel.py` 操作本机创意面板。面板记录「创意」和「完成状态」。

**面板是账本，不是执行器。** 本 skill 只负责读写面板状态；真正干活（出图、做视频、写稿）
调你已有的其他 skill。

## 五个状态（线性）

| key | 显示 |
|---|---|
| `idea` | 仅有创意 |
| `clarifying` | 正在明确 |
| `clarified` | 已经明确 |
| `building` | 正在制作 |
| `done` | 完成成品 |

## 什么时候用

- 用户冒出想法 → `new`
- 某个创意要往前推 → `advance`
- 想看面板现状 → `list --blocked` 或 `stats`

## 命令

```bash
python panel.py new "<标题>" [--body 描述] [--tags a,b] [--source 来源]
python panel.py list [--status 状态] [--tag 标签] [--q 关键词] [--blocked]
python panel.py show <id>
python panel.py advance <id> <状态> [--note "这步做了什么"]
python panel.py note <id> "<消息>"
python panel.py block <id> "<卡住的原因>"
python panel.py unblock <id>
python panel.py stats
python panel.py delete <id>
```

`advance` 的状态参数中英文都接受。

## 工作流

1. 用户说"我有个想法…" → `new`，落在 `idea`
2. 用 brainstorming 把需求聊清楚 → 结论写进 `--body` → `advance clarifying`
3. 方案确定 → `advance clarified`
4. 调对应 skill 做东西 → `advance building`
5. 交付 → `advance done`

## 铁律

- **每推进一步必须带 `--note`**，说清这步做了什么
- **不许在没经用户确认的情况下直接跳到「完成成品」**
- 做不下去就 `block` 写清原因，不要硬撑，也不要悄悄跳过
- 推进到当前状态会被拒绝（退出码 1），说明状态已经变了，先 `show` 看清楚再动
```

- [ ] **Step 4: 写 `skill/_meta.json`**

```json
{
  "version": "1.0.0",
  "name": "creative-panel",
  "platform": "minimax"
}
```

- [ ] **Step 5: 写 `README.md`**

```markdown
# 创意面板

记录「创意」与「完成状态」的本机面板。五态线性流水线，看板拖拽推进，agent 通过
`creative-panel` skill 读写同一份数据。

## 启动

双击 `start.bat`，浏览器会自动打开 http://127.0.0.1:8472

需要 Python 3.13 + fastapi + uvicorn（已装在 `C:\ProgramData\anaconda3`）。

## 安装 skill

双击 `install_skill.bat`，把 skill 同步到 `%USERPROFILE%\.minimax\skills\creative-panel\`。
**以项目内 `skill\` 为准**，不要直接改安装目录里的副本。

## agent 用法

```bash
python skill\panel.py new "AI 发展史短片系列" --tags 视频,科普
python skill\panel.py advance 1 clarifying --note "开始聊需求"
python skill\panel.py stats
```

## 测试

```bash
python -m pytest test_panel.py -q
```

## 数据

`data/panel.db`（SQLite）。备份直接拷这个文件。

设计文档：`docs/superpowers/specs/2026-10-12-creative-panel-design.md`
```

- [ ] **Step 6: 验证启动脚本**

双击 `start.bat` → 浏览器打开、五列渲染、无报错；关闭控制台窗口即停服务。

- [ ] **Step 7: 提交**

```bash
git add start.bat install_skill.bat skill README.md
git commit -m "feat: 启动脚本、creative-panel skill 与 README"
```

---

### Task 9: 端到端验收

**Files:**
- Modify: `test_panel.py`（补遗漏项）
- Test: 全部

**Interfaces:**
- Consumes: Task 1–8 全部产物
- Produces: 通过 SPEC 第 11 节全部 18 条的测试套件

- [ ] **Step 1: 跑全量测试**

Run: `python -m pytest test_panel.py -q`
Expected: 全部 PASS。逐条对照 SPEC 第 11 节编号，确认无遗漏；缺哪条补哪条。

- [ ] **Step 2: 核对 SPEC 覆盖**

逐节确认 SPEC 有对应实现：

- 第 4 节：schema、三条 PRAGMA、CHECK、触发器、tags 规范化、position 作用域
- 第 5 节：11 个端点、PATCH 白名单、422 文案、筛选语义、actor 规则
- 第 6 节：9 个 CLI 子命令、人话输出、服务未启动提示
- 第 7 节：SKILL.md 的三条铁律
- 第 8 节：设计令牌、五色状态色、流转轨、抽屉、动效仅一处、`prefers-reduced-motion`
- 第 9 节：错误表 12 行逐条可复现
- 第 12 节：确认没有实现任何 YAGNI 项（无分页、无多项目、无 stage_defs）

- [ ] **Step 3: 质量底线检查**

在浏览器中逐项确认：
- Tab 键能遍历所有可交互元素，焦点环可见
- `prefers-reduced-motion: reduce` 下拖拽无动画
- 1280px 宽度下五列不横向溢出
- 断开外网后刷新页面，一切正常（零 CDN）

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "test: 端到端验收——补齐 SPEC 第 11 节全部用例"
```

---

## Self-Review

**1. Spec 覆盖**：SPEC 13 节全部有对应任务——第 4 节→T1/T2，第 5 节→T2/T3，第 6 节→T4，第 7 节→T8，第 8 节→T5/T6/T7，第 9 节→T3/T4，第 10 节→T8，第 11 节→贯穿各任务测试 + T9，第 12 节由 T9 Step 2 核对，第 13 节是选型理由（体现在 T1/T3/T5 的技术选择中）。

**2. Placeholder 扫描**：无 TBD/TODO；所有代码块为可直接落地的完整实现。

**3. 类型一致性**：`STATUS_LABEL` 在 T1 由 `db.STATUS_LABEL` 定义，T3 导入 `db`，T4 自带一份同内容的 `LABEL`，T5 的 `app.js` 有同内容的 `STATUS_LABEL`——三处 key 完全一致（T1/T4/T5 都是 idea/clarifying/clarified/building/done）。`store.advance(idea_id, to, note=None, position=None, actor="agent")` 的签名在 T2 定义、T3 按关键字调用，一致。`update_idea(idea_id, fields, actor)` 同。
