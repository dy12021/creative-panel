/* 创意面板前端 —— 数据层 + 五列看板 + 流转轨 + 抽屉与拖拽（Task 5 / 6 / 7）
 *
 * 边界（SPEC 第 2 节）：面板是账本，不是执行器。这里只读写 REST API，
 * 不调任何 skill，也没有任何"替我去执行"的动作。
 *
 * Task 7 接进来的：openDrawer() closeDrawer() moveCard()，以及拖拽事件。
 * 交接约束：slideRail 必须在 reload() 之前调用（render() 会整板重建 DOM）。
 */

const STATUS_ORDER = ["idea", "clarifying", "clarified", "building", "done"];
const STATUS_LABEL = {
  idea: "仅有创意", clarifying: "正在明确", clarified: "已经明确",
  building: "正在制作", done: "完成成品"
};

const state = {
  items: [],       // 当前筛选后的列表项（12 字段，不含 events，含 docs_count）
  tags: new Set(), // 标签并集，只增不减，见 renderTagFilter
  filter: { q: "", tag: "", blocked: false },
  openId: null
};

/* 抽屉里「项目文档」区块的临时状态。
 * 清单随抽屉重绘从 idea.docs 来（GET /api/ideas/{id} 一次就带 docs），
 * 正文按需取：cache[docId] 存已读回来的 content，重绘时不必再打一次请求。 */
const docsState = { ideaId: null, docs: [], open: null, cache: {}, host: null };

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  if (!r.ok) {
    // SPEC 9：detail 恒为人话字符串；未捕获的 500 是纯文本，走 catch。
    const d = await r.json().catch(() => null);
    const msg = d && typeof d.detail === "string" ? d.detail : null;
    throw new Error(msg || ("请求失败（HTTP " + r.status + "）"));
  }
  return r.json();
}

// 过期响应丢弃：连续搜索会并发多次请求，慢的那次先回就会覆盖快的那次结果
let loadSeq = 0;

// 标签全集来自**不带筛选**的那次请求。拿当前筛选结果来算是错的：选中一个标签后
// 选项会塌缩成只剩那一个，再也换不到别的（Task 5 的注释只防住了这一半）。
// 但只增不减的并集同样是错的——标签被删光之后下拉里还留着死选项，选中即空板。
// 两次都取：筛选结果喂看板，不过滤的那份喂下拉。
async function load() {
  const seq = ++loadSeq;
  const p = new URLSearchParams();
  if (state.filter.q) p.set("q", state.filter.q);
  if (state.filter.tag) p.set("tag", state.filter.tag);
  if (state.filter.blocked) p.set("blocked", "1");
  const [items, all] = await Promise.all([
    api("GET", "/api/ideas?" + p.toString()),
    api("GET", "/api/ideas"),
  ]);
  if (seq !== loadSeq) return;          // 已经有更新的请求在飞，丢掉这次
  state.items = items;
  // 每次重算而不是并集：并集只增不减，标签被删后下拉留死选项，选中即空板
  state.tags = new Set();
  all.forEach(i => i.tags.split(",").filter(Boolean).forEach(t => state.tags.add(t)));
  // 读成功了就把上一轮的失败横幅收掉，别让一条已经解决的错误一直挂着
  const b = document.getElementById("board-msg");
  if (b) b.hidden = true;
  render();
}

// 读数失败的兜底：不给的话，服务没起来就是一片空白 + 一条未捕获的 Promise 拒绝
function reload() { load().catch(showBootError); }

function showBootError(err) {
  const board = document.getElementById("board");
  board.textContent = "";
  const box = el("div", "boot-error");
  box.append(el("p", "boot-error-title", "读不到面板数据：" + err.message));
  box.append(el("p", "boot-error-hint", "确认服务已启动：双击 start.bat，或看 http://127.0.0.1:8472"));
  board.append(box);
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;   // 一律 textContent，不拼 innerHTML
  return n;
}

function render() {
  const board = document.getElementById("board");
  board.textContent = "";
  for (const st of STATUS_ORDER) {
    const col = el("section", "col");
    col.dataset.status = st;
    const head = el("div", "col-head");
    // 列内顺序 position -> id（SPEC 4.2；position 是浮点，id 是 tie-breaker）
    const items = state.items.filter(i => i.status === st)
      .sort((a, b) => a.position - b.position || a.id - b.id);
    head.append(el("span", "col-count", items.length));
    const dot = el("span", "col-dot");
    dot.style.setProperty("--c", "var(--st-" + st + ")");
    head.append(dot);
    head.append(el("span", "col-name", STATUS_LABEL[st]));
    col.append(head);

    const list = el("div", "col-list");
    items.forEach(i => list.append(card(i)));
    col.append(list);

    if (st === "idea") {
      const add = el("button", "col-add", "＋ 快捷登记");
      add.onclick = () => quickNew();
      col.append(add);
    }
    board.append(col);
  }
  renderTagFilter();
}

/* ------------------------------------------------------------- 流转轨（SPEC 8.3）
 *
 * 本产品区别于通用任务板的签名元素：卡片顶部一条**横向**的地铁线，五站对应五个状态。
 * **渲染完全按索引决定，不按"走过的路径"**——所以跳跃推进时（idea 直接到 building），
 * 被跳过的态一律算"已过"，前 3 站全是实心。走过的站用**它自己那一态的颜色**，
 * 整条线因此是一条状态光谱。
 *
 * 阻塞时**只**把当前站转红（卡片红描边由 .card.is-blocked 负责），
 * 绝不能把整条线涂红——那会丢掉"走到哪一步"，正是流转轨存在的意义。
 */

// 就地把一条轨重画成 status 的样子。抽出来是为了 slideRail 能"先量后改"：
// 同一个函数负责建轨和改态，滑动才不会因为走了另一条渲染路径而失效。
function paintRail(rail, status, blocked) {
  const idx = STATUS_ORDER.indexOf(status);
  rail.textContent = "";                     // 先清干净，避免重画时叠出两层格
  rail.dataset.status = status;
  STATUS_ORDER.forEach((st, i) => {
    // 未知状态名只会让 idx=-1，于是 5 站全判成 future 且没有当前圆点，
    // 不会抛异常；服务端本来就用白名单约束 status，这里只是兜底。
    const seg = el("span", "rail-seg " + (idx < 0 ? "future" : i < idx ? "past" : i === idx ? "now" : "future"));
    seg.dataset.status = st;
    seg.style.setProperty("--seg", "var(--st-" + st + ")");
    seg.title = STATUS_LABEL[st];
    // **每一站都要有圆点**：地铁线的"站"是实体，缺了站就只剩一根线，
    // 读不出"五站"、也读不出卡在第几站。走过的站实心（它自己那态的颜色）、
    // 没到的站空心，由 CSS 按 .past/.future 分化。
    const dot = el("span", "rail-dot");
    // 红色也只落在**当前站**身上（SPEC 8.2）
    dot.style.setProperty("--dot", blocked && i === idx ? "var(--blocked)" : "var(--st-" + st + ")");
    seg.append(dot);
    rail.append(seg);
  });
  return rail;
}

function buildRail(status, blocked) {
  return paintRail(el("div", "rail"), status, !!blocked);
}

