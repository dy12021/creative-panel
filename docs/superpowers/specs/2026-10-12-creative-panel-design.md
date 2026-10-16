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

**`tags` 首尾补逗号 + 逐个 strip。** 存 `",视频,科普,"` 而不是 `"视频,科普"`，这样精确匹配只需一条 `LIKE '%,'||?||',%'`，不用写四路 `OR`。

规范化流程（服务端在写入时统一处理，存进去的一定是规范形式）：

1. 按逗号切分
2. 每个标签 `strip()` 去掉首尾空白
3. **丢弃空标签**
4. 去重后重新拼接，首尾各补一个逗号

`"视频, 科普, "` → `",视频,科普,"`。

**空标签的两种情况必须分开处理**：

- 请求**未提供** `tags` 字段（POST / PATCH 都不带）→ 存 `","`，表示"无标签"。这是合法状态。
- 请求**提供了** `tags` 但规范化后全空（如 `"  ,  ,"`）→ **422「标签不能为空」**。用户明确给了值却全是废的，属错误，fail fast。

**逗号是标签的分隔符，因此"标签含逗号"这个错误在设计上不可表达**——用户输入 `"a,b"` 就是两个标签，不是"一个含逗号的标签"。不要为它写校验分支，那条规则是空的。

**`position` 的作用域是"列内"。** 即 `(status, position)` 才是完整的排序键，不是全局唯一。跨列拖拽后 `position` 必须在目标列内重新计算，不沿用原值。

排序键为 **`<按看板列序的 status>, position, id`** 三段：

- `status` 用 `CASE` 表达式按 `idea → clarifying → clarified → building → done` 排（看板列序），**不用字典序**——字典序会把 `building` 排到 `idea` 前面，CLI 的 `list` 输出看起来是乱的。`CASE` 的取值从 `db.STATUS_KEYS` 生成，不硬编码。
- `position` 是列内顺序。
- `id` 是 tie-breaker，防浮点相等时顺序抖动。

**`position` 的归属分两种，别混：**

- **新建卡片**：**服务端算**，恒为 `COALESCE(该列 MAX(position), 0) + 1`，**客户端传的 `position` 一律忽略**（POST 不读该字段，有 `test_create_ignores_client_supplied_position` 锁定）。新卡总是落在本列末尾，这个语义没有歧义，也不需要前端参与。
- **拖拽落位**：**前端算**，因为它知道落点夹在哪两个邻居之间，取两者 `position` 的中点即可；服务端只存不校验，也不对取值范围做检查（本机自用，值再离谱也只是排序怪，不会损坏数据）。拖拽可传 `position`，不传则同样按 `COALESCE(MAX,0)+1` 追加到末尾。

**注意空列没有最大值，直接写 `MAX(position) + 1` 会得到 NULL，撞上 `NOT NULL` 约束。**

中点插入法在长期反复拖拽后会耗尽浮点精度。**重排触发条件**：当插入位置与相邻卡片的 `position` 差值小于 `1e-10` 时，按当前列的 `ORDER BY position, id` 顺序把该列 `position` 重写为 `1, 2, 3, …`。

**重排放服务端，不放前端。** 重排不是**校验**是**维护动作**：放服务端才绕不过去，`create_idea` / `update_idea` / `advance` 三条写 `position` 的路径全部经过同一个收口；放前端等于每个写入点抄一遍，漏一处就失效，而漏掉的那处恰好不报错，只是排序悄悄变怪。

**两条 SPEC 在"精确重复"这个输入上会碰撞，实现按 4.2 走。** 差值为 0 也是"小于 `1e-10`"，所以写一个与邻居完全同值的 `position` 会被立刻重排掉——**通过 API 造不出 position 相同的局面**。第 11 节第 11 条（`position` 相同时按 `id` 升序）锁的是**读数**口径，不是写入路径：库里仍然可能存在同值（旧数据、直接写库），`ORDER BY position, id` 照常兜底。该用例因此用裸 SQL 摆位，另有一条用例专门钉住"写同值会触发重排且不打乱相对顺序"。

**建连时必设的 PRAGMA（三类性质不同，别混为一谈）**：

- `PRAGMA foreign_keys=ON` —— **每连接生效**，SQLite 默认关闭。不设这一句，5.7 的级联删除根本不发生。
- `PRAGMA recursive_triggers=OFF` —— **每连接生效**，SQLite 默认关闭。**这是本项目的硬性前提，不是优化项**：4.3.1 实测证明 `updated_at` 触发器在 `ON` 下会无限递归。必须设，且有测试读回断言。
- `PRAGMA journal_mode=WAL` —— **数据库文件属性，首次建库设一次即可，之后自动持久化**。每次建连重复设置也幂等无妨，但它和上面两条不是一回事。用于避免 agent 与浏览器同时写时互相阻塞。

