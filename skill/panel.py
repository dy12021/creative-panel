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

# `edit` 能改哪些字段：就是 PATCH 的白名单（SPEC 5），不多不少。
# 白名单外的字段服务端一律 422，所以这里重复校验没有意义；**顺序**要定死，
# 用来念"改了哪几个字段"。这份表与 store.FIELDS 的一致性由测试上锁。
EDIT_FIELDS = ("title", "body", "tags", "source", "blocked_note", "position",
               "project_dir", "agent_session")

# 字段名的中文显示，**只用于显示**，跟 LABEL 是同一个理由：安装目录里没有
# server/store.py 可 import，这份拷贝无法避免，靠 test_cli_edit_field_table_
# matches_the_server 锁住漂移（标签被复用给另一个字段比缺个翻译更毒）。
FIELD_LABEL = {"title": "标题", "body": "描述", "source": "来源",
               "tags": "标签", "blocked_note": "阻塞备注", "position": "排序位置",
               "project_dir": "项目主文件夹", "agent_session": "agent 对话名"}

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
    n.add_argument("--project-dir", dest="project_dir",
                   help="项目主文件夹（作品真正待着的工作目录）")
    n.add_argument("--agent-session", dest="agent_session",
                   help="这次 agent 对话的名字，面板会一直显示它")

    l = sub.add_parser("list", help="按状态/标签/关键词/阻塞筛选")
    l.add_argument("--status", help="中英文状态名都收")
    l.add_argument("--tag")
    l.add_argument("--q")
    l.add_argument("--blocked", action="store_true", help="只看卡住的")

    e = sub.add_parser("edit", help="改标题/描述/标签/来源等，不改状态")
    e.add_argument("id")
    e.add_argument("--title")
    e.add_argument("--body")
    e.add_argument("--tags")
    e.add_argument("--source")
    e.add_argument("--blocked-note", dest="blocked_note",
                   help="改阻塞备注文字，不改卡住与否（那是 block/unblock 的活）")
    e.add_argument("--position", help="排到本列第几位，数字")
    e.add_argument("--project-dir", dest="project_dir", help="项目主文件夹")
    e.add_argument("--agent-session", dest="agent_session",
                   help="把这次的 agent 对话名记到卡上（换话题了就再打一次）")

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

    # 文档是"引用"不是"附件"：db 里只有路径，正文读的时候现读。所以 doc rm
    # 删的是面板上的那条指针，磁盘上的文件一个字节都不会动——这一点印在帮助
    # 里，免得有人以为它会删文件。
    dc = sub.add_parser("doc", help="挂载/移除/查看这张卡引用的项目文档")
    dsub = dc.add_subparsers(dest="doc_cmd", required=True)
    dsub.add_parser("list", help="列出引用了哪些文档").add_argument("id")

    da = dsub.add_parser("add", help="挂载一篇文档（.md/.markdown/.mdx/.txt）")
    da.add_argument("id")
    da.add_argument("path", help="相对路径按面板的启动目录解析，建议直接给绝对路径")
    da.add_argument("--title", default="", help="留空就用文件名")

    dr = dsub.add_parser("rm", help="移除引用，不动磁盘上的文件")
    dr.add_argument("id")
    dr.add_argument("doc_id")

    ds = dsub.add_parser("show", help="把文档正文打到标准输出")
    ds.add_argument("id")
    ds.add_argument("doc_id")
    return p


def main(argv=None):
    _use_utf8_out()
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args)
    except Fail as e:
        print(str(e))
        return 1


def _edit_payload(a):
    """只把**命令行上真给了**的字段放进 PATCH 的 body。

    SPEC 5 的 PATCH 语义是「只有你传了的字段才改」，而 argparse 的默认值恰好
    就是这件事需要的那个区分：没写 `--body` 拿到 None，写了 `--body ""` 拿到
    空串。所以判据只能是 `is not None`——用真值判断（`if a.body:`）会把"传了
    空串想清空"和"没传"混成一谈，agent 就再也清不掉描述了。

    返回空 dict 时**照样发出去**，让服务端回「没有可更新的字段」。CLI 不自己
    抄这条规则：它和 5.5 那一串边界（null 422、白名单外 422、空对象 422）一样
    属于服务端契约，本地拦等于养一份会和 store.py 分叉的拷贝。
    """
    payload = {}
    for k in EDIT_FIELDS:
        v = getattr(a, k, None)
        if v is not None:
            payload[k] = v
    return payload


def _edit(a):
    payload = _edit_payload(a)
    # position 是 REAL 列，服务端只收数字（store._num 拒字符串）。命令行传的
    # 一定是文本，转一下再发；转不动（用户写了字母）就原样发出去让服务端回
    # 422「排序位置 必须是数字」——CLI 不维护第二套合法性判断。
    if isinstance(payload.get("position"), str):
        try:
            payload["position"] = float(payload["position"])
        except ValueError:
            pass
    d = _obj(call("PATCH", "/api/ideas/%s" % a.id, json=payload),
             "修改结果", "id", "title", "status",
             types={"id": int, "title": str, "status": str})
    # 念**发出去的那些键**，不是响应的全部字段——响应带着整张卡，把没改过的
    # 字段也念一遍等于谎报「已修改」。这串与账本里那条 `edited` event 的
    # message（"修改了：body、tags"）一一对应，键序也相同：payload 按
    # EDIT_FIELDS 构造，store 照 dict 序拼 message。
    names = [k for k in EDIT_FIELDS if k in payload]
    print("✔ #%d 已修改：%s" % (d["id"], "、".join(
        FIELD_LABEL.get(k, k) for k in names)))
    if "title" in payload:
        print("    标题现在是：%s" % d["title"])


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