/* SPEC 8.6：全局只做这一处动效——放下卡片时实心段从旧态滑到新态。
 *
 * 用 FLIP（First-Last-Invert-Play）而不是给 CSS 写死索引偏移：写死偏移等于把
 * "一格有多高、圆点落在格子的什么位置"这套布局知识复制到 JS 里，格高一变就飘。
 * 量旧位置 -> 改态 -> 量新位置 -> 反向位移归零，让 CSS 的 transition 去补间。
 *
 * 遵守 prefers-reduced-motion 靠 CSS：style.css 里那条 reduce 规则用
 * transition:none !important 把补间掐掉，位移仍然瞬间归零，视觉上就是直接改态。
 * 所以这里不需要（也不应该）自己再查一遍媒体查询。
 *
 * Task 7 的落卡路径用它：拿到卡片的 .rail，先 slideRail 再 reload。
 */
function slideRail(rail, toStatus, blocked) {
  const same = rail.dataset.status === toStatus;
  // 必须取**当前站**那个圆点。现在每站都有圆点了，querySelector(".rail-dot")
  // 会抓到第一站（永远在最左），量出来的位移恒为 0，动画直接失效。
  const dot = rail.querySelector(".rail-seg.now .rail-dot");
  // 轨现在是**横向**的一条地铁线，圆点是沿着 X 轴换站，所以量 left、位移也用 X。
  const fromLeft = dot ? dot.getBoundingClientRect().left : 0;
  paintRail(rail, toStatus, blocked);
  if (same) return;                      // 只是阻塞态翻转，没有位移可播
  const dot2 = rail.querySelector(".rail-seg.now .rail-dot");
  if (!dot2) return;                     // 目标态认不出来，没有可滑的圆点
  const dx = fromLeft - dot2.getBoundingClientRect().left;
  if (Math.abs(dx) < .5) return;                       // 没有实际位移，不播动画
  dot2.style.transition = "none";
  dot2.style.transform = "translateX(" + dx + "px)";
  void dot2.offsetHeight;                              // 强制回流，把反向位移先落定再放行
  dot2.style.transition = "";                          // 交回样式表里的 transition
  dot2.style.transform = "";
}

// 卡片本体。流转轨在卡片顶部横着一条，下面是内容。点开抽屉由 Task 7 接进来。
function card(i) {
  const c = el("article", "card");
  c.dataset.id = i.id;
  c.dataset.status = i.status;
  // Task 7：真拖拽。没有这一行 cursor:grab 是个空头支票，拖了没反应。
  c.draggable = true;
  c.append(buildRail(i.status, i.blocked));
  const body = el("div", "card-body");
  body.append(el("div", "card-title", i.title));
  const meta = el("div", "card-meta");
  meta.append(el("span", "card-id", "#" + i.id));
  // tags 存成 ",构图,动效,"（首尾包裹逗号，SPEC 4.2）。filter(Boolean)
  // 顺手掐掉两端空串，直接 split 会多出两个空标签芯片。
  i.tags.split(",").filter(Boolean).forEach(t => meta.append(el("span", "tag", t)));
  if (i.blocked) {                        // blocked 是整数 0/1，不是布尔
    c.classList.add("is-blocked");
    meta.append(el("span", "tag is-blocked-tag", "卡住"));
  }
  // 项目文档角标：不点开抽屉也知道这张卡挂了文档（list 里只有 docs_count）
  if (i.docs_count > 0) {
    const badge = el("span", "tag is-doc-tag", "📄 " + i.docs_count);
    badge.title = "挂着 " + i.docs_count + " 篇项目文档";
    meta.append(badge);
  }
  body.append(meta);
  c.append(body);
  c.addEventListener("click", () => {
    // 拖拽结束后浏览器还会补一个 click；那次不算"点卡片开抽屉"
    if (justDragged()) return;
    openDrawer(i.id);
  });
  return c;
}

// 标签下拉的选项：state.tags 在 load() 里由**不带筛选**的那次请求重算。
// 见 load() 的注释——并集只增不减会留死选项，用筛选结果算会塌缩，两个坑各踩一半。
function renderTagFilter() {
  const sel = document.getElementById("tag");
  const cur = state.filter.tag;           // 以 state 为准，不读 sel.value
  sel.textContent = "";
  sel.append(new Option("全部标签", ""));
  [...state.tags].sort().forEach(t => sel.append(new Option(t, t)));
  sel.value = cur;
}

/* ------------------------------------------------------------ 抽屉（SPEC 8.5）
 *
 * 点卡片 = 右侧抽屉：完整描述、标签编辑、阻塞按钮、**完整时间线**。
 *
 * 列表项 12 字段里**没有 events**（实测后端契约），所以抽屉必须单独取详情：
 * 拿列表项渲染时间线只会得到一个空 ol，而那看起来像"没有历史"，
 * 不是"接口没给"。
 */

/* 时间戳：'2026-10-12T15:18:32.123' -> '10-12 15:18'。
 * 中间那个 T 是 ISO 格式留下的，不去掉就不是给人看的（与 CLI 同一套口径）。
 * created_at 恒为该格式（SPEC 4.3），但仍做一次防御：万一哪天变了，
 * 显示原值总比显示 undefined 好。 */
function fmtTime(ts) {
  if (typeof ts !== "string" || ts.length < 16 || ts[10] !== "T") return ts || "";
  return ts.slice(5, 10) + " " + ts.slice(11, 16);
}

// actor / kind 的中文表。跟 panel.py 的 ACTOR/KIND 是同一套词，
// 人和 agent 看同一条时间线时不能一个说"你"一个说"human"。
const ACTOR_LABEL = { human: "人", agent: "agent", system: "系统" };
const KIND_LABEL = {
  created: "创建", status: "推进", note: "备注",
  blocked: "标记卡住", unblocked: "解除阻塞", edited: "修改"
};

/* 抽屉内的一条提示。**抽屉关着的时候不能往里面塞消息**——那等于把错误
 * 藏在一个用户看不见的元素里。SPEC 8.5 点名的那类 422（落回原列误发
 * advance）恰恰发生在纯拖拽场景，此时抽屉多半是关着的：所以关着时改投
 * 工具条下方一条横幅，用户才看得到自己拖失败了。 */
function drawerMsg(text, kind) {
  let m = document.getElementById("drawer-msg");
  if (!text) {
    if (m) m.hidden = true;
    const b = document.getElementById("board-msg");
    if (b) b.hidden = true;
    return;
  }
  const drawerOpen = !document.getElementById("drawer").hidden;
  if (!drawerOpen) {
    let b = document.getElementById("board-msg");
    if (!b) {
      b = el("div", "board-msg");
      b.id = "board-msg";
      document.getElementById("board").before(b);
    }
    b.textContent = text;
    b.className = "board-msg" + (kind ? " is-" + kind : "");
    b.hidden = false;
    return;
  }
  if (!m) {
    m = el("div", "drawer-msg");
    m.id = "drawer-msg";
    document.getElementById("drawer-body").prepend(m);
  }
  m.textContent = text;
  m.className = "drawer-msg" + (kind ? " is-" + kind : "");
  m.hidden = false;
}

async function openDrawer(id) {
  state.openId = id;
  const d = document.getElementById("drawer");
  d.hidden = false;
  const body = document.getElementById("drawer-body");
  body.textContent = "";
  let idea;
  try {
    idea = await api("GET", "/api/ideas/" + id);
  } catch (e) {
    body.append(el("p", "drawer-error", "打不开：#" + id + " " + e.message));
    return;
  }
  if (state.openId !== id) return;        // 期间用户已经点了别的卡
  drawDrawer(id, idea);
}

