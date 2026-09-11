/* 注释关系图工作区前端回归（由 selftest.py 以 node 调用，不在浏览器中加载）。
   覆盖：
   1. 两栏模型渲染与连线暂存（点引用→点注释产生 links + 每引用一条 backlink）；
   2. 批量套用提交到 /note/preview，负载携带 batch_group；
   3. 提交 /note/save 后清空暂存并 applyState；后端 400（阻止）时刷新真实关系、
      不把失败暂存提交出去。 */
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const results = [];
function check(name, cond, extra) {
  results.push({ name, ok: !!cond, extra: extra || "" });
}

function makeEl(id) {
  const el = {
    _id: id, value: "", textContent: "", innerHTML: "", disabled: false,
    dataset: {}, _cls: new Set(), onclick: null, onchange: null,
    style: {}, _children: [], _btns: {},
    parentElement: null,
  };
  el.classList = {
    add(c) { el._cls.add(c); }, remove(c) { el._cls.delete(c); },
    toggle(c, f) { (f === undefined ? true : f) ? el._cls.add(c) : el._cls.delete(c); },
    contains(c) { return el._cls.has(c); },
  };
  el.querySelector = () => null;
  // 选择器感知：[data-x] 的元素通过 _btns["data-x"] 预置（自带 onclick）
  el.querySelectorAll = (sel) => {
    const m = String(sel).match(/\[data-([a-z]+)\]/);
    if (m) return el._btns[m[0]] || [];
    return el._btns.__all__ || [];
  };
  el.getBoundingClientRect = () => ({ top: 0, left: 0, right: 0, bottom: 0,
                                     width: 0, height: 0 });
  el.setAttribute = () => {};
  return el;
}

const els = {};
function el(id) { return els[id] || (els[id] = makeEl(id)); }

let calls = [];
const stubs = {
  S: { bookId: 7, chapters: [{ id: 1, href: "ch6.xhtml" }, { id: 2, href: "ch3.xhtml" }],
       currentChapter: 1 },
  api: async (url, method, body) => {
    calls.push({ url, method, body });
    if (stubs._failNext) { stubs._failNext = false; throw new Error(stubs._failMsg || "blocked"); }
    return stubs._resp;
  },
  toast() {}, esc: (s) => String(s ?? ""), applyState() {}, renderAll() {},
  reloadPreview() {}, selectChapter() {}, highlightInPreview() {},
  CSS: { escape: (s) => s },
  confirm: () => true,
  _resp: {}, _failNext: false, _failMsg: "",
};

const sandbox = {
  console, process,
  $: (sel) => el(sel),
  document: { addEventListener() {}, querySelectorAll: () => [],
              querySelector: () => null },
  window: { addEventListener() {}, speechSynthesis: undefined },
  results, check, callLog: calls, stubs, el,
  setTimeout, clearTimeout: () => {}, SpeechSynthesisUtterance: undefined,
};
Object.assign(sandbox, stubs);

const noteSrc = fs.readFileSync(path.join(__dirname, "note.js"), "utf8");

