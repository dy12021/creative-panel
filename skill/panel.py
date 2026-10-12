"""创意面板 CLI —— agent 的操作入口。

设计依据 SPEC 第 6 节。输出给人看，不输出 JSON。

三处不能省的收口：
- 服务地址取自 PANEL_URL，缺省本机 8472；
- 每次请求固定带 X-Panel-Actor: agent（SPEC 5.4）；
- 任何失败都是「一句人话 + 退出码 1」，绝不打 Python 栈（SPEC 第 9 节）。

面板是账本不是执行器：这里只改面板上的状态，不代替任何做活的 skill。
"""
import argparse
import os
import sys

import httpx

# 缺省是本机面板的端口（SPEC 6）。单独拎一个常量是为了让白盒测试能把它读
# 出来——这个值是契约，不是一个随手写的字面量。
DEFAULT_URL = "http://127.0.0.1:8472"
BASE_URL = os.environ.get("PANEL_URL", DEFAULT_URL).rstrip("/")
HEADERS = {"X-Panel-Actor": "agent"}

# 只用于把服务端返回的 key 翻成中文显示，**不做校验、也不定列序**
# （列序跟着服务端走，见 _stats）。合法状态由 db.STATUS_KEYS 说了算；
# 面板新加一个状态，这里只是少一个翻译——那个 key 会原样显示，不影响可用。
# （这份拷贝没法避免：install_skill.bat 只同步 skill/ 目录到
# %USERPROFILE%\.minimax\skills\，那边没有 server/db.py 可 import。
# 漂移由 test_cli_label_table_matches_the_server 上锁。）
LABEL = {"idea": "仅有创意", "clarifying": "正在明确", "clarified": "已经明确",
         "building": "正在制作", "done": "完成成品"}

# events.kind 同样只用于显示。时间线是给 agent 读的一行人话，
# "note / edited" 这种裸枚举混在里面等于让调用方再翻译一遍。
KIND = {"created": "创建", "status": "推进", "note": "备注",
        "blocked": "阻塞", "unblocked": "解除阻塞", "edited": "修改"}
ACTOR = {"human": "你", "agent": "agent", "system": "系统"}

TIMEOUT = 10