/* ------------------------------------------------------------ 分区与字段块
 *
 * 抽屉**默认是只读的**（用户 2026-10-13：「默认就是编辑模式，底下的时间线跟
 * 调试模式似的」）。原来每个字段都是输入框 + 一个保存按钮，满屏灰框——那不是
 * 详情页，是调试表单，看的人根本没得看。现在每个区块有「只读 / 编辑」两态，
 * 点标题右侧的编辑才进编辑态，存完回只读。
 *
 * 推进与阻塞**不是字段是动作**，常驻显示是对的：它们本来就该一眼可见、可点。
 */

function section(parent, title, action) {
  const sec = el("section", "sec");
  const head = el("div", "sec-head");
  head.append(el("h3", "sec-h", title));
  if (action) head.append(action);
  const slot = el("div", "sec-slot");
  sec.append(head, slot);
  parent.append(sec);
  return slot;
}

function miniBtn(text, title) {
  const b = el("button", "mini", text);
  b.type = "button";
  if (title) b.title = title;
  return b;
}

/* 只读 <-> 编辑 的字段块。
 *
 * 保存回调拿到的是**服务端回填后的值**再切回只读：服务端会规范化（去首尾空白），
 * 不回填就会出现「存了但看着没变」，而这个错只有在回读账本时才抓得到。
 */
function editable(host, value, renderView, makeEditor) {
  const view = el("div", "eb-view");
  const edit = el("div", "eb-edit");
  edit.hidden = true;
  host.append(view, edit);
  renderView(view, value);
  function show() { edit.hidden = true; view.hidden = false; }
  return {
    show,
    view,
    toEdit() {
      edit.textContent = "";
      makeEditor(edit, value, v => {
        value = v;
        // 必须先清空再渲染：只读态的 renderView 一律是 append（它只被调用一次
        // 时没问题），存完再调一次就会把旧内容留在上面——于是同一个 view 里
        // 叠了两三段 .body-text，页面上看着像存了两次。
        view.textContent = "";
        renderView(view, v);
        show();
      });
      view.hidden = true;
      edit.hidden = false;
    },
  };
}

/* 两个保存按钮：主次分明。「取消」等价于保存原值——它只是切回只读，
 * 不发请求，所以不会有"取消了一半写进账本"这种事。 */
function editActions(saveFn, cancelFn) {
  const bar = el("div", "eb-actions");
  const save = el("button", "primary", "保存");
  save.type = "button";
  save.onclick = saveFn;
  const cancel = el("button", "secondary", "取消");
  cancel.type = "button";
  cancel.onclick = cancelFn;
  bar.append(save, cancel);
  return bar;
}

/* ------------------------------------------------------------------ 时间线
 *
 * 原来是「时间 | actor | 文本」的三列网格加分隔线，读起来就是一张日志表。
 * 现在做成活动流：左边一条竖线 + 一个点，主文本在上、时间与操作人在下。
 *
 * `edited` 的 message 是服务端拼的「修改了：body、tags」——**字段名裸露**，
 * 这正是"像调试模式"的另一半原因。服务端那边是 API 契约不能改中文，所以
 * 翻译放在显示层（FIELD_CN 与 panel.py 的 FIELD_LABEL 同一套词）。
 */
const FIELD_CN = {
  title: "标题", body: "描述", tags: "标签", source: "来源",
  blocked_note: "阻塞备注", position: "排序位置",
  project_dir: "项目主文件夹", agent_session: "agent 对话名",
};

function prettyEdited(msg) {
  const m = /^修改了：(.*)$/.exec(msg || "");
  if (!m) return "改了内容";
  const names = m[1].split("、").map(x => FIELD_CN[x] || x);
  return "改了" + names.join("、");
}

function statusChip(key) {
  if (!key) return el("span", "chip chip-none", "—");
  return el("span", "chip chip-" + key, STATUS_LABEL[key] || key);
}

function eventLine(e) {
  const li = el("li", "tl-item tl-" + e.kind);
  li.append(el("span", "tl-dot"));
  const main = el("div", "tl-main");
  const line = el("div", "tl-line");
  if (e.kind === "status") {
    line.append(statusChip(e.from_status), el("span", "tl-arrow", "→"),
                statusChip(e.to_status));
    if (e.message) line.append(el("span", "tl-note", e.message));
  } else if (e.kind === "created") {
    line.append(el("span", "tl-what", "创建了这张卡"));
  } else if (e.kind === "edited") {
    line.append(el("span", "tl-what", prettyEdited(e.message)));
  } else {
    line.append(el("span", "tl-what",
      (KIND_LABEL[e.kind] || e.kind) + (e.message ? "：" + e.message : "")));
  }
  main.append(line, el("div", "tl-meta",
    fmtTime(e.created_at) + " · " + (ACTOR_LABEL[e.actor] || e.actor)));
  li.append(main);
  return li;
}

