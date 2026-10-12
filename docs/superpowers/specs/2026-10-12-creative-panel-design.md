# 创意面板 · 设计文档

- 日期：2026-10-12
- 状态：待评审
- 端口：8472

---

## 1. 问题与目标

创意从冒出来到落地，会经过一个过程：只有一个念头 → 把需求聊清楚 → 有方案了 → 真正在做 → 做完交付。这个过程今天散落在对话记录、session workspace 和脑子里，没有一个地方能一眼看到"我有多少想法卡在哪一步"。

**创意面板**要解决的就是这件事：记录每一个创意，追踪它的完成状态，让人和 agent 在同一个账本上协作。

**它是什么**：一个本机 Web 看板 + 一个 `creative-panel` skill。skill 教 agent 如何登记和推进创意，看板把这些数据渲染成人眼一眼能扫的样子。

**它不是什么**：不是素材库（不存图片/视频文件本身），不是执行器（不自己跑 skill）。

## 2. 核心职责

明确划清边界，避免后面实现时越界：

| 是 | 不是 |
|---|---|
| 创意的账本 | 创意的执行引擎 |
| 完成状态的看板 | 通用任务管理系统 |
| agent 与人共享的同一份数据 | 两套互不相通的数据 |

## 3. 运行形态

- 本机单进程服务，`start.bat` 双击启动
- 后端 FastAPI，数据存 `data/panel.db`（SQLite）
- 前端单页 SPA，原生 JS，**零 CDN 依赖**（断网可用）
- 无账号、无鉴权、无网络依赖 —— 纯本机自用
- 与 `media-center-pms`（端口 8471）技术栈一致，互不干扰

## 4. 数据模型

### 4.1 五个状态

状态存英文 key，界面显示中文。改名不用动数据。

| key | 显示 | 含义 |
|---|---|---|
| `idea` | 仅有创意 | 只有一个念头 |
| `clarifying` | 正在明确 | 在把想法聊成清楚的需求 |
| `clarified` | 已经明确 | 有方案了 |
| `building` | 正在制作 | 正在调 skill 做东西 |
| `done` | 完成成品 | 落地、可交付 |

状态是**线性有序**的：`idea < clarifying < clarified < building < done`。

### 4.2 存储约定

以下几条是容易踩坑的地方，实现时必须照做：

**`tags` 首尾补逗号。** 存 `",视频,科普,"` 而不是 `"视频,科普"`，这样精确匹配只需一条 `LIKE '%,'||?||',%'`，不用写四路 `OR`。**标签本身禁止含逗号**——CLI 和 API 都拒绝含逗号的标签，这是最简单且不会产生歧义的规则。

**`position` 的作用域是"列内"。** 即 `(status, position)` 才是完整的排序键，不是全局唯一。跨列拖拽后 `position` 必须在目标列内重新计算，不沿用原值。所有排序一律 `ORDER BY position, id`——`id` 作为 tie-breaker 防止浮点相等时顺序抖动。中点插入法在长期反复拖拽后会耗尽浮点精度，届时执行一次全量重排（按当前列顺序重写 `position` 为 1,2,3…）。

**`updated_at` 由 SQLite 触发器维护**，不靠每个写路径手写，避免漏改：

```sql
CREATE TRIGGER trg_ideas_touch AFTER UPDATE ON ideas
BEGIN
  UPDATE ideas SET updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')
  WHERE id = NEW.id;
END;
```

**`PRAGMA journal_mode=WAL`** 在每次建连时设置。agent 和浏览器可能同时写，WAL 避免互相阻塞。

### 4.3 表结构

```sql
CREATE TABLE ideas (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  body         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'idea',
  blocked      INTEGER NOT NULL DEFAULT 0,
  blocked_note TEXT NOT NULL DEFAULT '',
  tags         TEXT NOT NULL DEFAULT '',   -- 约定：首尾补逗号
  source       TEXT NOT NULL DEFAULT '',
  position     REAL NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  done_at      TEXT
);
CREATE INDEX idx_ideas_status ON ideas(status);

CREATE TABLE events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id     INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,   -- created|status|note|blocked|unblocked|edited
  from_status TEXT,
  to_status   TEXT,
  actor       TEXT NOT NULL,   -- human|agent|system
  message     TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_events_idea ON events(idea_id, id);
```