class Fail(Exception):
    """一次请求没成。message 是要打给人看的那句话。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status          # 给需要按状态码分支的调用方用


def label(key):
    """未知 key 也照样显示，不因为多了一个状态就 KeyError。

    显示层不该崩——真出问题了也得先让人看到已经发生的事。
    这里只兜"键不认识"；键的类型由调用点的 _obj(types=...) 收口，不在这里查。
    """
    return LABEL.get(key, key)


def _to_key(text):
    """中文显示名 -> key。认不出来就**原样返回**，交给服务端判。

    CLI 侧不校验状态名：非法值发出去，服务端回 422 并附上 5 个合法值，
    比 CLI 自己抄一份表可靠（见 LABEL 处的说明）。
    """
    for k, v in LABEL.items():
        if text == v:
            return k
    return text


# 值的类型也要查：进 `%d` 的要是整数，进 `.strip()`/`.replace()` 的要是文本。
# tags/body/blocked_note 额外容许 null——"没填"在显示层就是空，不是错
# （本面板的列是 NOT NULL，正常走不到 null；容许它是为了让外来响应也不炸）。
_NULL_IS_EMPTY = (str, type(None))


def _obj(value, where, *keys, types=None):
    """确认一段响应的**键在、值的类型也对**，原样返回；不对就 Fail。

    SPEC 第 9 节禁的是「Python 栈甩给 agent」，那不只发生在**错误**响应上：
    PANEL_URL 指到别的服务、服务端比 CLI 新一版，200 里都可能是个数组或者少
    了字段。那时 `d["counts"]` 这种直接下标抛的是 TypeError/KeyError，
    和 JSONDecodeError 一样是满屏栈。成功路径也得说一句人话。

    键在≠形状对：`id` 是个字符串时 `"#%d" % d["id"]` 照样抛 TypeError，
    `tags` 是个数字时 `.strip()` 抛 AttributeError。所以类型也得查——哪些
    位置敏感（进 `%d`、进 `.strip()`/`.replace()`、当 `label()` 的键）就
    在 `types` 里登记，不散落 `isinstance`。
    """
    if not isinstance(value, dict) or any(k not in value for k in keys):
        raise Fail("面板返回的%s不是预期格式（需要：%s），请确认 PANEL_URL 指向面板"
                   % (where, "、".join(keys)))
    for k, typ in (types or {}).items():
        if not isinstance(value[k], typ):
            raise Fail("面板返回的%s里「%s」类型不对（收到 %s），请确认 PANEL_URL 指向面板"
                       % (where, k, type(value[k]).__name__))
    return value


def _num(v, where):
    """把服务端给的数字转成人看的整数。

    `"%d" % "abc"` 抛的是 TypeError——形状对但类型不对时一样会漏栈，
    所以这个收口不能省。
    """
    try:
        return "%d" % int(v)
    except (TypeError, ValueError):
        raise Fail("面板返回的%s不是数字（%r），请确认 PANEL_URL 指向面板"
                   % (where, v))


def _detail_of(r):
    """把错误响应压成一句人话。**永远不抛**。

    契约上 detail 恒为字符串（SPEC 第 9 节），但 500 不在契约内：未捕获
    异常时服务端返的是纯文本 `Internal Server Error`，r.json() 当场抛
    JSONDecodeError，CLI 跟着吐一串 Python 栈。detail 不是字符串的形态
    （pydantic 数组）同样兜住——那是给程序员看的内容。
    """
    body = None
    try:
        body = r.json()
    except ValueError:
        pass
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    # 到这里只剩三种情况：纯文本 500、detail 形态异常、body 不是对象。
    # 只在"响应体压根不是 JSON"时才取首行；把 JSON/HTML 原样打出来对
    # agent 没意义，还会被当成后端的话去信。
    text = "" if body is not None else (r.text or "")
    head = next((x.strip() for x in text.splitlines() if x.strip()), "")
    if head.startswith("<"):
        head = ""
    prefix = "面板内部错误" if r.status_code >= 500 else "面板拒绝了这次请求"
    return ("%s（HTTP %d）%s" % (prefix, r.status_code, head)).strip()


def call(method, path, **kw):
    try:
        r = httpx.request(method, BASE_URL + path, headers=HEADERS,
                          timeout=TIMEOUT, **kw)
    except (httpx.RequestError, httpx.InvalidURL):
        # 连接被拒 / 读超时都归到这一句：本机面板不会自己慢到超时，
        # 见到这个错就说明没起来（SPEC 第 9 节第一行）。
        # InvalidURL 不继承 RequestError（PANEL_URL 手输成 `http://[::1`
        # 就是它），漏掉的话那边照样吐栈。
        raise Fail("面板未启动，请双击 start.bat")
    if r.status_code >= 400:
        raise Fail(_detail_of(r), status=r.status_code)
    try:
        return r.json()
    except ValueError:
        # PANEL_URL 指到了网页而不是面板：说一句人话，别抛解析异常。
        raise Fail("面板返回的不是 JSON（HTTP %d），请确认 PANEL_URL 指向面板"
                   % r.status_code)


def _use_utf8_out():
    """把输出编码这件事收口。

    Windows 的 stdout 编码跟着 locale 走（中文机器是 cp936），两个后果都得
    处理：`✔` 和 emoji 在 cp936 里编不出来，`print` 直接抛 UnicodeEncodeError；
    子进程写出的 cp936 字节，上游按 UTF-8 解码又是一屏乱码。

    管道（被 agent 采集，isatty 为假）一律切 UTF-8；交互式终端保留控制台
    编码，只把 errors 放宽到 replace——宁可显示成 ?，也不能因为编码崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8" if not stream.isatty() else None,
                               errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def build_parser():
    p = argparse.ArgumentParser(description="创意面板 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="记一条新创意")
    n.add_argument("title")
    n.add_argument("--body", default="")
    n.add_argument("--tags", help="逗号分隔，首尾逗号服务端自己处理")
    n.add_argument("--source", default="")

    l = sub.add_parser("list", help="按状态/标签/关键词/阻塞筛选")
    l.add_argument("--status", help="中英文状态名都收")
    l.add_argument("--tag")
    l.add_argument("--q")
    l.add_argument("--blocked", action="store_true", help="只看卡住的")

    s = sub.add_parser("show", help="看一张卡的详情与时间线")
    s.add_argument("id")

    a = sub.add_parser("advance", help="推进状态")
    a.add_argument("id")
    a.add_argument("status", help="中英文状态名都收")
    a.add_argument("--note", default="")

    nt = sub.add_parser("note", help="加一条备注")
    nt.add_argument("id")
    nt.add_argument("message")

    b = sub.add_parser("block", help="标记卡住")
    b.add_argument("id")
    b.add_argument("reason")

    sub.add_parser("unblock", help="解除卡住").add_argument("id")
    sub.add_parser("stats", help="五列各几张")
    sub.add_parser("delete", help="删除一张卡").add_argument("id")
    return p


def main(argv=None):
    _use_utf8_out()
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args)
    except Fail as e:
        print(str(e))
        return 1


def _advance(a):
    to = _to_key(a.status)
    try:
        d = call("POST", "/api/ideas/%s/advance" % a.id,
                 json={"to": to, "note": a.note})
    except Fail as e:
        # 服务端只说「该创意已经处于此状态」，没说是哪个状态——对调用方没用。
        # 补读一次把状态名翻成人话（SPEC 6 点名的翻译）。
        if e.status == 422 and "已经处于此状态" in str(e):
            cur = _obj(call("GET", "/api/ideas/%s" % a.id), "创意详情", "status",
                       types={"status": str})
            raise Fail("#%s 已经在『%s』了" % (a.id, label(cur["status"])))
        raise
    print("✔ #%s 已推进，当前状态：%s"
          % (a.id, label(_obj(d, "推进结果", "status",
                              types={"status": str})["status"])))
    if a.note:
        print("    %s" % a.note)