function drawDrawer(id, idea) {
  const body = document.getElementById("drawer-body");
  body.textContent = "";

  body.append(el("h2", "drawer-title", idea.title));
  const sub = el("div", "drawer-sub");
  sub.append(el("span", "drawer-id", "#" + idea.id));
  sub.append(el("span", "drawer-status chip-" + idea.status,
                STATUS_LABEL[idea.status] || idea.status));
  if (idea.source) sub.append(el("span", "drawer-source", idea.source));
  body.append(sub);

  /* --- 描述 ---------------------------------------------------------------- */
  const descBtn = miniBtn("编辑", "改描述");
  const desc = editable(section(body, "描述", descBtn), idea.body || "",
    (view, v) => {
      if (v) view.append(el("div", "body-text", v));
      else view.append(el("p", "field-empty", "还没写描述。点「编辑」补上。"));
    },
    (host, value, done) => {
      const ta = el("textarea", "drawer-body-edit");
      ta.rows = 3;
      ta.value = value;
      ta.placeholder = "为什么想做、给谁看、要什么效果";
      // 自动增高：先归 auto 才量得到真实内容高，否则只会越加越长。
      const grow = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };
      ta.addEventListener("input", grow);
      grow();
      const bar = editActions(async () => {
        const s = bar.querySelector(".primary");
        s.disabled = true;
        try {
          const got = await api("PATCH", "/api/ideas/" + id, { body: ta.value });
          drawerMsg("描述已保存", "ok");
          await load();
          done(got.body == null ? "" : got.body);
        } catch (e) { s.disabled = false; drawerMsg(e.message, "bad"); }
      }, () => done(value));
      host.append(ta, bar);
      ta.focus();
    });
  descBtn.onclick = () => desc.toEdit();

  /* --- 标签 ---------------------------------------------------------------- */
  const tagBtn = miniBtn("编辑", "改标签");
  const tagView = el("div", "chip-row");
  const renderTags = v => {
    tagView.textContent = "";
    const list = String(v || "").split(",").filter(Boolean);
    if (!list.length) tagView.append(el("span", "field-empty", "没有标签"));
    else list.forEach(t => tagView.append(el("span", "chip", t)));
  };
  renderTags(idea.tags);
  section(body, "标签", tagBtn).append(tagView);
  tagBtn.onclick = () => {
    const host = el("div", "eb-edit");
    tagView.replaceWith(host);
    const inp = el("input", "eb-input");
    inp.value = String(idea.tags || "").split(",").filter(Boolean).join(",");
    inp.placeholder = "逗号分隔，留空即无标签";
    const bar = editActions(async () => {
      const s = bar.querySelector(".primary");
      s.disabled = true;
      try {
        const got = await api("PATCH", "/api/ideas/" + id, { tags: inp.value });
        drawerMsg("标签已保存", "ok");
        await load();
        renderTags(got.tags);
        host.replaceWith(tagView);
      } catch (e) { s.disabled = false; drawerMsg(e.message, "bad"); }
    }, () => host.replaceWith(tagView));
    host.append(inp, bar);
    inp.focus();
  };

  /* --- 项目：两个字段一次存 --------------------------------------------------
   * 共用一个保存按钮而不是各配一个：它们本来就是一件事（这件作品在哪、哪次对话
   * 在推），一次 PATCH 只写一条 edited event，比分两次存多一条流水干净。 */
  const projBtn = miniBtn("编辑", "改项目文件夹与对话名");
  const projView = el("div", "proj-view");
  const PROJ_FIELDS = [
    ["project_dir", "项目主文件夹", "D:\\coding\\我的作品", true],
    ["agent_session", "agent 对话名", "与 agent 的对话名", false],
  ];
  const renderProj = vals => {
    projView.textContent = "";
    PROJ_FIELDS.forEach(([key, label, , mono]) => {
      const v = vals[key] || "";
      const row = el("div", "proj-item");
      row.append(el("span", "proj-label", label));
      row.append(v ? el("span", "proj-value" + (mono ? " is-mono" : ""), v)
                   : el("span", "proj-value is-empty", "未填写"));
      projView.append(row);
    });
  };
  renderProj(idea);
  section(body, "项目", projBtn).append(projView);
  projBtn.onclick = () => {
    const host = el("div", "eb-edit");
    projView.replaceWith(host);
    const inputs = {};
    PROJ_FIELDS.forEach(([key, label, ph]) => {
      const row = el("label", "proj-row");
      row.append(el("span", "proj-label", label));
      const inp = el("input", "proj-input");
      inp.value = idea[key] || "";
      inp.placeholder = ph;
      inp.autocomplete = "off";
      inputs[key] = inp;
      row.append(inp);
      host.append(row);
    });
    const bar = editActions(async () => {
      const s = bar.querySelector(".primary");
      s.disabled = true;
      try {
        // 清空靠传空串：传 null 服务端 422（"… 不能为 null，请传空串"）。
        const payload = {};
        Object.keys(inputs).forEach(k => { payload[k] = inputs[k].value; });
        const got = await api("PATCH", "/api/ideas/" + id, payload);
        drawerMsg("项目信息已保存", "ok");
        await load();
        renderProj(got);
        host.replaceWith(projView);
      } catch (e) { s.disabled = false; drawerMsg(e.message, "bad"); }
    }, () => host.replaceWith(projView));
    host.append(bar);
    inputs.project_dir.focus();
  };

  /* --- 项目文档 ------------------------------------------------------------- */
  const addBtn = miniBtn("＋ 挂载", "挂一篇文档");
  const docsSlot = section(body, "项目文档", addBtn);
  syncDocs(id, idea.docs);
  const docsHost = el("div", "docs-host");
  docsHost.id = "docs-host";
  docsSlot.append(docsHost);
  drawDocs(docsHost, id, true, false);  // 挂载表单默认收起：只读态不该摆两个空输入框
  let addForm = null;
  addBtn.onclick = () => {
    if (addForm) {                       // 再点一次 = 收起
      addForm.remove();
      addForm = null;
      return;
    }
    addForm = el("div");
    docsHost.append(addForm);           // drawDocs 重绘 host 时它会被清掉，重新点即可
    drawDocs(addForm, id, false, true);
  };

  /* --- 动作：推进 ------------------------------------------------------------ */
  const actSlot = section(body, "推进");
  const form = el("div", "advance-form");
  const sel = el("select", "advance-sel");
  STATUS_ORDER.forEach(s => sel.append(new Option(STATUS_LABEL[s], s, false, s === idea.status)));
  const note = el("input", "advance-note");
  note.type = "text";
  note.placeholder = "这步做了什么（可留空）";
  const go = el("button", "primary", "推进");
  go.onclick = async () => {
    if (sel.value === idea.status) {      // 对齐服务端 422，但不发请求（SPEC 5.1）
      drawerMsg("已经处于「" + STATUS_LABEL[idea.status] + "」", "warn");
      return;
    }
    go.disabled = true;
    try {
      // 关键：body 字段名是 to，不是 status（传错不报错，只是功能坏）
      await api("POST", "/api/ideas/" + id + "/advance",
                { to: sel.value, note: note.value });
      await load();
      const got = await api("GET", "/api/ideas/" + id);
      drawerMsg("");
      if (state.openId === id) drawDrawer(id, got);
    } catch (e) {
      drawerMsg(e.message, "bad");
    } finally {
      go.disabled = false;
    }
  };
  form.append(sel, note, go);
  actSlot.append(form);

  /* --- 动作：阻塞 ------------------------------------------------------------ */
  const blockSlot = section(body, "阻塞");
  const blockForm = el("div", "block-form");
  const reasonInput = el("input", "block-input");
  reasonInput.id = "block-reason";
  reasonInput.placeholder = "卡在哪了？";
  reasonInput.autocomplete = "off";
  const bBtn = el("button", (idea.blocked ? "secondary" : "warn") + " block-btn",
                  idea.blocked ? "解除阻塞" : "标记卡住");
  if (idea.blocked) {
    blockForm.append(bBtn);
  } else {
    reasonInput.addEventListener("keydown", e => { if (e.key === "Enter") bBtn.click(); });
    blockForm.append(reasonInput, bBtn);
  }
  bBtn.onclick = async () => {
    bBtn.disabled = true;
    try {
      if (idea.blocked) {
        await api("POST", "/api/ideas/" + id + "/unblock");
      } else {
        // 阻塞原因同样是页面内输入，不用 window.prompt（同一个反模式）。
        const r = document.getElementById("block-reason").value.trim();
        if (!r) { drawerMsg("阻塞原因不能为空", "warn"); return; }
        await api("POST", "/api/ideas/" + id + "/block", { reason: r });
      }
      await load();
      const got = await api("GET", "/api/ideas/" + id);
      drawerMsg("");
      if (state.openId === id) drawDrawer(id, got);
    } catch (e) {
      drawerMsg(e.message, "bad");
    } finally {
      bBtn.disabled = false;
    }
  };
  blockSlot.append(blockForm);
  // 5.6：blocked_note 只是当前快照，只在真的卡住时渲染，
  // 否则会出现"没卡住却挂着旧原因"的错觉
  if (idea.blocked) {
    blockSlot.append(el("p", "blocked-note", "卡住原因：" + (idea.blocked_note || "（没写）")));
  }

  /* --- 时间线 --------------------------------------------------------------- */
  const tlSlot = section(body, "时间线");
  const tl = el("ol", "timeline");
  (idea.events || []).forEach(e => tl.append(eventLine(e)));
  if (!(idea.events || []).length) tl.append(el("li", "tl-empty", "还没有记录"));
  tlSlot.append(tl);

  /* --- 危险区：删除 ----------------------------------------------------------
   * 放在最后、单独用一条分隔线隔开。它是个小字链接而不是大红按钮：
   * 删除要**不难找但不难误触**，和「推进」那种高频动作摆在一起会出事。
   */
  const danger = el("div", "danger-zone");
  const delBtn = el("button", "danger-link", "删除这条创意");
  delBtn.type = "button";
  delBtn.onclick = async () => {
    delBtn.disabled = true;
    try {
      if (!await confirmDelete(idea)) return;      // 取消：什么都不做
      await api("DELETE", "/api/ideas/" + id);
      closeDrawer();
      await load();
      // 抽屉已经关了，drawerMsg 会落到工具条下的横幅上（见该函数注释）
      drawerMsg("已删除「" + idea.title + "」", "ok");
    } catch (e) {
      drawerMsg(e.message, "bad");
    } finally {
      delBtn.disabled = false;
    }
  };
  danger.append(delBtn);
  body.append(danger);
}