**`PANEL_DB` 环境变量**：`db.py` 从这里读数据库路径，**缺省为 `<项目根>/data/panel.db`**。测试用它指向临时文件。与 `PANEL_URL`（CLI 读，缺省 `http://127.0.0.1:8472`）对称。

### 4.3 表结构

```sql
CREATE TABLE ideas (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  body         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'idea',
  blocked      INTEGER NOT NULL DEFAULT 0,
  blocked_note TEXT NOT NULL DEFAULT '',
  tags         TEXT NOT NULL DEFAULT ',',   -- 约定：首尾补逗号；无标签存 ','
  source       TEXT NOT NULL DEFAULT '',
  position     REAL NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  done_at      TEXT,
  CHECK (status IN ('idea','clarifying','clarified','building','done'))
);
CREATE INDEX idx_ideas_status ON ideas(status);

CREATE TABLE events (
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
CREATE INDEX idx_events_idea ON events(idea_id, id);

CREATE TRIGGER trg_ideas_touch AFTER UPDATE ON ideas
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE ideas SET updated_at = strftime('%Y-%m-%dT%H:%M:%f','now','localtime')
  WHERE id = NEW.id;
END;
```

`ideas.status` 同样加 `CHECK (status IN ('idea','clarifying','clarified','building','done'))`。CHECK 约束与 API 层校验并存：API 层给人话错误，CHECK 层兜住任何绕过 API 的写入。

### 4.3.1 关于 updated_at 触发器的递归风险（本项目实测结论）

`WHEN NEW.updated_at = OLD.updated_at` 这个条件**并不能保证不自递归**。实测矩阵：

| WHEN 子句 | 时间精度 | `recursive_triggers=OFF` | `ON` + 该行恰为当前时刻 |
|---|---|---|---|
| 有 | 秒 | ✅ | ❌ 无限递归 |
| 有 | **毫秒** | ✅ | ❌ **仍然无限递归** |
| 无 | 秒 | ✅ | ❌ 无限递归（**跨秒也炸**） |

两条结论：

1. **毫秒精度不解决这个问题。** 递归在微秒内完成，`%f` 也会取到同一个值。实测毫秒精度与秒精度在恶劣条件下同样炸。
2. **WHEN 子句必须保留。** 去掉它之后，连"跨秒"这种常见情况也会无限递归（内层 `UPDATE` 改的字段不同 → 无 WHEN 判断 → 再次触发）。它是在承重的，不是冗余代码。

**因此：`PRAGMA recursive_triggers=OFF` 是本项目的硬性前提，不是优化项。** `db.py` 每次建连必须设置它，并有一条测试读回该 PRAGMA 断言其值为 0。WHEN 子句与毫秒精度是**纵深防御**，它们显著降低出问题的概率，但**不构成保证**——文档不宣称"即使打开也不会递归"。

时间戳用 `%Y-%m-%dT%H:%M:%f`（毫秒），产出如 `2026-10-12T14:30:00.123`，**不带时区偏移**。当前单机自用没问题；将来若跨设备或跨时区，见第 12 节。

级联删除依赖两件事同时成立，缺一不可：上面的 `REFERENCES ... ON DELETE CASCADE`，以及 `db.py` 每次建连时执行 `PRAGMA foreign_keys=ON`。

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

时间戳由服务端统一生成，用 `strftime('%Y-%m-%dT%H:%M:%f','now','localtime')`，**毫秒精度、不带时区偏移**（将来若要跨时区，见第 12 节）。

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

`version` 取自 `server/app.py` 顶部的 `VERSION` 常量，硬编码，随功能变更手动递增。本机自用这样最简单；若将来版本号开始漂移，可改为从 `.version` 文件读取或构建时注入。

`created_at` / `events.created_at` 由服务端统一生成，格式与 4.3 相同（毫秒精度、不带时区，见第 12 节）。

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

新建卡片的 `position` 取值、以及浮点精度耗尽时的重排阈值，见 4.2。

### 5.3 筛选语义

四个筛选参数正交，同时出现时取交集。**任一筛选参数非法一律返回 422**，不做静默降级——fail fast 与 advance 的非法状态名处理保持一致。

**空串不算非法。** `?status=` / `?tag=` / `?q=` / `?blocked=` 里的**空串或纯空白**表达的是"用户没选"，服务端归一成"不过滤"，**不报 422**。fail-fast 针对的是 `?status=foobar`、`?blocked=5` 这类**表达了非法意图**的真值。区分依据是：空串/空白没有表达任何意图，真值表达了错误意图。

