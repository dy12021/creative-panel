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
    """毫秒精度、无时区（SPEC 4.3）。

    只需要一个纯 SQLite 标量函数，不碰文件系统——用 :memory: 而不是走
    connect()，免得为了取时间戳去 mkdir + 建库，顺手污染真实 data/panel.db。
    """
    c = sqlite3.connect(":memory:")
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