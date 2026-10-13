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

# ideas 表新增列 + idea_docs 表后的 schema 版本。用来判断存量库有没有迁移过。
SCHEMA_VERSION = 2

# v2 新增的两列。ALTER TABLE ADD COLUMN 只在列真的缺失时才跑——靠 PRAGMA
# table_info 判存在性，而不是只看 user_version：中途存在过手工改过表的库，
# user_version 对不上但列已经在了，只信版本号就会撞 duplicate column。
# NOT NULL + 常量默认值是 SQLite 允许的，老行自动补空串。
_V2_COLUMNS = (
    ("project_dir", "TEXT NOT NULL DEFAULT ''"),
    ("agent_session", "TEXT NOT NULL DEFAULT ''"),
)

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
  project_dir  TEXT NOT NULL DEFAULT '',
  agent_session TEXT NOT NULL DEFAULT '',
  position     REAL NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  done_at      TEXT,
  CHECK (status IN ('idea','clarifying','clarified','building','done'))
);
CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status);

-- 卡片引用的项目文档：只存**路径**，不存内容。文档在外面改了，面板重新读就是
-- 新的（SPEC 10.1）。position 是挂载次序，按登记先后递增。
CREATE TABLE IF NOT EXISTS idea_docs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id    INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
  path       TEXT NOT NULL,
  title      TEXT NOT NULL DEFAULT '',
  position   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idea_docs_idea ON idea_docs(idea_id, position, id);

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
    """建表 + 幂等迁移。

    executescript 里的 CREATE TABLE IF NOT EXISTS 对**已存在的表**是彻底的空操作，
    它不会把新列补进老表——所以新列一律靠下面的 ALTER。只查列是否存在，不查
    user_version：存量库可能来自任何一次手工改动，只信版本号会在"版本号是 0
    但列已经在了"的库上撞 duplicate column 而起不来。
    """
    c = connect()
    try:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(ideas)")}
        for name, decl in _V2_COLUMNS:
            if name not in cols:
                c.execute("ALTER TABLE ideas ADD COLUMN %s %s" % (name, decl))
        c.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        c.commit()
    finally:
        c.close()


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


def iso_from_timestamp(ts: float) -> str:
    """把 POSIX 时间戳转成与 now_iso 同一形态的字符串。

    文件 mtime 要和 created_at / updated_at 在同一个时间轴上排序、给人看，
    所以不能直接塞一个 Unix 秒数进去——形态不一致会让"这份文档什么时候改过"
    在时间线上没法和卡片事件对照。
    """
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


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