- `status`：精确等于该状态 key；非法值 422
- `tag`：与**整标签**精确相等，不是子串匹配。配合 4.2 的首尾逗号约定，实现为 `tags LIKE '%,'||?||',%'`
- `q`：对 `title` 和 `body` 分别做子串匹配。空格分隔的多个关键词是 **AND** 关系，且**每个词独立地匹配 title 或 body**（不是把整串当一个子串找）。实现直接用 SQLite `LIKE`——它**默认对 ASCII 大小写不敏感**，英文 `AI`/`ai` 天然满足，中文无大小写也满足。**不要额外套 `lower()` 或 `COLLATE NOCASE`**；只有将来要支持俄语、希腊语等有大小写的非 ASCII 语言时才需要改。
- `blocked`：`1` 只返回卡住的，`0` 只返回没卡住的，省略返回全部

### 5.4 actor 的判定

`events.actor` 由请求头决定，请求体不接受该字段。

- `panel.py` CLI 固定带 `X-Panel-Actor: agent`
- 前端约定**不带**该头，服务端缺省记为 `human`

**头里的非法值一律忽略，按缺省 `human` 处理，不返回 422。** 只接受 `human` / `agent` / `system` 三个值，其余（如 `robot`）当没传。本机无鉴权，为一个取值报错没有意义，忽略更稳。

这是一个**约定而非安全边界**：浏览器 fetch 同样能设置自定义头。本机无鉴权、仅自己使用，这个弱约束够用，唯一的价值是让时间线里的 actor 大致可信。**不要把它当成防伪机制写进代码注释。**

### 5.5 PATCH 的 event 写法

一次 PATCH 无论改几个字段，**只写一条 `edited` event**，`message` 记录被修改的字段名列表（如 `修改了：tags, source`），不存 diff——diff 在这里没有阅读价值，知道"哪些字段被碰过"就够了。

边界情况：

- `PATCH {}`（空对象）→ **422**「没有可更新的字段」，与"白名单外字段"同样 fail fast。
- 字段值与当前值**完全相同** → **仍然 200 且仍写一条 `edited` event**。语义是"有人碰过这条记录"，且实现上无需额外做差异比较。
- 任一字段传 **`null`** → **422**，`字段名 不能为 null，请传空串`（带字段名前缀，见第 9 节）。要清空字段就传空串 `""`。`null` 语义歧义，且会撞 `NOT NULL` 约束。

  **这条守卫独立于类型校验存在，不能被类型分档取代。** null 检查先跑**全字段、无例外**（包括 `tags` 和 `position`），类型分档只对已确认非 null 的值生效。否则 `PATCH {"tags": null}` 会静默清空标签并写一条谎称"修改了：tags"的 event，`PATCH {"position": null}` 会漏成 `sqlite3.IntegrityError`。

### 5.6 block / unblock 的行为

**`block` 允许重复调用**，覆盖 `blocked_note`，**每次都写一条 `blocked` event**——因为 block 的核心是"记录原因"，原因变了就值得留痕。它与 advance 的语义不同：advance 是状态跃迁，到当前状态是噪声；block 是追加事实，重复即有新信息。

**`unblock` 保留** `blocked_note` 不清空。历史原因以 `events` 为准，卡片上的 `blocked_note` 只是当前快照。**前端只在 `blocked == 1` 时渲染 `blocked_note`**，避免出现"没卡住却挂着旧原因"的错觉。

再次 `block` 会覆盖 `blocked_note`。

### 5.7 删除

`DELETE /api/ideas/{id}` 通过外键级联删除其 `events`，不留孤儿。依赖 4.3 的 `ON DELETE CASCADE` 与 `PRAGMA foreign_keys=ON`。`idea_docs`（5.8）同样级联删除——留着孤儿行的话 `AUTOINCREMENT` 不回收 id，将来重建一张卡可能撞上旧文档记录，而这种错不报错，只会让新卡的文档莫名多出来。

**删卡不动磁盘上的文件。** `idea_docs` 存的只是路径，删引用等于让面板不再指向那个文件，文件本身毫发无损——这和「挂载时不复制内容」是同一条原则的两面。

**这是整个面板唯一不可撤销的操作**（没有回收站，级联掉的 events 找不回来），所以前端必须过一道显式确认，见 8.5.2。

### 5.8 项目文档（2026-10-13 增补）

**卡片引用的是文档的路径，不是文档的内容。** 这是整节的地基：库里存一份副本，
就一定会和磁盘上的真文档对不上，而"面板上显示的是三个月前的版本"这种错没人看得出来。
所以 `idea_docs` 只有 `path`，正文在 `.../content` 这个端点读的时候现读。