def _doc_ids_hint(idea_id):
    """把"这张卡当前挂了哪些文档"拼成一句，附在报错后面。

    idea_docs 的 id 是**全局**自增的，和卡片的 id 不是一套号（#7 的卡上可能挂
    着 文档#5）。两者都印成 "#" 时，人和 agent 都会顺手把卡片的号填进 doc rm，
    报一句"这张卡上没有 #7 这份文档"，然后卡在这猜下一个号。把候选直接报出来
    比让人猜一轮便宜得多。
    """
    try:
        items = _obj(call("GET", "/api/ideas/%s/docs" % idea_id),
                     "文档清单", "docs", types={"docs": list})["docs"]
    except Fail:
        return ""
    if not items:
        return "  这张卡当前没有挂任何文档。"
    return "  这张卡当前挂的：%s" % "、".join(
        "文档#%s=%s" % (x.get("id"), x.get("title")) for x in items)


def _no_such_doc(idea_id, doc_id):
    return Fail("#%s 上没有 文档#%s\n%s" % (idea_id, doc_id, _doc_ids_hint(idea_id)))


def _doc(a):
    """文档是引用：db 里只有路径，正文要的时候现读。"""
    k = a.doc_cmd
    if k == "list":
        items = _obj(call("GET", "/api/ideas/%s/docs" % a.id),
                     "文档清单", "docs", types={"docs": list})["docs"]
        if not items:
            print("#%s 还没有挂项目文档" % a.id)
            return
        for raw in items:
            d = _obj(raw, "文档清单里的一条",
                     "id", "path", "title", "exists",
                     types={"id": int, "path": str, "title": str, "exists": bool})
            # 一律叫「文档#N」而不是「#N」：文档 id 是全局自增的，和卡片 id
            # 不是一套号，写成同一个样子迟早有人把卡片的号填进来。
            print("文档#%-3d %s%s" % (d["id"], d["title"],
                                      "" if d["exists"] else "   ← 文件已不在原处"))
            print("        %s" % d["path"])
    elif k == "add":
        d = _obj(call("POST", "/api/ideas/%s/docs" % a.id,
                      json={"path": a.path, "title": a.title}),
                 "挂载结果", "id", "path", "title",
                 types={"id": int, "path": str, "title": str})
        print("✔ #%s 已挂载文档：文档#%d %s" % (a.id, d["id"], d["title"]))
        print("    %s" % d["path"])
    elif k == "rm":
        try:
            call("DELETE", "/api/ideas/%s/docs/%s" % (a.id, a.doc_id))
        except Fail as e:
            if e.status == 404:
                raise _no_such_doc(a.id, a.doc_id)
            raise
        print("✔ #%s 已移除 文档#%s 的引用（磁盘上的文件没动）" % (a.id, a.doc_id))
    elif k == "show":
        try:
            d = _obj(call("GET", "/api/ideas/%s/docs/%s/content" % (a.id, a.doc_id)),
                     "文档内容", "title", "path", "exists", "text",
                     types={"title": str, "path": str, "exists": bool, "text": str})
        except Fail as e:
            if e.status == 404:
                raise _no_such_doc(a.id, a.doc_id)
            raise
        if not d["exists"]:
            raise Fail("#%s 引用的 文档#%s 已经不在原处：%s"
                       % (a.id, a.doc_id, d["path"]))
        sys.stdout.write(d["text"])
        if d["text"] and not d["text"].endswith("\n"):
            sys.stdout.write("\n")


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
    # project_dir / agent_session / docs 是 v2 才有的键，不进上面那串必填键：
    # 老服务端返回的详情缺它们时，这里该安静地少显示两行，而不是报"格式不对"
    # ——那是给人看的排障信息，不是版本检查。doc 子命令走自己的端点，版本对不
    # 对会在那里以 detail 的形式报出来。
    if d.get("project_dir"):
        print("  项目主文件夹：%s" % d["project_dir"])
    if d.get("agent_session"):
        print("  agent 对话：%s" % d["agent_session"])
    docs = d.get("docs") or []
    if docs:
        print("  ── 项目文档 ──")
        for doc in docs:
            flag = "" if doc.get("exists") else "   ← 文件已不在原处"
            print("  文档#%s %s%s" % (doc["id"], doc["title"], flag))
            print("      %s" % doc["path"])
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
        # 只把真给了的字段发出去，理由和 _edit_payload 完全一样：不带
        # --project-dir 时 argparse 给的是 None，直接透传会让服务端 null
        # 守卫拒掉整条请求（"项目主文件夹 不能为 null"），而这里本来只想
        # 「没填」。缺席用 ""，null 交给 store 报——两者语义不同。
        payload = {"title": a.title, "body": a.body, "tags": a.tags,
                   "source": a.source}
        for k in ("project_dir", "agent_session"):
            v = getattr(a, k, None)
            if v is not None:
                payload[k] = v
        d = _obj(call("POST", "/api/ideas", json=payload),
                 "新建结果", "id", "title", "status",
                 types={"id": int, "title": str, "status": str})
        print("✔ 已创建 #%d「%s」  状态=%s" % (d["id"], d["title"],
                                              label(d["status"])))
        if payload.get("project_dir") or payload.get("agent_session"):
            print("    项目主文件夹：%s" % payload.get("project_dir", ""))
            print("    agent 对话：%s" % payload.get("agent_session", ""))
    elif c == "list":
        _list(a)
    elif c == "edit":
        _edit(a)
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
    elif c == "doc":
        _doc(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
