/* 列表工作区前端回归（由 selftest.py 以 node 调用，不在浏览器中加载）。
   覆盖三个已复核缺陷：
   1. createFromSelection 必须提交 chapter_id（否则后端 KeyError → 500）；
   2. 前后边界方向 prev/next 必须映射为后端的 before/after（“下一侧”走 after 分支）；
   3. 每次操作的 DOM 前后快照在 renderListWorkspace 重绘后仍保留在双栏中。 */
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const results = [];
function check(name, cond, extra) {
  results.push({ name, ok: !!cond, extra: extra || "" });
}

function makeEl(id) {
  const parent = { classList: { add() {}, remove() {}, toggle() {}, contains: () => false } };
  const el = {
    _id: id, value: "", textContent: "", innerHTML: "", disabled: false,
    checked: false, dataset: {}, _buttons: null, _cls: new Set(),
    classList: null, onclick: null, onchange: null, parentElement: parent,
    addEventListener() {}, appendChild() {},
    querySelectorAll() { return el._buttons || []; },
  };
  el.classList = {
    add(c) { el._cls.add(c); }, remove(c) { el._cls.delete(c); },
    toggle(c, f) { (f === undefined ? true : f) ? el._cls.add(c) : el._cls.delete(c); },
    contains(c) { return el._cls.has(c); },
  };
  return el;
}

const els = {};
function el(id) { return els[id] || (els[id] = makeEl(id)); }

let captured = null;
const stubs = {
  S: { bookId: 7, nodes: {}, htmlLang: {}, candidates: {} },
  api: async (url, method, body) => { captured = { url, method, body }; return stubs._resp; },
  toast() {}, esc: (s) => s, applyState() {}, renderAll() {},
  reloadPreview() {}, refreshCandidates: async () => {}, highlightInPreview() {},
  selectChapter() {},
  _resp: {},
};

const sandbox = {
  console, process,
  $: (sel) => el(sel),
  document: { addEventListener() {}, querySelectorAll: () => stubs._docButtons || [] },
  window: {},
  results, check, getCaptured: () => captured,
  stubs, setTimeout,
};
Object.assign(sandbox, stubs);

const listSrc = fs.readFileSync(path.join(__dirname, "list.js"), "utf8");

const testSrc = `
(async () => {
  // ---- 缺陷 1：createFromSelection 提交 chapter_id ----
  L.mode = "create";
  L.chapterId = 42;
  L.selected = new Set(["html[1]/body[1]/p[1]", "html[1]/body[1]/p[2]"]);
  L.blocks = {};
  $("#lwType").value = "ol";
  $("#lwStart").value = "3";
  $("#lwStrip").checked = true;
  stubs._resp = {
    new_path: "html[1]/body[1]/ol[1]",
    model: { type: "ol", start: 3, items: [], boundaries: { prev: null, next: null },
             reading: "有序列表", issues: [] },
    nodes: [], state: {}, before_pretty: "<ol> 前", after_pretty: "<ol> 后",
  };
  await createFromSelection();
  const cap1 = getCaptured();
  check("组合请求 POST 到 /list/create",
        cap1.method === "POST" && cap1.url.endsWith("/list/create"));
  check("组合负载携带 chapter_id（不再 500）", cap1.body.chapter_id === 42,
        JSON.stringify(Object.keys(cap1.body)));
  check("组合负载携带 paths/type/start",
        Array.isArray(cap1.body.paths) && cap1.body.paths.length === 2
        && cap1.body.type === "ol" && cap1.body.start === 3);

  // ---- 缺陷 3：重绘后双栏保留前后快照 ----
  check("提交后立即写入前后快照",
        $("#lwBefore").textContent === "<ol> 前" &&
        $("#lwAfter").textContent === "<ol> 后");
  L.model = stubs._resp.model;
  renderListWorkspace();
  check("重绘后左栏保留提交前 DOM", $("#lwBefore").textContent === "<ol> 前");
  check("重绘后右栏保留提交后 DOM", $("#lwAfter").textContent === "<ol> 后");
  showDiff("X", "Y");
  renderListWorkspace();
  check("更新快照后重绘仍保留",
        $("#lwBefore").textContent === "X" && $("#lwAfter").textContent === "Y");
  await openListWorkspaceCreate(99, ["p[1]"], null);
  renderListWorkspace();
  check("重新打开工作区清空对照栏",
        $("#lwBefore").textContent === "" && $("#lwAfter").textContent === "");

  // ---- 缺陷 2：边界方向映射 ----
  const model = {
    type: "ol", start: 1,
    items: [{ path: "html[1]/body[1]/ol[1]/li[1]", depth: 0, illegal: false }],
    boundaries: {
      prev: { action: "join", tag: "ol", count: 1, path: "html[1]/body[1]/ol[0]" },
      next: { action: "join", tag: "ol", count: 2, path: "html[1]/body[1]/ol[2]" },
    },
    reading: "", issues: [],
  };
  L.mode = "edit"; L.chapterId = 5;
  L.path = "html[1]/body[1]/ol[1]"; L.model = model;
  stubs._resp = { new_path: L.path, model, nodes: [], state: {},
                  before_pretty: "b", after_pretty: "a" };

  const nextBtn = { classList: { contains: (c) => c === "lw-join" }, onclick: null };
  $("#lwNext")._buttons = [nextBtn];
  $("#lwPrev")._buttons = [];
  renderListWorkspace();
  await nextBtn.onclick();
  const cap2 = getCaptured();
  check("下一侧接续调用 op 且 side=after",
        cap2.url.endsWith("/list/op") && cap2.body.op === "join"
        && cap2.body.params.side === "after", JSON.stringify(cap2.body.params));

  const prevBtn = { classList: { contains: (c) => c === "lw-join" }, onclick: null };
  $("#lwPrev")._buttons = [prevBtn];
  $("#lwNext")._buttons = [];
  renderListWorkspace();
  await prevBtn.onclick();
  const cap3 = getCaptured();
  check("上一侧接续调用 op 且 side=before",
        cap3.body.params.side === "before", JSON.stringify(cap3.body.params));

  L.model = { type: "ol", start: 1, items: model.items,
    boundaries: { prev: null,
      next: { action: "blocked", tag: "h2", path: "html[1]/body[1]/h2[1]" } },
    reading: "", issues: [] };
  const sel = { dataset: { side: "next" }, value: "into_prev", onchange: null };
  stubs._docButtons = [sel];
  renderListWorkspace();
  await sel.onchange();
  const cap4 = getCaptured();
  check("下一侧拦截元素处置映射 side=after",
        cap4.body.op === "extend" && cap4.body.params.side === "after"
        && cap4.body.params.placement === "into_prev",
        JSON.stringify(cap4.body.params));
  stubs._docButtons = [];
})().catch((e) => { console.error("TEST HARNESS ERROR:", e); process.exit(2); });
`;

vm.runInNewContext(listSrc + "\n" + testSrc, sandbox, { filename: "list.js+test" });

setTimeout(() => {
  let fail = 0;
  for (const r of results) {
    console.log((r.ok ? "PASS  " : "FAIL  ") + r.name + (r.extra ? "  " + r.extra : ""));
    if (!r.ok) fail++;
  }
  console.log("\n%d passed, %d failed", results.length - fail, fail);
  process.exit(fail ? 1 : 0);
}, 200);