| 端点 | 说明 |
| --- | --- |
| `GET /api/ideas/{id}/docs` | `{"docs":[…]}`，每条带 `exists` / `size` / `modified_at`（来自 `os.stat`） |
| `POST /api/ideas/{id}/docs` | `{path, title?}` → **201**；`title` 省略时用文件名（去后缀） |
| `GET /api/ideas/{id}/docs/{doc_id}/content` | 返回 `text` 与 `truncated` |
| `DELETE /api/ideas/{id}/docs/{doc_id}` | 删引用，**不动磁盘上的文件** |

契约要点：

- **两种"不存在"必须分开**：文档记录没有 → **404**（调用方 id 写错了）；文件被人在
  外面删了 → **200 且 `exists:false`**、`text` 为空串。合在一起的话，"文档被人删了"
  会显示成接口坏了，而引用其实还在、还能补回来。
- **只收 `.md` / `.markdown` / `.mdx` / `.txt`**。这不是安全边界（面板绑 `127.0.0.1`，
  本地进程都打得进来），是防止手滑把 `.env`、密钥之类挂进面板。
- **单篇上限 512 KB**，超出截断并置 `truncated:true`，**不报错**——大文档不该让抽屉打不开。
- **解码顺序 utf-8 → gbk → utf-8/replace**。本机的文档不一定是 UTF-8，
  直接 `decode("utf-8")` 会在 GBK 中文文档上抛 `UnicodeDecodeError` 变成 500；
  宁可给一段带替换符的文本，也不要整个面板挂掉。
- **判重按归一化后的绝对路径**：`docs/a.md` 与它的绝对路径是同一篇，挂两次报
  「这份文档已经挂在这张卡上了」。否则清单里会出现两条一模一样的东西。
- **相对路径按进程当前工作目录解析**，即 `start.bat` 的启动目录（项目根）。
  所以 CLI 一律建议传绝对路径，并把归一化后的路径回显给调用方核对。
- **挂载/移除复用 `edited` event**，不新增 `events.kind`。`kind` 有 `CHECK` 约束，
  加一个 kind 要整表重建（连带 id 与外键），为一条时间线文案不值得。
- `GET /api/ideas/{id}` 的返回**多一个 `docs` 数组**（抽屉一次 GET 就能画全）；
  `GET /api/ideas` 的每条**多一个 `docs_count`**（看板角标要用，用一条 `GROUP BY` 批量取，
  不做 N+1）。`docs_count` 不是 `ideas` 表的列，别混进列集合的断言里。

### 5.9 项目主文件夹与 agent 对话名

`ideas` 表新增两列，**走 PATCH 白名单**（`store.FIELDS`），语义与既有字段完全一致：
传 `null` → 422，要清空就传 `""`。

| 字段 | 记什么 |
| --- | --- |
| `project_dir` | 作品真正待着的工作目录（代码库、素材目录） |
| `agent_session` | 这次 agent 对话的名字 |

粒度是**每张卡各存各的**——一个创意对应一个目录、一次对话；不存在"面板当前项目"
这种全局概念。若日后要默认值，做法是"卡片没填就回落到全局"，**不是**把全局值
写进每张卡（后者会让"改全局"变成一次批量写，且没有 trace）。

## 6. panel.py CLI

agent 的操作入口，是 REST API 的薄封装。输出给人看，不输出 JSON。

```bash
python panel.py new "<标题>" [--body 描述] [--tags a,b] [--source 来源]
                    [--project-dir <工作目录>] [--agent-session <对话名>]
python panel.py list [--status <态>] [--tag <标签>] [--q <关键词>] [--blocked]
python panel.py edit <id> [--title <标题>] [--body <描述>] [--tags a,b]
                    [--source <来源>] [--blocked-note <备注>] [--position <位次>]
                    [--project-dir <工作目录>] [--agent-session <对话名>]
python panel.py show <id>
python panel.py advance <id> <状态> [--note "这步做了什么"]
python panel.py note <id> "<消息>"
python panel.py block <id> "<卡住的原因>"
python panel.py unblock <id>
python panel.py stats
python panel.py delete <id>
python panel.py doc list <id>
python panel.py doc add <id> "<路径.md>" [--title <标题>]
python panel.py doc rm <id> <文档号>
python panel.py doc show <id> <文档号>
```

`edit` 打 `PATCH /api/ideas/{id}`，可改字段就是白名单那八个（见第 5 节），没有 `--status`——状态仍只能走 `advance`（5.1）。**没传的字段不进请求体**（PATCH 语义是"只有你传了的字段才改"），所以服务端那几个 422 分支（空对象、白名单外、null）在 CLI 上够不着——CLI 结构上发不出 JSON null，argparse 的一切都是文本。空对象只有 `edit <id>` 一个字段都不给时才会发生，服务端回「没有可更新的字段」，CLI 原样透传。`--body ""` 是**清空描述**，与不传是两件事：CLI 靠 argparse 的默认值（没传 = `None`，传空串 = `""`）区分，判据必须是 `is not None` 而不是真值判断。

