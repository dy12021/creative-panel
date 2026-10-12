"""FastAPI 路由。设计依据 SPEC 第 5 节。

detail 会原样返给客户端，是 API 契约的一部分（SPEC 第 9 节），不是日志文案——
CLI 只是把它翻译给人看，所以这里的字面量改了等于改契约。
"""
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
BLOCKED_VALUES = ("0", "1")


def resolve_actor(request: Request) -> str:
    """SPEC 5.4：只认三个值，非法值当没传记 human，不报错。

    这是约定不是安全边界——浏览器 fetch 同样能设这个头，本机也没有鉴权。
    唯一的价值是让时间线里的 actor 大致可信，不要把它当防伪机制加固。
    """
    v = request.headers.get("X-Panel-Actor")
    return v if v in ACTORS else "human"


def blocked_filter(raw):
    """blocked 只接受 0/1，非法值 422，不静默降级（SPEC 5.3）。

    store 里 `blocked in (1, "1", True)` 这句对 "5" 判 False -> 当成 0，
    于是 ?blocked=5 静默返回"全部未卡住"的卡片，用户以为筛选生效了。
    而 HTTP 查询参数恒为字符串：声明成 int 拦不住（"5" 照样解析成 5），
    白名单只能在这一层显式比对字符串。
    """
    if raw is None:
        return None
    if raw not in BLOCKED_VALUES:
        raise store.Invalid("未知取值：%s（blocked 只接受 0 或 1）" % raw)
    return int(raw)


def create_app():
    db.init_db()
    app = FastAPI(title="创意面板", version=VERSION)

    @app.exception_handler(store.NotFound)
    async def _not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(store.Invalid)
    async def _invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": exc.detail})

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION}

    # 收 JSON body 的端点一律把 body 声明成 dict：数组/字符串/数字/null 会在
    # 进 store 之前被挡成 422，而不是让 store.update_idea 拿 list 调 .items()
    # 炸出 500（堆栈对调用方毫无用处）。
    @app.get("/api/ideas")
    def ideas(status: str | None = None, tag: str | None = None,
              q: str | None = None, blocked: str | None = None):
        # 四个筛选正交取交集（SPEC 5.3）；排序由 store 负责，这里不重排。
        return store.list_ideas(status=status, tag=tag, q=q,
                                blocked=blocked_filter(blocked))

    @app.post("/api/ideas", status_code=201)
    def create(body: dict, request: Request):
        # 值直接透传，**不要**写成 body.get("body") or ""：`or ""` 会把
        # null / 0 / False 一律洗成空串，store 的 null 与类型守卫就永远
        # 触发不到，POST {"title":"a","body":0} 静默存成空描述——SPEC 第 9 节
        # 明确说，不报错的数据损坏比 500 更糟。缺席才用 ""，null 交给 store
        # 报「描述 不能为 null，请传空串」。tags 例外：create_idea 的签名里
        # None 就是"未提供"这个哨兵，这里没法也不该把 null 和缺席区分开。
        return store.create_idea(
            title=body.get("title", ""),
            body=body.get("body", ""),
            tags=body.get("tags"),
            source=body.get("source", ""),
            actor=resolve_actor(request))

    @app.get("/api/ideas/{idea_id}")
    def one(idea_id: int):
        return store.get_idea(idea_id)      # 不存在时抛 NotFound -> 404

    @app.patch("/api/ideas/{idea_id}")
    def patch(idea_id: int, body: dict, request: Request):
        # SPEC 5.1：status 必须 422，且给这句专属文案（store 的通用
        # "不支持的字段" 也够用，但这里用的是 SPEC 点名的字面量）。
        if "status" in body:
            raise store.Invalid("请使用 /advance 接口变更状态")
        # 其余白名单、空对象、null、类型全交给 store 判，app 层不重复实现。
        return store.update_idea(idea_id, body, actor=resolve_actor(request))

    @app.post("/api/ideas/{idea_id}/advance")
    def adv(idea_id: int, body: dict, request: Request):
        # to 缺失 -> None -> store 报「未知状态：None（合法值：…）」。
        # note / position 传 null 是合法缺省：store 用 None 表示"不指定"，
        # 与 PATCH 里 null 一律 422 的语义不同，这里不能混。
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
        store.delete_idea(idea_id)      # 不存在时抛 NotFound -> 404
        return {"ok": True}

    @app.get("/api/stats")
    def stats():
        return store.stats()

    # web/ 由 Task 5 创建；目录不存在时 StaticFiles 会当场抛错，所以先判
    # 存在再挂。挂在最后——Starlette 按注册顺序匹配，路由在前，不会被盖住。
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


app = create_app()