/* 删除确认，返回 Promise<boolean>。
 *
 * 把"会丢什么"逐条列出来，而不是只问一句"确定吗"：用户点确认前必须看清楚
 * 标题对不对、知道时间线有多少条。不可撤销的操作不能只靠一个"确定吗"——
 * 真删错了，账本就少了一段，而面板没有回收站可捞。
 */
function confirmDelete(idea) {
  const dlg = document.getElementById("confirm");
  document.getElementById("confirm-title").textContent = idea.title;

  const list = document.getElementById("confirm-list");
  list.textContent = "";
  const evN = (idea.events || []).length;
  const docN = (idea.docs || []).length;
  const items = [];
  if (evN) items.push(evN + " 条时间线记录");
  if (docN) items.push(docN + " 篇项目文档的引用");
  if (!items.length) items.push("（这条还没有历史记录，也没有挂文档）");
  items.forEach(t => list.append(el("li", null, t)));

  return new Promise(resolve => {
    const ok = document.getElementById("confirm-ok");
    const no = document.getElementById("confirm-cancel");
    // 收尾统一走 finish，且**幂等**：<dialog> 的 close() 不清监听，而 #confirm
    // 是复用节点，残留的监听会让第二次确认被第一次的闭包直接 resolve 掉——
    // 那就是"删错了卡"，所以这个 guarded 收尾不能省。
    let answered = false;
    function finish(v) {
      if (answered) return;
      answered = true;
      ok.removeEventListener("click", onOk);
      no.removeEventListener("click", onNo);
      dlg.removeEventListener("close", onClose);
      if (dlg.open) dlg.close();
      resolve(v);
    }
    function onOk() { finish(true); }
    function onNo() { finish(false); }
    // 监听 close 而不是 cancel：Esc、点「算了」、点确认都会走 close，
    // 所以"没点确认就关掉了"一律判成取消，不会出现 Promise 悬着、
    // 删除按钮永远卡在 disabled 的情况。
    function onClose() { finish(false); }
    ok.addEventListener("click", onOk);
    no.addEventListener("click", onNo);
    dlg.addEventListener("close", onClose);
    dlg.showModal();
  });
}

/* ------------------------------------------------------------ 项目文档（SPEC 10）
 *
 * 清单的增删只打 /api/ideas/{id}/docs（GET 回清单），不重 GET 整条卡片——
 * 抽屉里别的地方不需要跟着变，整块重绘会把用户正在填的输入框清空。
 */
function syncDocs(ideaId, docs) {
  if (docsState.ideaId !== ideaId) {
    docsState.ideaId = ideaId;
    docsState.open = null;
    docsState.cache = {};
  }
  docsState.docs = docs || [];
  // 预览开着的那条被移除了：收起，否则会渲染出一段没有归属的预览
  if (docsState.open !== null && !docsState.docs.some(d => d.id === docsState.open)) {
    docsState.open = null;
  }
}

async function refreshDocs(ideaId, host) {
  const r = await api("GET", "/api/ideas/" + ideaId + "/docs");
  syncDocs(ideaId, r.docs);
  await load();                           // 看板卡片的文档角标
  if (state.openId === ideaId && host) drawDocs(host, ideaId, true, false);
}

/* 字节数 -> 人话。与 CLI 同一套口径（KB 保留一位、不到 1 KB 直接给字节）。 */
function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

async function openDoc(ideaId, docId, host) {
  if (docsState.open === docId) {         // 再点一次 = 收起
    docsState.open = null;
    drawDocs(host, ideaId, true, false);
    return;
  }
  docsState.open = docId;
  drawDocs(host, ideaId, true, false);   // 先把「读取中…」画出来
  let c = docsState.cache[docId];
  if (!c) {
    try {
      c = await api("GET", "/api/ideas/" + ideaId + "/docs/" + docId + "/content");
      // 文件不在原处的不缓存：人把文件放回去之后要能一刷新就读到
      if (c.exists) docsState.cache[docId] = c;
      else docsState.cache[docId] = null;
    } catch (e) {
      c = { error: e.message };
      docsState.cache[docId] = c;
    }
  }
  if (state.openId === ideaId && docsState.open === docId) drawDocs(host, ideaId, true, false);
}

function docPreview(doc) {
  const box = el("div", "doc-preview");
  const c = docsState.cache[doc.id];
  if (!c) { box.append(el("p", "doc-note", "正在读取…")); return box; }
  if (c.error) { box.append(el("p", "doc-note is-bad", c.error)); return box; }
  if (!c.exists) {
    // 200 + exists:false：文件被人在外面删了。引用留着（那是用户的账本），
    // 但要说清现状，不能显示成一片空白让人以为接口坏了。
    box.append(el("p", "doc-note is-warn",
      "文件已不在原处。引用仍然留着，确认后可以点「移除」把它从卡片上摘掉。"));
    return box;
  }
  if (c.truncated) box.append(el("p", "doc-note is-warn", "内容较长，只显示前 512 KB"));
  const md = el("div", "md");
  // 全站唯一一处 innerHTML。mdToHtml 先整体转义再格式化，用户文档里的
  // 原始字符不可能原样进 DOM——这是硬要求，不是风格选择。
  md.innerHTML = mdToHtml(c.text || "");
  box.append(md);
  return box;
}

/* 清单与挂载表单分开渲染：只读态不该在清单下面常年摆两个空输入框。
 * withList / withForm 决定画哪一半；表单增删成功后只重画清单那份 host，
 * 否则会把用户正填的路径连同表单一起冲掉。 */