> **Windows / PowerShell 5.1 上清空字段要写 `--body=`（等号紧贴、后面什么都没有）**，不能写 `--body ""`：PS 5.1 会把空字符串参数整个吃掉，于是它变成"没给这个选项"，argparse 报 `expected one argument`。cmd 与 bash 不受影响。

**`new` 的 `project_dir` / `agent_session` 同样走 `is not None` 判据**：不带这两个选项时 argparse 给的是 `None`，直接透传会让服务端的 null 守卫拒掉**整条请求**（新建直接失败），而这里本来只想表达"没填"。

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

**亮色纸面，冷调**（不是暖米色）。彩色只在表达状态时出现。

| 用途 | 值 |
|---|---|
| 应用底 | `#EDF0F4` |
| 面板 / 列 | `#F6F8FA` |
| 卡片 | `#FFFFFF` |
| 抬升 / hover | `#F9FBFC` |
| 边框 | `#DBE1E9` |
| 轨道线（未到） | `#DBE1E9` |
| 主文字 | `#15181D` |
| 次文字 | `#69717E` |

> 早期版本这里是 SnowLuma 的**暗色** oklch 令牌，是实现时从参考 app 外推的、用户并未要求的
> 选择，实际用起来被否掉了。暗色不再是契约的一部分。

### 8.2 状态色

色序语义不变：灰 → 冷 → 冷紫 → 暖 → 绿，色温从冷走到暖再收。amber 用于"制作中"是热度而非警告；红色全部留给阻塞。**明度按亮色底重新调过**，暗底那套直接搬过来会糊在白卡片上。

| 状态 | 色 | 理由 |
|---|---|---|
| `idea` 仅有创意 | 中性灰 `#8A93A2` | 没方向就该是灰的 |
| `clarifying` 正在明确 | sky `#2E86C8` | 在探索，冷 |
| `clarified` 已经明确 | violet `#7B4DD6` | 成形了，接品牌紫 |
| `building` 正在制作 | amber `#D4860B` | 在干活，是热的 |
| `done` 完成成品 | emerald `#1B9A67` | 绿，收尾 |
| 阻塞 | red `#D33F35` | 仅作用于当前态站点与卡片描边 |

### 8.3 签名元素：流转轨（**横向地铁线**）

流转轨是卡片顶部一条**横向**的线，线上五站，一站对应一个状态。创意就是那趟车，线往右走。

- 轨道线本身是**连续的**：五格各画一段、首尾相接，线不伸出两端
- 索引 `< i`（已过）→ **实心圆点 + 该段线**，用**它自己那一态**的颜色
- 索引 `== i`（当前）→ **大一号的实心点，外圈留一圈卡片色的环**（把点从线上"抬"起来 = "你在这里"）
- 索引 `> i`（未到）→ **空心圆点 + 灰色段线**
- **每一站都必须有圆点元素**——地铁线的"站"是实体，缺了站就只剩一根线，读不出五站也读不出卡在第几站

一张 `building` 卡片（`i=3`）：

```
●━━━━━●━━━━━●━━━◉┈┈┈○
idea  clari  clari  build  done
灰     sky    violet  amber  未到
```

前 3 站实心、各用自己那态的颜色，当前站琥珀色大点带白环，最后一站空心。

**渲染完全按索引比较，不按"走过的路径"。** 跳跃推进时被跳过的态一律算「已过」：`idea` 直接到 `building`，前 3 站全实心、第 4 站是当前点、第 5 站空心。这符合线性有序语义。

**阻塞时不要把整条线涂红。** 整线变红会丢掉「走到哪一步」这个信息，正是流转轨存在的意义。正确做法：**只把当前站转红** + 给卡片加一道红色描边；已过的站保持各自颜色。

作用：不点开卡片就能看出它走到哪一步。这是本产品区别于通用任务板的核心识别特征。

### 8.4 布局

统计数字直接作为列头的 mono 大数字，不单独占一行。

**列头数字是「筛选后」的数量，不是总量。** 数字紧贴它描述的那一堆卡片，写 12 却只显示 3 张会让用户无法对账。总量的需求由 `panel.py stats`（GET `/api/stats`）满足，那是 CLI 的活，不是看板的。五列数字之和恒等于当前视图里的卡片总数——这条是对账不变量，改筛选逻辑时必须守住。

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

