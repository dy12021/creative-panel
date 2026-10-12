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

BASE_URL = os.environ.get("PANEL_URL", "http://127.0.0.1:8472").rstrip("/")
HEADERS = {"X-Panel-Actor": "agent"}

# 只用于把服务端返回的 key 翻成中文显示，**不做校验**。合法状态由
# db.STATUS_KEYS 说了算；CLI 侧再维护一份表就会和它分叉——面板改列序时
# 这份表还按旧顺序翻译，且拒掉服务端新加的状态。
# （这份拷贝是刻意的：install_skill.bat 只同步 skill/ 目录到
# %USERPROFILE%\.minimax\skills\，那里没有 server/db.py 可 import。）
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
    except httpx.RequestError:
        # 连接被拒 / 读超时都归到这一句：本机面板不会自己慢到超时，
        # 见到这个错就说明没起来（SPEC 第 9 节第一行）。
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
            cur = call("GET", "/api/ideas/%s" % a.id)
            raise Fail("#%s 已经在『%s』了" % (a.id, label(cur["status"])))
        raise
    print("✔ #%s 已推进，当前状态：%s" % (a.id, label(d["status"])))
    if a.note:
        print("    %s" % a.note)


def _timeline(events):
    for e in events:
        who = ACTOR.get(e["actor"], e["actor"])
        ts = e["created_at"]
        # "2026-10-12T15:18:32.123" -> "10-12 15:18"：中间那个 T 是 ISO
        # 格式留下的，不去掉就不是给人看的。
        when = "%s %s" % (ts[5:10], ts[11:16])
        if e["kind"] == "status":
            print("  %s [%s] %s → %s %s" % (when, who, label(e["from_status"]),
                                             label(e["to_status"]), e["message"]))
        else:
            print("  %s [%s] %s %s" % (when, who, KIND.get(e["kind"], e["kind"]),
                                       e["message"]))


def _show(a):
    d = call("GET", "/api/ideas/%s" % a.id)
    print("#%d「%s」  状态=%s%s" % (d["id"], d["title"], label(d["status"]),
                                    "  已阻塞：%s" % d["blocked_note"]
                                    if d["blocked"] else ""))
    if d["tags"] != ",":
        print("  标签：%s" % "、".join(t for t in d["tags"].strip(",").split(",")))
    if d["body"]:
        print("  描述：%s" % d["body"].replace("\n", "\n        "))
    print("  ── 时间线 ──")
    _timeline(d["events"])


def _list(a):
    items = call("GET", "/api/ideas", params={
        "status": _to_key(a.status) if a.status else None,
        "tag": a.tag, "q": a.q, "blocked": "1" if a.blocked else None})
    if not items:
        print("（没有符合条件的创意）")
        return
    for d in items:
        print("#%-4d %s [%s] %s" % (d["id"], "■" if d["blocked"] else " ",
                                    label(d["status"]), d["title"]))


def _stats():
    s = call("GET", "/api/stats")
    print("  " + " | ".join("%s %d" % (label(k), s["counts"][k]) for k in LABEL))
    print("  阻塞中：%d" % s["blocked"])


def dispatch(a):
    c = a.cmd
    if c == "new":
        d = call("POST", "/api/ideas",
                 json={"title": a.title, "body": a.body,
                       "tags": a.tags, "source": a.source})
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