function drawDocs(host, ideaId, withList, withForm) {
  host.textContent = "";
  const docs = docsState.docs || [];

  if (withList !== false) {
  docsState.host = host;
  const list = el("div", "doc-list");
  if (!docs.length) {
    list.append(el("p", "doc-empty", "还没挂文档。把 SPEC 或 README 的路径挂上来，随时在这里看。"));
  }
  docs.forEach(d => {
    const item = el("div", "doc-item" + (docsState.open === d.id ? " is-open" : ""));
    const open = el("button", "doc-open");
    open.type = "button";
    open.append(el("span", "doc-title", d.title || "（无标题）"));
    open.append(el("span", "doc-path", d.path));   // 路径给等宽字体，逐字对齐好核对
    const bits = [];
    if (!d.exists) bits.push("文件已不在原处");
    else {
      if (d.size != null) bits.push(fmtSize(d.size));
      if (d.modified_at) bits.push("改于 " + fmtTime(d.modified_at));
    }
    open.append(el("span", "doc-meta", bits.join(" · ")));
    open.onclick = () => openDoc(ideaId, d.id, host);
    item.append(open);

    const rm = el("button", "secondary doc-remove", "移除");
    rm.type = "button";
    rm.onclick = async () => {
      rm.disabled = true;
      try {
        await api("DELETE", "/api/ideas/" + ideaId + "/docs/" + d.id);
        delete docsState.cache[d.id];
        if (docsState.open === d.id) docsState.open = null;
        drawerMsg("");
        await refreshDocs(ideaId, host);
        drawerMsg("已移除「" + (d.title || d.path) + "」", "ok");
      } catch (e) {
        rm.disabled = false;
        drawerMsg(e.message, "bad");
      }
    };
    item.append(rm);
    if (docsState.open === d.id) item.append(docPreview(d));
    list.append(item);
  });
  host.append(list);
  } // withList

  if (withForm === true) {
  const form = el("div", "doc-add");
  const pIn = el("input", "doc-add-path");
  pIn.type = "text";
  pIn.placeholder = "D:\\coding\\作品\\SPEC.md";
  pIn.autocomplete = "off";
  const tIn = el("input", "doc-add-title");
  tIn.type = "text";
  tIn.placeholder = "标题（可留空）";
  tIn.autocomplete = "off";
  const add = el("button", "secondary doc-add-btn", "挂载");
  add.type = "button";
  add.onclick = async () => {
    const p = pIn.value.trim();
    if (!p) { drawerMsg("文档路径不能为空", "warn"); return; }
    const payload = { path: p };
    const t = tIn.value.trim();
    if (t) payload.title = t;            // 省略 title，服务端用文件名兜底
    add.disabled = true;
    try {
      await api("POST", "/api/ideas/" + ideaId + "/docs", payload);
      pIn.value = "";
      tIn.value = "";
      drawerMsg("");
      // 重画**清单**那一份，不重画自己：否则刚清空的表单连同刚成功的提示一起没了
      await refreshDocs(ideaId, docsState.host);
      drawerMsg("已挂载「" + (t || p) + "」", "ok");
    } catch (e) {
      drawerMsg(e.message, "bad");       // 例如「这份文档已经挂在这张卡上了」
      add.disabled = false;
    }
  };
  form.append(pIn, tIn, add);
  host.append(form);
  } // withForm
}

/* ------------------------------------------------------------ markdown 渲染器
 *
 * 自己写、零依赖（SPEC 12：不引任何 CDN 与库）。
 *
 * **安全顺序是硬要求：先把整段文本做 HTML 转义，再做格式化。**
 * 反过来（先格式化再转义）会让用户文档里的原始字符有机会落进 innerHTML；
 * 只有反过来才可能把标记当数据、把数据当标记，两者的信任边界正好相反。
 * 转义之后 ``*``、``|``、``#`` 这些标记原样保留，格式化照常工作。
 *
 * 支持范围够用即可：ATX 标题、围栏代码块、行内代码、粗体、斜体、删除线、
 * 有序/无序列表、引用、分隔线、表格。表格是刚需——项目的 SPEC 大量用表格。
 * 链接只放行 http/https/mailto 与相对地址（挡 javascript: 伪协议）。
 * 图片不渲染成 <img>：本地路径 + 无 CSP 白名单会变成一串坏图，降级成 alt 文字。
 */

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// 只放行这三种带协议头的地址；其余（javascript: / data: / vbscript: …）降级成纯文本。
const MD_OK_SCHEME = /^(?:https?:|mailto:)/i;

function isSafeUrl(u) {
  // 先去掉控制字符再判协议：浏览器解析 URL 时会吞掉 \t \n 这类字符，
  // 「java\tscript:alert(1)」按明面看是没有协议头的。
  const probe = String(u).replace(/[\u0000-\u0020\u007f-\u009f]/g, "");
  if (/^[a-z][a-z0-9+.-]*:/i.test(probe)) return MD_OK_SCHEME.test(probe);
  return true;                            // 相对地址：无协议头，落在 127.0.0.1 自己的域下
}

// 占位符用 U+0000 包裹下标：转义不会碰它，正则也不会误当成 markdown 标记。
const TOK = "\u0000";

function token(store, html) {
  store.push(html);
  return TOK + (store.length - 1) + TOK;
}

function inlineMd(src) {
  const store = [];
  let t = src;
  // 行内代码先摘出来：代码里的 * _ # 不该被当成强调（顺序问题，先摘才不会挨刀）
  t = t.replace(/`([^`\n]+)`/g, (m, c) => token(store, "<code>" + c + "</code>"));
  // 图片降级成文字：alt + 一个标记，让人知道原来这里有张图
  t = t.replace(/!\[([^\]]*)\]\([^)\s]*(?:\s+"[^"]*")?\)/g,
    (m, alt) => token(store, '<span class="md-img">［图片］' + (alt || "") + "</span>"));
  // 链接：协议白名单之外一律降级成「文字（地址）」，点不动也就没有伪协议可执行
  t = t.replace(/\[([^\]]*)\]\(([^)\s]*(?:\s+"[^"]*")?)\)/g, (m, text, dest) => {
    const url = dest.replace(/\s+"[^"]*"$/, "");
    if (isSafeUrl(url)) {
      return token(store, '<a href="' + url + '" target="_blank" rel="noopener noreferrer">'
        + (text || url) + "</a>");
    }
    return text + "（" + url + "）";
  });
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  t = t.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  // 下划线斜体只认成对的：文件名 a_b_c.html 不该被劈开
  t = t.replace(/(^|[\s(（])_([^_]+)_(?=$|[\s).,，。!！?？:：;；])/g, "$1<em>$2</em>");
  return t.replace(/\u0000(\d+)\u0000/g, (m, i) => store[+i]);
}

// 注意 RE_QUOTE 认的是 **&gt;** 而不是 >：本文件的顺序是「先整体转义、再解析块」，
// 到这一步行首的 > 早就变成 &gt; 了。写成 > 会让引用块永远匹配不上（实测假阳性：
// 单测第一版就是在这儿翻车的）。RE_ROW 用的 | 本来就不转义，所以不受影响。
const RE_FENCE = /^\s*(`{3,}|~{3,})\s*(.*)$/;
const RE_HEAD = /^(#{1,6})\s+(.*)$/;
const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const RE_UL = /^\s*[-*+]\s+(.*)$/;
const RE_OL = /^\s*\d+[.)]\s+(.*)$/;
const RE_QUOTE = /^\s*&gt;\s?(.*)$/;
const RE_ROW = /^\s*\|/;
const RE_SEP = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

function splitCells(row) {
  let s = row.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map(c => c.trim());
}

// 相邻两行软换行怎么接：中文直接接上（不需要空格），英文补一个空格免得粘词。
function joinSoft(a, b) {
  if (!a || !b) return a + b;
  const x = a[a.length - 1], y = b[0];
  return /[0-9A-Za-z]/.test(x) && /[0-9A-Za-z]/.test(y) ? a + " " + b : a + b;
}