- **drop 时先比较源列与目标列**：相同 → `PATCH {position}`；不同 → `POST /advance {to, position}`。**不要在落回原列时发 advance**，否则会撞上 5.1 的 422，用户看到一个莫名其妙的错误
- 跨列拖拽一次 `advance` 完成，立刻写 `events`（actor=human）
- 点卡片 = 右侧抽屉：完整描述、标签、项目信息、项目文档、阻塞按钮、**完整时间线**
- 抽屉里的"推进"控件 = 目标状态选择器 + **一个可填可不填的备注框**。人手操作同样要能补一句，否则 human 路径写出的 events 全是空 message，时间线会很难读
- 拖动时显示落点占位块 `#0000001a`（沿用 SnowLuma 的做法）

### 8.5.1 抽屉：默认只读，可展开到全屏（2026-10-13 增补）

用户反馈原话：「首先应该可以展开到全屏，其次实在是太丑了，默认就是编辑模式，底下的时间线跟调试模式似的」。

**默认是只读的。** 原来每个字段都是「输入框 + 一个保存按钮」，满屏灰框——那不是详情页，是调试表单，看的人根本没得看。现在每个区块有「只读 / 编辑」两态，点区块标题右侧那个小字「编辑」才进编辑态，存完回只读。

- **推进与阻塞不是字段是动作**，常驻显示：它们本来就该一眼可见、可点。
- **项目的两个字段共用一次 PATCH**（`project_dir` + `agent_session` 一起发），一次只写一条 `edited` event，比分两次存多一条流水干净。
- **取消不发请求**：它只是把 `hidden` 切回去，所以不存在"取消了一半写进账本"。
- **保存后必须用服务端回填的值重绘**，不能拿本地输入值：服务端会规范化（去首尾空白），不回填就会出现「存了但看着没变」。

**时间线是活动流，不是日志表。** 左边一条竖线 + 一个点，主文本在上、时间与操作人在下。两条硬要求：

1. **不裸露字段名。** `edited` 的 message 是服务端拼的「修改了：body、tags」，服务端那边是 API 契约不能改中文，所以**翻译放在显示层**（`FIELD_CN`，与 `panel.py` 的 `FIELD_LABEL` 同一套词）。这条和"默认只读"是同一个病根：满屏 `body`/`tags` 正是"像调试模式"的另一半。
2. **状态跃迁渲染成状态芯片**（染各自的状态色），而不是「仅有创意 → 正在明确」一行纯文字——和看板列头、流转轨上的状态点说同一件事。

**全屏**（`.drawer.is-full`）：点右上角 `⤢` 展开，内容仍然收在 `max-width: 52rem` 的居中阅读列里——铺满 1280px 会让一行中文长到难换行，而区块头的「编辑」会远到看不见对应关系。`Esc` 逐级退出：**先退出全屏，再按一次才关抽屉**；一步关掉会让「我只是想看大图」变成丢上下文。关抽屉时会一并复位全屏态，否则下次打开是全屏的。

> **一条只有实测才会暴露的坑**：`.eb-edit` 设了 `display:flex`，而 HTML `[hidden]` 的 UA 样式 `display:none` 优先级更低、会被整条盖掉，于是「存完回只读」时 textarea 仍然可见，同一段内容在页面上出现两次。不报任何异常，只是"看起来没切换"。凡是给带 `hidden` 的元素设 `display`，都必须补一条 `[hidden] { display: none }`。

### 8.5.2 删除（2026-10-16 增补）

用户要求「要有删除创意的管理员功能」。查下来后端 `DELETE /api/ideas/{id}` 与 CLI `panel.py delete` 一直都有，**缺的只是网页入口**——在那之前卡片只能删、没法在面板上删。

- **位置**：抽屉最底部，独立 `.danger-zone` 区，上面一条分隔线。是小字链接而不是大红按钮：删除要不难找，但绝不能和「推进」这种高频动作长得像。
- **两步**：点它只是打开确认表（`#confirm`，一个 `<dialog>`），不是执行。这是全面板唯一不可撤销的操作。
- **确认表摆出"会丢什么"**：卡片标题、N 条时间线记录、M 篇项目文档的引用，逐条列；并明说**磁盘上的文档文件不会被删，只是解除引用**。不可撤销的操作不能只问一句"确定吗"——真删错了账本就少了一段，而面板没有回收站可捞。
- 确认后：关抽屉、刷新看板、提示落在工具条下的横幅（`drawerMsg` 在抽屉关着时会自动改投那里）。

> **两个只有实测才会暴露的坑**：
>
> 1. **`<dialog>` 的 `close()` 不清事件监听**，而 `#confirm` 是复用节点。第一次确认留下的监听会让**第二次确认被第一次的闭包直接 resolve 掉**——那就不是"取消"而是"删错卡"。所以收尾走一个 `answered` 守卫的幂等 `finish()`，并解绑三个监听。
> 2. **全局 Esc 只避让了 `composer`**（8.5.1 写的），没避让 `#confirm`。于是确认表开着时按 Esc，不只关掉确认表，**还把底下的抽屉一起关了**；而 `cancel` 事件在那种情况下又没正常走到，Promise 永远不 resolve，删除按钮就**卡在 `disabled` 再也点不动**。修法两处：全局 Esc 改成 `document.querySelector("dialog[open]")` 一律不插手；`confirmDelete` 监听 `close` 而不是 `cancel`（Esc / 算了 / 确认都会走 `close`，所以"没点确认就关掉了"一律判成取消）。