级联删除依赖两件事同时成立，缺一不可：上面的 `REFERENCES ... ON DELETE CASCADE`，以及 `db.py` 每次建连时执行 `PRAGMA foreign_keys=ON`（SQLite 默认关闭外键，不开这一句级联不会发生）。

`source` 的语义：**这条创意是从哪儿来的**，例如 `对话产生` / `随手记` / `从 AI 发展史项目带过来`。它是自由文本，不做枚举约束。

### 4.4 events 表

审计流。人（前端拖拽）和 agent（CLI）的每次改动都留痕。`kind` 取值：

| kind | 何时写 |
|---|---|
| `created` | POST /api/ideas |
| `status` | advance |
| `note` | POST .../note |
| `blocked` / `unblocked` | block / unblock |
| `edited` | PATCH（见 5.5） |

时间戳统一用 ISO 8601 本地时间字符串。

## 5. REST API

前端和 CLI 共用同一套 API，SQLite 是唯一真相源。

```
GET    /api/health                        {ok, version}
GET    /api/ideas?status=&tag=&q=&blocked= 列表
POST   /api/ideas                         建创意
GET    /api/ideas/{id}                    单条 + events
PATCH  /api/ideas/{id}                    见下方白名单
POST   /api/ideas/{id}/advance            {to, note?, position?}
POST   /api/ideas/{id}/note               {message} 追加备注
POST   /api/ideas/{id}/block              {reason}
POST   /api/ideas/{id}/unblock
DELETE /api/ideas/{id}
GET    /api/stats                         各态计数 + 阻塞数
```

`version` 取自 `server/app.py` 顶部的 `VERSION` 常量，硬编码，随功能变更手动递增。

**`PATCH` 字段白名单**：仅接受 `title`、`body`、`tags`、`source`、`blocked_note`、`position`。请求体中出现任何其他键（含 `status`、`blocked`、`id`）一律返回 422。

### 5.1 关键约束：状态只能走 advance

**`PATCH` 请求体中出现 `status` 字段必须返回 422**，错误信息为"请使用 /advance 接口变更状态"。

理由：`advance` 是唯一会写 `events` 的状态变更路径。禁止 PATCH 改 status，保证任何状态变化都必然产生一条审计记录，不会出现卡片莫名跳格而时间线里查不到原因。

同理 `block` / `unblock` 也走各自的端点，不通过 PATCH 的 `blocked` 字段——但 PATCH 允许改 `blocked_note`。

**advance 到当前状态返回 422**，错误信息"该创意已经处于此状态"。理由与禁 PATCH status 一致：`events` 只记录真实变更，不写 `from == to` 的噪声记录。代价是前端在渲染"推进"按钮时要自己判断目标态，不能无条件发请求。

**状态跳跃是允许的**：可以从 `idea` 直接 advance 到 `building`。允许回退。约束只在于"必须留下记录"，不限制跳几步。

**advance 到 `done` 时自动写 `done_at`**；离开 `done` 时清空 `done_at`。

### 5.2 position 与拖拽

`advance` 接受可选 `position`，用于**跨列拖拽时一次调用同时改状态和位置**，避免"advance + PATCH"两次调用产生竞态。

- **跨列拖拽** → `POST /advance {to, position}`
- **列内拖拽** → `PATCH {position}`

新建卡片 `position = 该列当前最大值 + 1`。

### 5.3 筛选语义

四个筛选参数正交，同时出现时取交集。**任一筛选参数非法一律返回 422**，不做静默降级——fail fast 与 advance 的非法状态名处理保持一致。

- `status`：精确等于该状态 key；非法值 422
- `tag`：与**整标签**精确相等，不是子串匹配。配合 4.2 的首尾逗号约定，实现为 `tags LIKE '%,'||?||',%'`
- `q`：对 `title` 和 `body` 分别做子串匹配，大小写不敏感。空格分隔的多个关键词是 **AND** 关系，且**每个词独立地匹配 title 或 body**（不是把整串当一个子串找）
- `blocked`：`1` 只返回卡住的，`0` 只返回没卡住的，省略返回全部

### 5.4 actor 的判定

`events.actor` 由请求头决定，请求体不接受该字段。

- `panel.py` CLI 固定带 `X-Panel-Actor: agent`
- 前端约定**不带**该头，服务端缺省记为 `human`