// 输入是**已转义**的行数组；块级解析与转义分开，是为了让引用块能递归复用。
function renderMdBlocks(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const fence = RE_FENCE.exec(line);
    if (fence) {
      const marker = fence[1][0];
      const body = [];
      i++;
      while (i < lines.length && !(RE_FENCE.test(lines[i]) && lines[i].trim()[0] === marker)) {
        body.push(lines[i]);
        i++;
      }
      i++;                                 // 吃掉收尾的 ```
      out.push('<pre class="md-pre"><code>' + (body.join("\n") || " ") + "</code></pre>");
      continue;
    }

    if (!line.trim()) { i++; continue; }

    const h = RE_HEAD.exec(line);
    if (h) {
      const lv = h[1].length;
      // 两个类都给：md-h 管通用排版，md-hN 管字号。别只给其中一个——
      // style.css 里字号是按级别分的，少一个就全部退回默认大小。
      out.push('<h' + lv + ' class="md-h md-h' + lv + '">' + inlineMd(h[2]) + "</h" + lv + ">");
      i++;
      continue;
    }

    if (RE_HR.test(line)) { out.push('<hr class="md-hr">'); i++; continue; }

    // 表格：当前行像行、下一行像 |---| 分隔行，两条都成立才算表格
    if (RE_ROW.test(line) && i + 1 < lines.length && RE_SEP.test(lines[i + 1])) {
      const head = splitCells(line);
      const align = splitCells(lines[i + 1]).map(c => {
        const l = c.startsWith(":"), r = c.endsWith(":");
        return l && r ? ' class="is-mid"' : l ? ' class="is-left"' : r ? ' class="is-right"' : "";
      });
      i += 2;
      const rows = [];
      while (i < lines.length && RE_ROW.test(lines[i])) { rows.push(splitCells(lines[i])); i++; }
      const th = head.map((c, k) => "<th" + (align[k] || "") + ">" + inlineMd(c) + "</th>").join("");
      const tb = rows.map(r => "<tr>" + head.map((_, k) =>
        "<td" + (align[k] || "") + ">" + inlineMd(r[k] || "") + "</td>").join("") + "</tr>").join("");
      out.push('<div class="md-tablewrap"><table class="md-table"><thead><tr>' + th
        + "</tr></thead><tbody>" + tb + "</tbody></table></div>");
      continue;
    }

    const q = RE_QUOTE.exec(line);
    if (q) {
      const body = [];
      while (i < lines.length) {
        const m = RE_QUOTE.exec(lines[i]);
        if (!m) break;
        body.push(m[1]);
        i++;
      }
      out.push('<blockquote class="md-quote">' + renderMdBlocks(body) + "</blockquote>");
      continue;
    }

    if (RE_UL.test(line) || RE_OL.test(line)) {
      const ordered = RE_OL.test(line) && !RE_UL.test(line);
      const items = [];
      while (i < lines.length) {
        const m = (ordered ? RE_OL : RE_UL).exec(lines[i]);
        if (!m) break;
        items.push("<li>" + inlineMd(m[1]) + "</li>");
        i++;
      }
      out.push((ordered ? "<ol class=\"md-ol\">" : '<ul class="md-ul">') + items.join("") + "</" + (ordered ? "ol" : "ul") + ">");
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim() && !RE_HEAD.test(lines[i])
           && !RE_HR.test(lines[i]) && !RE_UL.test(lines[i]) && !RE_OL.test(lines[i])
           && !RE_QUOTE.test(lines[i]) && !RE_FENCE.test(lines[i])
           && !(RE_ROW.test(lines[i]) && i + 1 < lines.length && RE_SEP.test(lines[i + 1]))) {
      para.push(inlineMd(lines[i].trim()));
      i++;
    }
    if (para.length) out.push("<p>" + para.reduce(joinSoft) + "</p>");
    else i++;                              // 理论上进不来，留着防死循环
  }
  return out.join("");
}

// 对外入口：先转义整段，再交给块级解析。
function mdToHtml(src) {
  if (src == null || src === "") return "";
  return renderMdBlocks(escHtml(src).replace(/\r\n?/g, "\n").split("\n"));
}

function closeDrawer() {
  document.getElementById("drawer").hidden = true;
  state.openId = null;
  setDrawerFull(false);         // 关掉之前把全屏态复位，否则下次打开是全屏的
}

/* 抽屉全屏（用户 2026-10-13：「首先应该可以展开到全屏」）。
 * 读长文档时 27rem 的窄栏要来回横竖滚，全屏才有正常的阅读宽度。 */
function setDrawerFull(on) {
  const d = document.getElementById("drawer");
  d.classList.toggle("is-full", !!on);
  const b = document.getElementById("drawer-full");
  const label = on ? "退出全屏" : "展开到全屏";
  b.textContent = on ? "⤡" : "⤢";
  b.setAttribute("aria-label", label);
  b.title = label;
}

function isDrawerFull() {
  return document.getElementById("drawer").classList.contains("is-full");
}

/* ------------------------------------------------------------ 拖拽（SPEC 8.5）
 *
 * **drop 的判定顺序是 SPEC 点名的坑**：
 *   相同列 -> PATCH {position}；不同列 -> POST /advance {to, position}。
 * 不要在落回原列时发 advance——那会撞上 5.1 的 422，用户看到一个莫名其妙的错误。
 *
 * position 在前端算中点（SPEC 4.2"由前端计算"），但**耗尽重排放服务端**：
 * 服务端 _settle_position 是唯一收口，任何写 position 的路径都过它。
 */

/* 落点在目标列里的 position。beforeId = 落点前面那张卡（null = 落到列尾）。
 * 从 state.items 取同列的其余卡，按 (position, id) 排序后取邻居中点——
 * 与 store 读数口径一致，否则算出来的位置和实际排序对不上。 */
function positionFor(toStatus, id, beforeId) {
  const sibs = state.items
    .filter(i => i.status === toStatus && i.id !== id)
    .sort((a, b) => a.position - b.position || a.id - b.id);
  const before = beforeId ? sibs.find(s => s.id === beforeId) : null;
  if (!before) {
    return sibs.length ? sibs[sibs.length - 1].position + 1 : 1;
  }
  const k = sibs.indexOf(before);
  return k > 0 ? (sibs[k - 1].position + before.position) / 2 : before.position - 1;
}

/* 跨列拖拽：先比源列与目标列，相同走 PATCH，不同才 advance（SPEC 8.5）。
 * 返回服务端落库后的这条卡，供调用方播流转轨。 */
async function moveCard(id, fromStatus, toStatus, beforeId) {
  const pos = positionFor(toStatus, id, beforeId);
  if (fromStatus === toStatus) {
    // 同列：只改位置。PATCH 不带 status——带了会被 422 挡掉。
    return await api("PATCH", "/api/ideas/" + id, { position: pos });
  }
  // 跨列：一次 advance 同时改状态和位置，避免 advance + PATCH 两次调用产生竞态
  return await api("POST", "/api/ideas/" + id + "/advance", { to: toStatus, position: pos });
}

let dragId = null, dragFrom = null;

/* 拖拽结束后浏览器会补一个 click，那次**不算**"点卡片开抽屉"。
 *
 * 不用 setTimeout 清标志位：dragend 先于 click 派发，但两者不在同一个任务里，
 * 0ms 定时器会在 click 到达之前就把标志清掉，于是每拖一次就弹一次抽屉
 * （实测：拖完抽屉自己打开，还挡住下一次拖拽）。
 * 改用时间窗：dragend 之后的短窗口内的 click 一律不算。 */
let lastDragEnd = 0;
function justDragged() {
  return Date.now() - lastDragEnd < 500;
}

/* 拖动时的落点占位块（SPEC 8.5 的 #0000001a）。单独一个节点跟着指针走，
 * 用户能看见"会插在哪"，而不是把卡片丢下去再猜。 */