### 8.6 动效

只做一处：放下卡片时，流转轨的实心段从旧态滑到新态。其余一律不加。全部动效遵守 `prefers-reduced-motion`。

### 8.7 字体

- 显示体（列头大数字、标题）：`"Bahnschrift"` —— Windows 10+ 自带的 DIN 风格窄体 grotesk，自带宽度轴，做列头数字有工业味；中文回落雅黑
- 正文：`"Segoe UI"` / `"Microsoft YaHei UI"`，`.875rem / 1.5`
- 数字、ID 与时间戳：`"Cascadia Mono"` / `Consolas`
- **零网络字体**（与"零 CDN"是同一条硬约束，不是两个偏好）

### 8.8 形状

`radius = 10px`（输入框/按钮 6px，标签胶囊 999px），卡片阴影
`0 1px 2px rgba(21,32,51,.06), 0 4px 12px rgba(21,32,51,.04)`，`spacing` 基准 4px。

## 9. 错误处理

| 情况 | 行为 |
|---|---|
| 服务未启动 | CLI 打印「面板未启动，请双击 start.bat」，退出码 1 |
| 标题为空 | 422；CLI 打印「标题不能为空」 |
| 非法状态名 | 422；CLI 列出 5 个合法状态名 |
| advance 到当前状态 | 422；「该创意已经处于此状态」 |
| `status` 筛选值非法 | 422；「未知状态：foobar」 |
| PATCH 含白名单外的字段 | 422；指出是哪个字段，并提示状态须走 advance |
| PATCH 空对象 | 422；「没有可更新的字段」 |
| PATCH 字段传 `null` | 422；`字段名 不能为 null，请传空串`（带字段名前缀；**全字段无例外，含 `tags` 与 `position`**） |
| 提供了 tags 但规范化后全空 | 422；「标签不能为空」 |
| 字段传了非字符串（如 `{"title": 5}`） | 422；`中文字段名 必须是字符串`。**必须显式校验**——SQLite 的 TEXT 亲和性会把标量静默强转（`True` → `"1"`），不报错的数据损坏比 500 更糟。错误文案用**中文字段名**（如「排序位置」「描述」），不打原始键——CLI 的定位是"输出人话"。`position` 要求是数字（`bool` 也要拒），否则 TEXT 会排到 REAL 之后、破坏 `ORDER BY position`；`tags` 的文案是「标签必须是逗号分隔的字符串」 |
| `X-Panel-Actor` 值非法 | **不报错**，按缺省 `human` 记录（见 5.4） |
| **框架层校验错误**（`body` 非对象、`idea_id=abc`、缺 body、坏 JSON） | 422；**同样返回人话 `detail` 字符串**。由 `app.py` 的全局 `RequestValidationError` 处理器统一转换，**不得把 pydantic 的数组直接透出去**——下游（CLI 直接打印、前端 `throw new Error`）都假定它是字符串 |
| 筛选参数为空串（`?status=` 等） | 200，等同不过滤（见 5.3，空串不表达意图） |
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