这是一个**约定而非安全边界**：浏览器 fetch 同样能设置自定义头。本机无鉴权、仅自己使用，这个弱约束够用，唯一的价值是让时间线里的 actor 大致可信。**不要把它当成防伪机制写进代码注释。**

### 5.5 PATCH 的 event 写法

一次 PATCH 无论改几个字段，**只写一条 `edited` event**，`message` 记录被修改的字段名列表（如 `修改了：tags, source`），不存 diff——diff 在这里没有阅读价值，知道"哪些字段被碰过"就够了。

### 5.6 unblock 的行为

`unblock` **保留** `blocked_note` 不清空。历史原因以 `events` 为准，卡片上的 `blocked_note` 只是当前快照。**前端只在 `blocked == 1` 时渲染 `blocked_note`**，避免出现"没卡住却挂着旧原因"的错觉。

再次 `block` 会覆盖 `blocked_note`。

### 5.7 删除

`DELETE /api/ideas/{id}` 通过外键级联删除其 `events`，不留孤儿。依赖 4.3 的 `ON DELETE CASCADE` 与 `PRAGMA foreign_keys=ON`。

## 6. panel.py CLI

agent 的操作入口，是 REST API 的薄封装。输出给人看，不输出 JSON。

```bash
python panel.py new "<标题>" [--body 描述] [--tags a,b] [--source 来源]
python panel.py list [--status <态>] [--tag <标签>] [--q <关键词>] [--blocked]
python panel.py show <id>
python panel.py advance <id> <状态> [--note "这步做了什么"]
python panel.py note <id> "<消息>"
python panel.py block <id> "<卡住的原因>"
python panel.py unblock <id>
python panel.py stats
python panel.py delete <id>
```

输出示例：

```
✔ 已创建 #12「AI 发展史短片系列」  状态=仅有创意
✔ #12 正在明确 → 已经明确
    需求已明确：6段H3，每段90秒
```

`advance` 的第二个参数接受英文 key 或中文显示名，两者都合法。推进到当前状态会被服务端拒绝（422），CLI 需把该错误翻译成「#12 已经在『正在明确』了」。

CLI 每次请求固定带 `X-Panel-Actor: agent` 头。`tags` 的首尾逗号由服务端统一处理，CLI 只需原样传入 `视频,科普`。

服务地址从 `panel.py` 顶部的 `BASE_URL` 常量读，默认 `http://127.0.0.1:8472`；可用环境变量 `PANEL_URL` 覆盖（测试用）。

## 7. creative-panel skill

### 7.1 职责边界

skill 只管**怎么操作面板**。具体干活（出图、做视频、写稿）仍由已有的 53 个 skill 负责。

**面板是账本，不是执行器。**

### 7.2 SKILL.md 内容

**何时使用**
- 用户冒出一个想法，要求记下来
- 某个创意要往前推一步
- 要看面板现状（哪些卡住了、哪些在制作中）

**工作流**

1. 用户说"我有个想法…" → `new`，状态 `idea`
2. 用 brainstorming 把需求聊清楚 → 结论写进 `body` → `advance clarifying`
3. 方案确定 → `advance clarified`
4. 调对应 skill 做东西 → `advance building`
5. 交付 → `advance done`

**铁律**

- 每推进一步必须带 `--note` 说清这步做了什么
- **不许在没经用户确认的情况下直接跳到"完成成品"**
- 做不下去就 `block` 写清原因，不要硬撑，也不要悄悄跳过

## 8. 界面设计

视觉方向对标本机的 SnowLuma WebUI：从其打包 CSS 中提取的设计令牌为基准。

### 8.1 底色

单色冷灰，全部 `hue 250`，彩度极低。彩色只在表达状态时出现。

| 用途 | 值 |
|---|---|
| 应用底 | `oklch(13% .008 250)` |
| 面板 | `oklch(15% .01 250)` |
| 卡片 | `oklch(17% .012 250)` |
| 抬升 / hover | `oklch(19% .012 250)` |
| 边框 | `oklch(20% .03 250 / .28)` |
| 主文字 | `oklch(98% .005 240)` |
| 次文字 | `oklch(66% .02 240)` |

### 8.2 状态色