let phEl = null;
function dropPlaceholder() {
  if (!phEl) {
    phEl = el("div", "drop-ph");
    phEl.id = "drop-ph";
  }
  return phEl;
}
function clearPlaceholder() {
  if (phEl && phEl.parentNode) phEl.parentNode.removeChild(phEl);
  document.querySelectorAll(".col.drop-hint").forEach(x => x.classList.remove("drop-hint"));
}

/* 找到该插在哪张卡前面：取指针下方最近的一张的上半区/下半区判断。
 * 返回 {list, before} —— before 为 null 表示落到列尾。 */
function findDropSlot(col, clientY) {
  const list = col.querySelector(".col-list");
  const cards = [...list.querySelectorAll(".card")].filter(c => +c.dataset.id !== dragId);
  for (const c of cards) {
    const r = c.getBoundingClientRect();
    if (clientY < r.top + r.height / 2) return { list, before: c };
  }
  return { list, before: null };
}

document.addEventListener("dragstart", e => {
  const c = e.target.closest && e.target.closest(".card");
  if (!c) return;
  dragId = +c.dataset.id;
  dragFrom = c.closest(".col").dataset.status;
  c.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  // Firefox 要求 setData 才肯启动拖拽；内容用不上，给个空串即可
  e.dataTransfer.setData("text/plain", String(dragId));
});

document.addEventListener("dragend", () => {
  const c = document.querySelector(".card.dragging");
  if (c) c.classList.remove("dragging");
  clearPlaceholder();
  dragId = null; dragFrom = null;
  // 记下收尾时刻，让紧随其后的补发 click 被 justDragged() 挡掉
  lastDragEnd = Date.now();
});

document.addEventListener("dragover", e => {
  const col = e.target.closest && e.target.closest(".col");
  if (!col || dragId === null) return;
  e.preventDefault();                       // 不 preventDefault 就不允许 drop
  e.dataTransfer.dropEffect = "move";
  document.querySelectorAll(".col.drop-hint").forEach(x => {
    if (x !== col) x.classList.remove("drop-hint");
  });
  col.classList.add("drop-hint");
  const { list, before } = findDropSlot(col, e.clientY);
  const ph = dropPlaceholder();
  if (before) list.insertBefore(ph, before); else list.appendChild(ph);
});

document.addEventListener("drop", async e => {
  const col = e.target.closest && e.target.closest(".col");
  if (!col || dragId === null) return;
  e.preventDefault();
  const id = dragId, from = dragFrom, to = col.dataset.status;
  const ph = dropPlaceholder();
  // beforeId 取占位块后面那张卡（占位块已经在 DOM 里，位置就是用户看到的位置）
  const nxt = ph.nextElementSibling;
  const beforeId = nxt && nxt.classList.contains("card") && +nxt.dataset.id !== id
    ? +nxt.dataset.id : null;
  clearPlaceholder();
  const cardEl = document.querySelector('.card[data-id="' + id + '"]');
  const rail = cardEl && cardEl.querySelector(".rail");
  try {
    const got = await moveCard(id, from, to, beforeId);
    // **slideRail 必须在 reload 之前**：render() 会整板重建 DOM，重建后圆点是
    // 新节点，没有"旧位置"可量，滑动就不会发生。
    if (rail && got && got.status) slideRail(rail, got.status, got.blocked);
    await load();
  } catch (err) {
    drawerMsg(err.message, "bad");          // 例如落回原列误发 advance 的 422
    await load();
  }
  document.querySelectorAll(".card.dragging").forEach(x => x.classList.remove("dragging"));
  dragId = null; dragFrom = null;
  lastDragEnd = Date.now();     // 同 dragend：拖拽收尾，补发的 click 不该开抽屉
});

// 键盘可达：卡片本身是 article，拖拽对它没有意义，给一个回车开抽屉的入口
document.addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const c = e.target.closest && e.target.closest(".card");
  if (c) { e.preventDefault(); openDrawer(+c.dataset.id); }
});

// 快捷登记：打开页面内的 <dialog> 表单，不是 window.prompt。
// prompt 只能填一个字段、样式不受控、一关输入就没了；这里标题/描述/标签都能填。
function quickNew() {
  const dlg = document.getElementById("composer");
  document.getElementById("c-title").value = "";
  document.getElementById("c-body").value = "";
  document.getElementById("c-tags").value = "";
  dlg.showModal();
  document.getElementById("c-title").focus();
}

document.getElementById("c-cancel").onclick = () => document.getElementById("composer").close();

document.getElementById("composer-form").addEventListener("submit", async e => {
  // <form method="dialog"> 默认会直接关掉；先 preventDefault，自己走异步提交，
  // 失败时对话框要留着让人改，成功才关。
  e.preventDefault();
  const title = document.getElementById("c-title").value.trim();
  if (!title) return;
  const body = document.getElementById("c-body").value.trim();
  const tags = document.getElementById("c-tags").value.trim();
  const save = document.getElementById("c-save");
  save.disabled = true;
  try {
    const payload = { title: title };
    if (body) payload.body = body;
    if (tags) payload.tags = tags;
    const got = await api("POST", "/api/ideas", payload);
    document.getElementById("composer").close();
    await load();
    openDrawer(got.id);
  } catch (err) {
    drawerMsg(err.message, "bad");
  } finally {
    save.disabled = false;
  }
});

/* 计划里说这批名字"暴露给 Task 6/7"。同一文件内互相引用靠裸名字就够了，
   但顶层 const 声明不会挂到 window 上——浏览器控制台敲 window.state 会拿到
   undefined。显式挂一份，函数声明本来就在 window 上，重复挂无害。 */
Object.assign(window, {
  api, state, render, load, reload, el, card, STATUS_ORDER, STATUS_LABEL,
  buildRail, paintRail, slideRail, openDrawer, closeDrawer, moveCard,
  positionFor, quickNew, fmtTime,
  // 本任务新增：Playwright/控制台断言要能直接够到渲染器与文档区块
  mdToHtml, escHtml, isSafeUrl, renderMdBlocks, fmtSize,
  drawDocs, openDoc, refreshDocs, docsState
});

let qTimer = null;
document.getElementById("q").oninput = e => {
  state.filter.q = e.target.value.trim();
  clearTimeout(qTimer);
  qTimer = setTimeout(reload, 200);       // 每敲一下就发一次请求太吵
};
document.getElementById("tag").onchange = e => { state.filter.tag = e.target.value; reload(); };
document.getElementById("only-blocked").onchange = e => { state.filter.blocked = e.target.checked; reload(); };
document.getElementById("new-btn").onclick = () => quickNew();
document.getElementById("drawer-close").onclick = () => closeDrawer();
document.getElementById("drawer-full").onclick = () => setDrawerFull(!isDrawerFull());

// Esc：全屏时先退出全屏，再按一次才关抽屉。直接一步关掉会让"我只是想看大图"
// 变成丢上下文。
//
// **任何一个 <dialog> 开着时一律不插手**：模态 dialog 自己吃 Esc，关掉的是它。
// 只避让 composer 是不够的——删除确认表也在这底下，一并交出去。
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (document.querySelector("dialog[open]")) return;
  const d = document.getElementById("drawer");
  if (d.hidden) return;
  e.preventDefault();
  if (isDrawerFull()) setDrawerFull(false);
  else closeDrawer();
});

reload();