**同步方向是单向的：以项目内的 `skill/` 为准，覆盖 `%USERPROFILE%\.minimax\skills\creative-panel\`。不要反过来直接改安装目录里的副本**——那份是安装产物，下次同步会被覆盖，改了等于没改（且改动丢失）。

**注意：Windows 的 bat 不展开 `~`**，脚本里必须写 `%USERPROFILE%` 而不是 `~/.minimax`。

`.gitignore` 忽略 `data/`。

## 11. 测试策略

`test_panel.py`，依赖 `pytest` + `httpx`。

**运行方式**：用 `tmp_path` fixture 建临时 db 文件，通过环境变量 `PANEL_DB` 让 `db.py` 指向它；在同一 fixture 里用 `uvicorn.Server` 起一个后台线程监听**随机空闲端口**，测试全部通过真实 HTTP 访问。端口用 `socket.bind(('', 0))` 取得后立即关闭再交给 uvicorn——存在 TOCTOU 窗口，但本机单进程测试下可接受，**不要改用固定端口**，否则无法与正在运行的面板并存。fixture 结束时关线程、删临时文件。**绝不使用 `data/panel.db`**。

覆盖：

1. 建创意 → 5 态全链路推进 → `events` 条数与顺序正确
2. `PATCH` 传 `status` 返回 422
3. `PATCH` 传白名单外字段返回 422；`PATCH {}` 返回 422；值未变仍写 `edited`
4. advance 到当前状态返回 422
5. 阻塞 / 解除阻塞各写一条 event；重复 `block` 覆盖 note 且再写一条；`unblock` 后 `blocked_note` 保留
6. 四维筛选：`status` / `tag` / `q` / `blocked`，含非法 `status` 返回 422
7. 删除级联删除 `events`（这条专门验证 `PRAGMA foreign_keys=ON` 真的生效）
8. advance 到 `done` 写 `done_at`，离开 `done` 清空
9. `PATCH` 多字段只写一条 `edited` event
10. tags 规范化：`"视频, 科普, "` → `",视频,科普,"`；`tag=科普` 命中、`tag=科` 不命中；全空白标签被拒
11. 排序稳定性：`position` 相同时按 `id` 升序
12. 空列中新建卡片 `position` 为 1（验 `COALESCE`，不撞 NOT NULL）
13. 任意一条 UPDATE 都刷新 `updated_at`
14. **建连后读回 `PRAGMA recursive_triggers` 断言为 0、`PRAGMA foreign_keys` 断言为 1**（4.3.1 的硬性前提，必须有回归锁）
15. CHECK 约束生效：绕过 API 直接写非法 `status` / `actor` / `kind` 被数据库拒绝
16. `PATCH {"body": null}` 返回 422
17. `X-Panel-Actor: robot` 不报错，`actor` 记为 `human`
18. 未提供 `tags` 存 `","`；提供 `"  ,  ,"` 返回 422
19. **`PATCH {"tags": null}` 与 `PATCH {"position": null}` 均返回 422 且不落库**（第 5.5 节的无例外 null 守卫，必须有回归锁）
20. **CLI `edit` 的 PATCH 语义**：`没传的字段不被改动`（反向断言，回读数据库而不是看 stdout）、`传空串是「清空」不是「不改」`、`--status` 不存在于 parser`（状态唯一入口是 `/advance`，见第 5.1 节）。**判据必须是 `is not None` 而不是真值判断**——写成 `if v:` 会让 agent 永远清不掉描述，而退出码 0、输出「已修改：描述」全都正常，只有回读账本才抓得到

目标：改后端后一条命令能确认没回归。

**前端没有自动化测试**，这是「无构建步骤 + 零 CDN」这条设计约束的直接后果，不是遗漏：本项目刻意不引入 JS 测试框架和打包器。代价要认——改 `web/` 不会有任何测试变红。所以前端的质量门是**真实浏览器实拍**：每个动前端的任务，实施者自测后由上层用真实浏览器起真服务端、喂真实数据渲染一次，至少确认——五列渲染出来、控制台零错误、零 404、除 `data:` favicon 外无任何非回环请求、筛选能来回切换。Task 9 的端到端验收会再走一遍完整交互。

## 12. 不做的事（YAGNI）

以下明确排除，留到真正需要时再说：

- 账号 / 权限系统（纯本机自用）
- 附件存储与**素材**在线预览（面板仍然不存素材文件本身；5.8 增补的只是**文档**——
  而且是"引用路径 + 现读现渲染"，不是把文件搬进库里）
- 多项目 / 多看板
- 状态自定义（5 态固定，线性，不做成可配置的 stage_defs）
- 富文本编辑器（`body` 用纯文本 + 换行）
- 服务端推送（前端轮询即可）
- 移动端适配（先保证桌面 1280px 以上可用）
- skill 自动调用（面板不会主动去跑别的 skill）

**将来很可能会回头做的**（现在明确不做，但记在这里免得重新讨论）：

- `GET /api/events?since=` 全局事件流。若以后想做"最近发生了什么"这类跨创意的视图，现在只能逐个 idea 取 events 拼装
- 列表接口的分页与排序：`?limit=` / `?offset=` / `?sort=`。现在一次拉全部，本机量级（几十条）没压力，但接口形态早晚要动
- 时间戳改为带时区偏移的格式。当前格式（4.3）不带偏移，跨设备或跨时区场景会出问题
- 卡片附件缩略图（要动存储模型，是另一个 spec 的事）
- 多设备同步（当前假设只有这一台机器）

## 13. 技术选型理由

- **FastAPI + SQLite + 原生 JS**：与 `media-center-pms` 同款，已在该机器上跑通并维护多轮，踩过的坑已知
- **CLI 封装而非裸 API**：`SKILL.md` 里教 agent 拼 JSON 会带来持续的失败率；CLI 输出人话，agent 只需读一行
- **零 CDN**：本机面板在无外网环境下也应可用
- **纯系统字体栈**：中文界面用系统中文字体是正确的工程选择，且保证离线