| 状态 | 色 | 理由 |
|---|---|---|
| `idea` 仅有创意 | 中性灰 `oklch(66% .02 240)` | 没方向就该是灰的 |
| `clarifying` 正在明确 | sky `oklch(75% .14 230)` | 在探索，冷 |
| `clarified` 已经明确 | violet `oklch(70% .18 293)` | 成形了，接品牌紫 |
| `building` 正在制作 | amber `oklch(80% .16 80)` | 在干活，是热的 |
| `done` 完成成品 | emerald `oklch(72% .15 160)` | 绿，收尾 |
| 阻塞 | red `oklch(62% .2 27)` | 仅作用于当前态圆点与卡片描边 |

色序语义：灰 → 冷 → 冷紫 → 暖 → 绿，色温从冷走到暖再收。amber 用于"制作中"是热度而非警告；红色全部留给阻塞。

色值统一取两位小数（原始取自 SnowLuma 的 violet 是 `293.541`，此处已归一化）。

### 8.3 签名元素：流转轨

每张卡片左缘一条竖直轨，分 5 段对应 5 态。段填充规则——**以当前态索引 `i`（`idea`=0 … `done`=4）为准**：

- 索引 `< i` → 实心（已过）
- 索引 `== i` → 发光圆点（当前）
- 索引 `> i` → 虚线（未到）

**跳跃推进时，被跳过的态算"已过"，一律实心。** 例如 `idea` 直接跳到 `building`（`i=3`），前 3 段全实心，第 4 段发光，第 5 段虚线。这符合线性有序语义，实现时不要按"走过的路径"画，要按索引比较画。

```
▌ AI 发展史短片系列
▌ ┃━━━━━━━◉┄┄┄       ← building：3 段实心，第 4 段发光，第 5 段虚线
▌ 视频 · 科普
```

**阻塞时不要把整条轨涂红。** 整轨变红会丢掉"走到哪一步"这个信息，正是流转轨存在的意义。正确做法：只把**当前态圆点**转为红色，并给卡片加一道红色描边。进度可读性必须保留。

作用：不点开卡片就能看出它走到哪一步。这是本产品区别于通用任务板的核心识别特征。

### 8.4 布局

统计数字直接作为列头的 mono 大数字，不单独占一行。

```
┌ 创意面板      搜索…   标签▾   ⊘只看不卡住的   ＋ 记一个想法 ┐
├────────────┬────────────┬────────────┬────────────┬──────────┤
│ 3          │ 2          │ 4          │ 1          │ 12       │
│ 仅有创意   │ 正在明确   │ 已经明确   │ 正在制作   │ 完成成品 │
├────────────┼────────────┼────────────┼────────────┼──────────┤
│ ▌卡片      │ ▌卡片      │ ▌卡片      │ ▌卡片      │ ▌卡片    │
│ ▌ ▣卡住    │            │            │            │          │
│ ＋          │            │            │            │          │
└────────────┴────────────┴────────────┴────────────┴──────────┘
```

### 8.5 交互

- **跨列拖拽** = 一次 `POST /advance {to, position}`，立刻写 `events`（actor=human）
- **列内拖拽** = `PATCH {position}`
- 点卡片 = 右侧抽屉：完整描述、标签编辑、阻塞按钮、**完整时间线**
- 抽屉里的"推进"控件 = 目标状态选择器 + **一个可填可不填的备注框**。人手操作同样要能补一句，否则 human 路径写出的 events 全是空 message，时间线会很难读
- 拖动时显示落点占位块 `#0000001a`（沿用 SnowLuma 的做法）

### 8.6 动效

只做一处：放下卡片时，流转轨的实心段从旧态滑到新态。其余一律不加。全部动效遵守 `prefers-reduced-motion`。

### 8.7 字体

- 正文：`system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei"`，`.84rem / 1.45`
- 数字与 ID：`ui-monospace, Consolas, "Liberation Mono", monospace`
- 标题 `letter-spacing: -.025em`
- 零网络字体

### 8.8 形状

`radius-2xl = 1rem`，`box-shadow: 0 1px 2px rgba(0,0,0,.15)`，`spacing` 基准 4px。

## 9. 错误处理

