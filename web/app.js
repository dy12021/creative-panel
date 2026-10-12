/* 创意面板前端 —— 数据层 + 五列看板 + 流转轨（Task 5 / Task 6）
 *
 * 边界（SPEC 第 2 节）：面板是账本，不是执行器。这里只读写 REST API，
 * 不调任何 skill，也没有任何"替我去执行"的动作。
 *
 * 留给后续任务的接口：api() state render() STATUS_LABEL STATUS_ORDER
 * buildRail() paintRail() slideRail()。
 * Task 7 接抽屉与拖拽。
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

async function load() {
  const seq = ++loadSeq;
  const p = new URLSearchParams();
  if (state.filter.q) p.set("q", state.filter.q);
  if (state.filter.tag) p.set("tag", state.filter.tag);
  if (state.filter.blocked) p.set("blocked", "1");
  const items = await api("GET", "/api/ideas?" + p.toString());
  if (seq !== loadSeq) return;          // 已经有更新的请求在飞，丢掉这次
  state.items = items;
  collectTags(items);
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
 * 本产品区别于通用任务板的签名元素：卡片左缘一条竖直窄条，自上而下 5 格，
 * 一格对应一个状态。**渲染完全按索引决定，不按"走过的路径"**——所以跳跃推进时
 * （idea 直接到 building），被跳过的态一律算"已过"，前 3 格全是实心。
 * 已过的格子用**它自己那一态的颜色**，整条轨因此是一条状态光谱。
 *
 * 阻塞时**只**把当前态圆点转红（卡片红描边由 .card.is-blocked 负责），
 * 绝不能把整条轨涂红——那会丢掉"走到哪一步"，正是流转轨存在的意义。
 */

// 就地把一条轨重画成 status 的样子。抽出来是为了 slideRail 能"先量后改"：
// 同一个函数负责建轨和改态，滑动才不会因为走了另一条渲染路径而失效。
function paintRail(rail, status, blocked) {
  const idx = STATUS_ORDER.indexOf(status);
  rail.textContent = "";                     // 先清干净，避免重画时叠出两层格
  rail.dataset.status = status;
  STATUS_ORDER.forEach((st, i) => {
    // 未知状态名只会让 idx=-1，于是 5 格全判成 future 且没有当前圆点，
    // 不会抛异常；服务端本来就用白名单约束 status，这里只是兜底。
    const seg = el("span", "rail-seg " + (idx < 0 ? "future" : i < idx ? "past" : i === idx ? "now" : "future"));
    seg.dataset.status = st;
    seg.style.setProperty("--seg", "var(--st-" + st + ")");
    seg.title = STATUS_LABEL[st];
    if (i === idx) {
      // 圆点是轨上唯一的"当前"信号，红色也只落在它身上（SPEC 8.2）
      const dot = el("span", "rail-dot");
      dot.style.setProperty("--dot", blocked ? "var(--blocked)" : "var(--st-" + st + ")");
      seg.append(dot);
    }
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
  const dot = rail.querySelector(".rail-dot");
  const fromTop = dot ? dot.getBoundingClientRect().top : 0;
  paintRail(rail, toStatus, blocked);
  if (same) return;                      // 只是阻塞态翻转，没有位移可播
  const dot2 = rail.querySelector(".rail-dot");
  if (!dot2) return;                                   // 目标态认不出来，没有可滑的圆点
  const dy = fromTop - dot2.getBoundingClientRect().top;
  if (Math.abs(dy) < .5) return;                       // 没有实际位移，不播动画
  dot2.style.transition = "none";
  dot2.style.transform = "translateY(" + dy + "px)";
  void dot2.offsetHeight;                              // 强制回流，把反向位移先落定再放行
  dot2.style.transition = "";                          // 交回样式表里的 transition
  dot2.style.transform = "";
}

// 卡片本体。流转轨接在左缘，内容装进 .card-body（卡片是横向 flex，轨才贴得住左缘）。
// 点开抽屉由 Task 7 接进来。
function card(i) {
  const c = el("article", "card");
  c.dataset.id = i.id;
  c.dataset.status = i.status;
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
  return c;
}

// 标签下拉的选项来自 state.tags 这个并集，而不是当前筛选结果。
// 否则选中一个标签后选项会塌缩成只剩那一个，再也换不到别的标签。
function collectTags(items) {
  for (const i of items) {
    i.tags.split(",").filter(Boolean).forEach(t => state.tags.add(t));
  }
}

function renderTagFilter() {
  const sel = document.getElementById("tag");
  const cur = state.filter.tag;           // 以 state 为准，不读 sel.value
  sel.textContent = "";
  sel.append(new Option("全部标签", ""));
  [...state.tags].sort().forEach(t => sel.append(new Option(t, t)));
  sel.value = cur;
}

/* Task 6 / Task 7 会用真正的实现覆盖下面两个函数（同文件内后声明者生效）。
   先给最小不抛异常的版本，免得本任务交付态点一下按钮就 ReferenceError。 */
function quickNew() { /* Task 6 填充：问标题 -> POST /api/ideas */ }

function closeDrawer() {
  document.getElementById("drawer").hidden = true;
  state.openId = null;
}

/* 计划里说这批名字"暴露给 Task 6/7"。同一文件内互相引用靠裸名字就够了，
   但顶层 const 声明不会挂到 window 上——浏览器控制台敲 window.state 会拿到
   undefined。显式挂一份，函数声明本来就在 window 上，重复挂无害。 */
Object.assign(window, {
  api, state, render, load, reload, el, card, STATUS_ORDER, STATUS_LABEL,
  buildRail, paintRail, slideRail
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