function modelFixture() {
  const ref3a = { key: "ch6.xhtml|r3a", chapter: "ch6.xhtml", path: "p/a1", tag: "a",
    el_id: "enr3a", declared: true, number: 3, marker: "[3]", href: "#en3",
    external: false, within_note: null, target_key: "ch6.xhtml|n3",
    target_kind: "note", target_missing: false, context: "第一次",
    spine_pos: 5, order: 1 };
  const ref3b = { key: "ch6.xhtml|r3b", chapter: "ch6.xhtml", path: "p/a2", tag: "a",
    el_id: "enr3b", declared: true, number: 3, marker: "[3]", href: "#en3",
    external: false, within_note: null, target_key: "ch6.xhtml|n3",
    target_kind: "note", target_missing: false, context: "第二次",
    spine_pos: 5, order: 2 };
  const sup6 = { key: "ch6.xhtml|sup6", chapter: "ch6.xhtml", path: "p2/sup1", tag: "sup",
    el_id: null, declared: false, number: 6, marker: "[6]", href: null,
    external: false, within_note: null, target_key: null, target_kind: null,
    target_missing: false, context: "", spine_pos: 5, order: 3 };
  const n3 = { key: "ch6.xhtml|n3", chapter: "ch6.xhtml", path: "ol/li1", tag: "li",
    el_id: "en3", declared: true, note_type: "endnote", in_section: true,
    number: 3, marker: "[3]", nested: false, parent_note_key: null,
    text: "3. 共用尾注", ref_keys: ["ch6.xhtml|r3a", "ch6.xhtml|r3b"],
    backlinks: [{ href: "#enr3a", external: false, ref_key: "ch6.xhtml|r3a" }],
    orphan: false, mixed_plain: false, spine_pos: 5, order: 9 };
  const n6 = { key: "ch6.xhtml|n6", chapter: "ch6.xhtml", path: "aside2", tag: "aside",
    el_id: "en6", declared: false, note_type: "endnote", in_section: true,
    number: 6, marker: "[6]", nested: false, parent_note_key: null,
    text: "[6] 未声明注释", ref_keys: [], backlinks: [],
    orphan: true, mixed_plain: false, spine_pos: 5, order: 12 };
  return {
    chapters: [{ href: "ch6.xhtml", name: "ch6.xhtml" },
               { href: "ch3.xhtml", name: "ch3.xhtml" }],
    refs: [ref3a, ref3b, sup6], notes: [n3, n6],
    edges: [{ ref_key: "ch6.xhtml|r3a", note_key: "ch6.xhtml|n3" },
            { ref_key: "ch6.xhtml|r3b", note_key: "ch6.xhtml|n3" }],
    issues: [{ severity: "warning", message: "可见注号「[6]」没有 noteref 语义也没有连线" },
             { severity: "warning", message: "注释 #en3 缺少回到注号「[3]」的回程链接" }],
    blocks: { cycles: [], external: [], unmatched: [] },
    batches: [{ id: "batch-0", chapter: "ch6.xhtml", note_type: "endnote",
                start: 6, end: 6, applicable: true,
                items: [{ number: 6, ref_key: "ch6.xhtml|sup6",
                          note_key: "ch6.xhtml|n6", note_type: "endnote",
                          applicable: true, reason: "" }] }],
    sequence: [
      { kind: "ref", chapter: "ch6.xhtml", path: "p/a1", text: "正文注号「[3]」" },
      { kind: "note", chapter: "ch6.xhtml", path: "ol/li1", text: "  ↳ 尾注" },
      { kind: "back", chapter: "ch6.xhtml", path: "ol/li1", text: "    ↩ 回到本引用处" },
      { kind: "warn", chapter: "ch6.xhtml", path: "p/a2",
        text: "    ⚠ 没有回到本引用的回程链接（共用回链只回到第一处）" }],
  };
}