| 情况 | 行为 |
|---|---|
| 服务未启动 | CLI 打印「面板未启动，请双击 start.bat」，退出码 1 |
| 标题为空 | 422；CLI 打印「标题不能为空」 |
| 非法状态名 | 422；CLI 列出 5 个合法状态名 |
| advance 到当前状态 | 422；「该创意已经处于此状态」 |
| `status` 筛选值非法 | 422；「未知状态：foobar」 |
| PATCH 含白名单外的字段 | 422；指出是哪个字段，并提示状态须走 advance |
| 标签含逗号 | 422；「标签不能包含逗号」 |
| id 不存在 | 404；CLI 打印「没有 #99 这条创意」 |

CLI 捕获 HTTP 错误后把后端的 `detail` 字段原样翻译成人话，不打印堆栈。

## 10. 目录结构

```
creative-panel/
  start.bat                 双击启动
  install_skill.bat         同步 skill 到 %USERPROFILE%\.minimax\skills\
  server/
    app.py                  FastAPI 路由 + VERSION 常量
    db.py                   schema + 建连（含两个 PRAGMA）+ 触发器
  web/
    index.html
    app.js
    style.css
  skill/
    SKILL.md                creative-panel 技能源文件
    panel.py                CLI
  data/
    panel.db                （不入版本库）
  test_panel.py
  README.md
  docs/superpowers/specs/
```

skill 源码放在项目里与代码一起版本控制。`install_skill.bat` 负责同步到 `%USERPROFILE%\.minimax\skills\creative-panel\`，避免两份漂移。

**注意：Windows 的 bat 不展开 `~`**，脚本里必须写 `%USERPROFILE%` 而不是 `~/.minimax`。

`.gitignore` 忽略 `data/`。

## 11. 测试策略

`test_panel.py`，依赖 `pytest` + `httpx`。

**运行方式**：用 `tmp_path` fixture 建临时 db 文件，通过 `PANEL_DB` 环境变量让 `db.py` 指向它；在同一 fixture 里用 `uvicorn.Server` 起一个后台线程监听**随机空闲端口**，测试全部通过真实 HTTP 访问。fixture 结束时关线程、删临时文件。**绝不使用 `data/panel.db`**，也不占用固定端口 8472，因此可与正在运行的面板并存、可重复运行。

覆盖：

1. 建创意 → 5 态全链路推进 → `events` 条数与顺序正确
2. `PATCH` 传 `status` 返回 422
3. `PATCH` 传白名单外字段返回 422
4. advance 到当前状态返回 422
5. 阻塞 / 解除阻塞各写一条 event，且 `unblock` 后 `blocked_note` 保留
6. 四维筛选：`status` / `tag` / `q` / `blocked`，含非法 `status` 返回 422
7. 删除级联删除 `events`（这条专门验证 `PRAGMA foreign_keys=ON` 真的生效）
8. advance 到 `done` 写 `done_at`，离开 `done` 清空
9. `PATCH` 多字段只写一条 `edited` event
10. tags 首尾逗号约定：`tag=科普` 命中、`tag=科` 不命中
11. 排序稳定性：`position` 相同时按 `id` 升序

目标：改后端后一条命令能确认没回归。

## 12. 不做的事（YAGNI）

以下明确排除，留到真正需要时再说：

- 账号 / 权限系统（纯本机自用）
- 附件存储与在线预览（面板不存素材文件本身）
- 多项目 / 多看板
- 状态自定义（5 态固定，线性，不做成可配置的 stage_defs）
- 富文本编辑器（`body` 用纯文本 + 换行）
- 服务端推送（前端轮询即可）
- 移动端适配（先保证桌面 1280px 以上可用）
- skill 自动调用（面板不会主动去跑别的 skill）

**将来很可能会回头做的**（现在明确不做，但记在这里免得重新讨论）：

- `GET /api/events?since=` 全局事件流。若以后想做"最近发生了什么"这类跨创意的视图，现在只能逐个 idea 取 events 拼装
- 卡片附件缩略图（要动存储模型，是另一个 spec 的事）
- 多设备同步（当前假设只有这一台机器）

## 13. 技术选型理由

- **FastAPI + SQLite + 原生 JS**：与 `media-center-pms` 同款，已在该机器上跑通并维护多轮，踩过的坑已知
- **CLI 封装而非裸 API**：`SKILL.md` 里教 agent 拼 JSON 会带来持续的失败率；CLI 输出人话，agent 只需读一行
- **零 CDN**：本机面板在无外网环境下也应可用
- **纯系统字体栈**：中文界面用系统中文字体是正确的工程选择，且保证离线