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
  items: [],       // 当前筛选后的列表项（12 字段，不含 events）
  tags: new Set(), // 标签并集，只增不减，见 renderTagFilter
  filter: { q: "", tag: "", blocked: false },
  openId: null
};

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

function drawDrawer(id, idea) {
  const body = document.getElementById("drawer-body");
  body.textContent = "";

  body.append(el("h2", "drawer-title", idea.title));
  const sub = el("div", "drawer-sub");
  sub.append(el("span", "drawer-id", "#" + idea.id));
  sub.append(el("span", "drawer-status", STATUS_LABEL[idea.status] || idea.status));
  if (idea.source) sub.append(el("span", "drawer-source", idea.source));
  body.append(sub);

  // 完整描述（body 可能是多行，纯文本保留换行）
  body.append(el("h3", "sec-h", "描述"));
  const bd = el("div", "drawer-body-text");
  bd.textContent = idea.body || "（还没有描述）";
  body.append(bd);

  /* 标签编辑：整串编辑比逐个加删省事，也避免"最后剩一个标签时删不掉"这种
   * 半吊子交互。PATCH tags 传逗号串，服务端统一规范化（SPEC 4.2）。 */
  body.append(el("h3", "sec-h", "标签"));
  const tagForm = el("div", "tag-form");
  const tagInput = el("input", "tag-input");
  tagInput.type = "text";
  tagInput.placeholder = "逗号分隔，留空即无标签";
  tagInput.value = idea.tags.split(",").filter(Boolean).join(",");
  const tagSave = el("button", "secondary", "保存标签");
  tagSave.onclick = async () => {
    tagSave.disabled = true;
    try {
      const got = await api("PATCH", "/api/ideas/" + id, { tags: tagInput.value });
      tagInput.value = got.tags.split(",").filter(Boolean).join(",");
      drawerMsg("");
      await load();                       // 下拉的标签全集跟着更新
      drawDrawer(id, got);
    } catch (e) {
      drawerMsg(e.message, "bad");        // 例如"标签不能为空"
    } finally {
      tagSave.disabled = false;
    }
  };
  tagForm.append(tagInput, tagSave);
  body.append(tagForm);

  /* 推进：目标状态选择器 + **可填可不填**的备注框。
   * 备注框不是装饰——人手操作同样要能补一句，否则 human 路径写出的
   * events 全是空 message，时间线会很难读（SPEC 8.5）。 */
  body.append(el("h3", "sec-h", "推进"));
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
  body.append(form);

  /* 阻塞按钮。注意请求体字段：block 收 {reason}，不是 note（SPEC 5.6）。 */
  body.append(el("h3", "sec-h", "阻塞"));
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
  body.append(blockForm);
  // 5.6：blocked_note 只是当前快照，只在真的卡住时渲染，
  // 否则会出现"没卡住却挂着旧原因"的错觉
  if (idea.blocked) {
    body.append(el("p", "blocked-note", "卡住原因：" + (idea.blocked_note || "（没写）")));
  }

  body.append(el("h3", "sec-h", "时间线"));
  const tl = el("ol", "timeline");
  (idea.events || []).forEach(e => {
    const li = el("li", "tl-item");
    li.append(el("span", "ev-when", fmtTime(e.created_at)));
    li.append(el("span", "ev-actor", ACTOR_LABEL[e.actor] || e.actor));
    const txt = el("span", "ev-text");
    let s;
    if (e.kind === "status") {
      s = (STATUS_LABEL[e.from_status] || e.from_status || "?") + " → "
        + (STATUS_LABEL[e.to_status] || e.to_status || "?");
      if (e.message) s += "　" + e.message;
    } else {
      const k = KIND_LABEL[e.kind] || e.kind;
      s = k + (e.message ? "：" + e.message : "");
    }
    txt.textContent = s;
    li.append(txt);
    tl.append(li);
  });
  if (!(idea.events || []).length) tl.append(el("li", "tl-empty", "还没有记录"));
  body.append(tl);
}

function closeDrawer() {
  document.getElementById("drawer").hidden = true;
  state.openId = null;
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
  positionFor, quickNew, fmtTime
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

reload();