def _timeline(events):
    if not isinstance(events, list):
        raise Fail("面板返回的时间线不是一个列表，请确认 PANEL_URL 指向面板")
    for raw in events:
        e = _obj(raw, "时间线里的一条",
                 "actor", "kind", "created_at", "message",
                 "from_status", "to_status",
                 types={"actor": str, "kind": str, "created_at": str,
                        "message": _NULL_IS_EMPTY,
                        "from_status": _NULL_IS_EMPTY,
                        "to_status": _NULL_IS_EMPTY})
        who = ACTOR.get(e["actor"], e["actor"])
        ts = e["created_at"]
        # "2026-10-12T15:18:32.123" -> "10-12 15:18"：中间那个 T 是 ISO
        # 格式留下的，不去掉就不是给人看的。
        when = "%s %s" % (ts[5:10], ts[11:16])
        msg = e["message"] or ""
        if e["kind"] == "status":
            # from/to 和 message 一样可能是 null（新建的创意没有来源状态），
            # 同样归一成空串。别把 Python 的 None 打进给人看的输出。
            print("  %s [%s] %s → %s %s" % (when, who, label(e["from_status"] or ""),
                                             label(e["to_status"] or ""), msg))
        else:
            print("  %s [%s] %s %s" % (when, who, KIND.get(e["kind"], e["kind"]),
                                       msg))


def _show(a):
    d = _obj(call("GET", "/api/ideas/%s" % a.id), "创意详情",
             "id", "title", "status", "blocked", "blocked_note",
             "tags", "body", "events",
             types={"id": int, "title": str, "status": str, "blocked": int,
                    "blocked_note": _NULL_IS_EMPTY, "tags": _NULL_IS_EMPTY,
                    "body": _NULL_IS_EMPTY, "events": list})
    print("#%d「%s」  状态=%s%s" % (d["id"], d["title"], label(d["status"]),
                                    "  已阻塞：%s" % (d["blocked_note"] or "")
                                    if d["blocked"] else ""))
    tags = [t for t in (d["tags"] or "").strip(",").split(",") if t]
    if tags:
        print("  标签：%s" % "、".join(tags))
    if d["body"]:
        print("  描述：%s" % d["body"].replace("\n", "\n        "))
    print("  ── 时间线 ──")
    _timeline(d["events"])


def _list(a):
    items = call("GET", "/api/ideas", params={
        "status": _to_key(a.status) if a.status else None,
        "tag": a.tag, "q": a.q, "blocked": "1" if a.blocked else None})
    if not isinstance(items, list):
        raise Fail("面板返回的创意列表不是一个列表，请确认 PANEL_URL 指向面板")
    if not items:
        print("（没有符合条件的创意）")
        return
    for raw in items:
        d = _obj(raw, "列表里的一条", "id", "status", "title", "blocked",
                 types={"id": int, "status": str, "title": str, "blocked": int})
        print("#%-4d %s [%s] %s" % (d["id"], "■" if d["blocked"] else " ",
                                    label(d["status"]), d["title"]))


def _stats():
    s = _obj(call("GET", "/api/stats"), "统计结果", "counts")
    counts = s["counts"]
    if not isinstance(counts, dict):
        raise Fail("面板返回的统计结果里 counts 不是对象，请确认 PANEL_URL 指向面板")
    # 列序跟着服务端走：store.stats() 的 counts 是按 db.STATUS_KEYS 造的
    # dict，顺序天然权威。再抄一份 LABEL 的键序来遍历有两个后果——服务端
    # 少给一个键就当场 KeyError（满屏栈），而若只补个 0 蒙混过去，面板
    # 上就会多出一列账本里根本没有的数。
    print("  " + " | ".join("%s %s" % (label(k), _num(v, "状态「%s」的计数" % k))
                             for k, v in counts.items()))
    print("  阻塞中：%s" % _num(s.get("blocked", 0), "阻塞计数"))


def dispatch(a):
    c = a.cmd
    if c == "new":
        d = _obj(call("POST", "/api/ideas",
                      json={"title": a.title, "body": a.body,
                            "tags": a.tags, "source": a.source}),
                 "新建结果", "id", "title", "status",
                 types={"id": int, "title": str, "status": str})
        print("✔ 已创建 #%d「%s」  状态=%s" % (d["id"], d["title"],
                                              label(d["status"])))
    elif c == "list":
        _list(a)
    elif c == "show":
        _show(a)
    elif c == "advance":
        _advance(a)
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
        _stats()
    elif c == "delete":
        call("DELETE", "/api/ideas/%s" % a.id)
        print("✔ 已删除 #%s" % a.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