const testSrc = `
(async () => {
  // ---- 初始模型 ----
  stubs._resp = modelFixture();
  await openNoteWorkspace(null);
  check("模型载入两栏", N.model.refs.length === 3 && N.model.notes.length === 2);
  check("暂存编辑初始为空", !nHasEdits());
  const refsBox = $("#ntRefs"), notesBox = $("#ntNotes");
  check("渲染引用节点", (refsBox.innerHTML.match(/nt-node/g) || []).length === 3);
  check("渲染注释节点", (notesBox.innerHTML.match(/nt-node/g) || []).length === 2);
  check("未声明节点带 undecl 样式", refsBox.innerHTML.includes("undecl")
        && notesBox.innerHTML.includes("undecl"));

  // ---- 连线暂存：选引用再点注释 → links + backlink ----
  onNoteNodeClick("ref", "ch6.xhtml|sup6");
  check("选中引用", N.selected && N.selected.key === "ch6.xhtml|sup6");
  N.linkFrom = { side: "ref", key: "ch6.xhtml|sup6" };
  onNoteNodeClick("note", "ch6.xhtml|n6");
  check("连线暂存 links", N.edits.links.length === 1
        && N.edits.links[0].note_key === "ch6.xhtml|n6");
  check("每次连线都建立回程目标", N.edits.backlinks.length === 1
        && N.edits.backlinks[0].ref_key === "ch6.xhtml|sup6");
  check("有暂存时可提交", nHasEdits());

  // ---- 为多次引用补回程：n3 缺 r3b 的 backlink ----
  stubs._resp = { model: N.model };
  N.selected = { side: "note", key: "ch6.xhtml|n3" };
  const addBtn = { dataset: { addbl: "ch6.xhtml|r3b" }, onclick: null };
  el("#ntDetail")._btns["[data-addbl]"] = [addBtn];
  renderNoteWorkspace();
  await addBtn.onclick();
  check("为第二次引用建立独立回程", N.edits.backlinks.some(
    (b) => b.ref_key === "ch6.xhtml|r3b" && b.note_key === "ch6.xhtml|n3"));

  // ---- 批量按钮 → /note/preview 携带 batch_group ----
  callLog.length = 0;
  stubs._resp = { model: N.model };
  const batchBtn = { dataset: { batch: "batch-0" }, onclick: null };
  el("#ntBatches")._btns["[data-batch]"] = [batchBtn];
  renderNoteWorkspace();
  await batchBtn.onclick();
  const prevCall = callLog.find((c) => c.url.endsWith("/note/preview"));
  check("批量暂存 POST 到 preview", !!prevCall && prevCall.method === "POST");
  check("批量负载携带 batch_group=batch-0",
        prevCall.body.edits.batch_group === "batch-0");

  // ---- 删除仍被引用的注释：前端本地阻止并列来源（不发请求）----
  callLog.length = 0;
  N.selected = { side: "note", key: "ch6.xhtml|n3" };
  renderNoteWorkspace();
  const delBtn = $("#ntDelete");
  check("仍被引用时也提供删除按钮（来源由后端阻止）", !!delBtn);
  stubs.confirm = () => true;
  await delBtn.onclick();
  check("前端发现仍有引用来源，本地阻止删除",
        N.edits.deletes.length === 0 && callLog.length === 0);

  // ---- 提交保存：成功后清空暂存 ----
  N.edits = { marks: [], unmarks: [], links: [], unlinks: [],
              backlinks: [{ ref_key: "ch6.xhtml|r3b", note_key: "ch6.xhtml|n3" }],
              deletes: [] };
  stubs._resp = { model: N.model, state: { changes: [] } };
  callLog.length = 0;
  await $("#ntSave").onclick();
  const saveCall = callLog.find((c) => c.url.endsWith("/note/save"));
  check("提交 POST 到 /note/save", !!saveCall);
  check("保存负载含 backlinks", saveCall.body.edits.backlinks.length === 1);
  check("保存成功后暂存清空", !nHasEdits());

  // ---- 后端阻止（400/异常）：恢复真实模型，不再保留失败暂存 ----
  N.edits = { marks: [], unmarks: [], links: [{ ref_key: "x", note_key: "y" }],
              unlinks: [], backlinks: [], deletes: [] };
  stubs._resp = modelFixture();
  stubs._failNext = true; stubs._failMsg = "存在跨书目链接，提交被阻止";
  await $("#ntSave").onclick();
  check("阻止后暂存被清空", !nHasEdits());
  check("阻止后模型回滚到真实关系", N.model.batches.length === 1);
})().catch((e) => { console.error("TEST HARNESS ERROR:", e); process.exit(2); });
`;

const fixtureSrc = "(" + modelFixture.toString() + ")";
vm.runInNewContext(noteSrc + "\nconst modelFixture = " + fixtureSrc + ";\n" + testSrc,
                   sandbox, { filename: "note.js+test" });

setTimeout(() => {
  let fail = 0;
  for (const r of results) {
    console.log((r.ok ? "PASS  " : "FAIL  ") + r.name + (r.extra ? "  " + r.extra : ""));
    if (!r.ok) fail++;
  }
  console.log("\n%d passed, %d failed", results.length - fail, fail);
  process.exit(fail ? 1 : 0);
}, 200);
