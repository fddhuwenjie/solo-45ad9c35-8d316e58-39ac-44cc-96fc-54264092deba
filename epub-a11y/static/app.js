/* EPUB 无障碍阅读顺序校修工具 —— 前端逻辑 */
"use strict";

const S = {
  bookId: null,
  chapters: [],
  nodes: {},          // chapterId -> [node]
  htmlLang: {},       // chapterId -> <html> 的 lang
  candidates: {},     // chapterId -> 列表候选 {candidates, lists}
  issues: [],
  landmarks: [],
  changes: [],
  checkLabels: {},
  currentChapter: null,
  selected: null,     // {chapterId, path}
  pendingHighlight: null,
  listSelectMode: false,  // 预览框选模式：框选连续节点以组合列表
};

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(url, method = "GET", body = null) {
  const opt = { method };
  if (body) { opt.headers = { "Content-Type": "application/json" }; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  const d = await r.json();
  if (!r.ok) { toast(d.error || "请求失败", true); throw new Error(d.error); }
  return d;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.style.color = isErr ? "#cf222e" : "#1a7f37";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.textContent = ""), 5000);
}

/* ---------------- 书籍加载 ---------------- */
async function refreshCandidates(chapterId) {
  S.candidates[chapterId] =
    await api(`/api/books/${S.bookId}/list/candidates/${chapterId}`);
}

async function loadBooks() {
  const books = await api("/api/books");
  const sel = $("#bookSelect");
  sel.innerHTML = '<option value="">选择书籍…</option>' +
    books.map((b) => `<option value="${b.id}">${esc(b.title)} (#${b.id})</option>`).join("");
  if (books.length && !S.bookId) openBook(books[0].id);
}

async function openBook(id) {
  const st = await api(`/api/books/${id}`);
  S.bookId = id;
  applyState(st);
  S.nodes = {}; S.htmlLang = {}; S.candidates = {};
  $("#tableModal").classList.remove("open");
  $("#listModal").classList.remove("open");
  await Promise.all(S.chapters.map(async (c) => {
    const d = await api(`/api/books/${id}/chapters/${c.id}/nodes`);
    S.nodes[c.id] = d.nodes; S.htmlLang[c.id] = d.html_lang;
  }));
  await Promise.all(S.chapters.map(async (c) => {
    S.candidates[c.id] = await api(`/api/books/${id}/list/candidates/${c.id}`);
  }));
  $("#bookSelect").value = id;
  renderAll();
  if (S.chapters.length) selectChapter(S.chapters[0].id);
}

function applyState(st) {
  S.chapters = st.chapters;
  S.issues = st.issues;
  S.landmarks = st.landmarks;
  S.changes = st.changes;
  S.checkLabels = st.check_labels;
}

function renderAll() {
  renderTree(); renderOrder(); renderIssues(); renderLandmarks(); renderChanges();
}

/* ---------------- 章节树 ---------------- */
function tagBadge(n) {
  const cls = n.tag.match(/^h[1-6]$/) ? "h" : n.tag === "img" ? "img" : n.tag === "aside" ? "aside" : "";
  return `<span class="tag-badge ${cls}">${esc(n.tag)}</span>`;
}

function nodeLabel(n) {
  let t = n.tag === "img" ? (n.img_src || "图片") : (n.text || "");
  if (n.epub_type) t = `[${n.epub_type}] ` + t;
  return esc(t || "(空)");
}

function issueFlags(chapterHref, path) {
  const n = S.issues.filter((i) => i.chapter_href === chapterHref && i.node_path === path).length;
  return n ? `<span class="issue-flag" title="${n} 个问题">⚠${n}</span>` : "";
}

function renderTree() {
  const ul = $("#chapterTree");
  ul.innerHTML = "";
  for (const ch of S.chapters) {
    const li = document.createElement("li");
    li.className = "chapter-row";
    li.draggable = true;
    li.dataset.chapterId = ch.id;
    li.innerHTML = `📄 ${esc(ch.title)}${issueFlags(ch.href, null) || ""}`;
    li.onclick = () => selectChapter(ch.id);
    enableChapterDnD(li, ch);
    ul.appendChild(li);

    const nodes = S.nodes[ch.id] || [];
    const byParent = {};
    nodes.forEach((n) => (byParent[n.parent_path || ""] ??= []).push(n));
    const addNodes = (parentPath, depth) => {
      for (const n of byParent[parentPath] || []) {
        const ni = document.createElement("li");
        ni.className = "node-row";
        ni.draggable = true;
        ni.style.paddingLeft = 14 + depth * 14 + "px";
        ni.dataset.path = n.dom_path;
        ni.dataset.chapterId = ch.id;
        if (S.selected && S.selected.path === n.dom_path && S.selected.chapterId === ch.id)
          ni.classList.add("selected");
        ni.innerHTML = `${tagBadge(n)}${nodeLabel(n)}${issueFlags(ch.href, n.dom_path)}`;
        ni.onclick = (e) => { e.stopPropagation(); selectNode(ch.id, n.dom_path); };
        enableNodeDnD(ni, ch, n);
        ul.appendChild(ni);
        addNodes(n.dom_path, depth + 1);
      }
    };
    addNodes("", 0);
  }
}

/* ---------------- 拖拽 ---------------- */
function enableChapterDnD(li, ch) {
  li.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", JSON.stringify({ kind: "chapter", id: ch.id }));
  });
  li.addEventListener("dragover", (e) => { e.preventDefault(); li.classList.add("drop-target"); });
  li.addEventListener("dragleave", () => li.classList.remove("drop-target"));
  li.addEventListener("drop", async (e) => {
    e.preventDefault(); li.classList.remove("drop-target");
    const d = JSON.parse(e.dataTransfer.getData("text/plain"));
    if (d.kind === "chapter" && d.id !== ch.id) {
      const ids = S.chapters.map((c) => c.id).filter((i) => i !== d.id);
      ids.splice(ids.indexOf(ch.id), 0, d.id);
      const r = await api(`/api/books/${S.bookId}/spine/reorder`, "POST", { order: ids });
      applyState(r.state); renderAll();
      toast("章节顺序已调整；重跑检查: " + r.checks_ran.map(labelOf).join("、"));
    } else if (d.kind === "node" && d.chapterId === ch.id) {
      await doMoveNode(ch.id, d.path, null, null);  // 移到本章末尾
    }
  });
}

function enableNodeDnD(li, ch, node) {
  li.addEventListener("dragstart", (e) => {
    e.stopPropagation();
    e.dataTransfer.setData("text/plain",
      JSON.stringify({ kind: "node", chapterId: ch.id, path: node.dom_path }));
  });
  li.addEventListener("dragover", (e) => { e.preventDefault(); e.stopPropagation(); li.classList.add("drop-target"); });
  li.addEventListener("dragleave", () => li.classList.remove("drop-target"));
  li.addEventListener("drop", async (e) => {
    e.preventDefault(); e.stopPropagation(); li.classList.remove("drop-target");
    const d = JSON.parse(e.dataTransfer.getData("text/plain"));
    if (d.kind === "node" && d.chapterId === ch.id && d.path !== node.dom_path)
      await doMoveNode(ch.id, d.path, node.dom_path, null);
    else if (d.kind === "node" && d.chapterId !== ch.id)
      toast("暂不支持跨章移动节点", true);
  });
}

async function doMoveNode(chapterId, path, beforePath, parentPath) {
  const r = await api(`/api/books/${S.bookId}/node/move`, "POST", {
    chapter_id: chapterId, dom_path: path, before_path: beforePath, parent_path: parentPath });
  S.nodes[chapterId] = r.nodes;
  applyState(r.state); renderAll(); reloadPreview();
  toast("节点已移动；重跑检查: " + r.checks_ran.map(labelOf).join("、"));
}

/* ---------------- 预览定位 ---------------- */
function selectChapter(cid) {
  S.currentChapter = cid;
  const ch = S.chapters.find((c) => c.id === cid);
  if (!ch) return;
  $("#preview").src = `/api/books/${S.bookId}/preview/${encodeURI(ch.href)}`;
  renderTree();
}

function reloadPreview() {
  const f = $("#preview");
  if (f.src) f.src = f.src;  // 强制刷新以反映最新编辑
}

function postPreview(msg) {
  const w = $("#preview").contentWindow;
  if (w) w.postMessage(msg, "*");
}

function setListSelectMode(on) {
  S.listSelectMode = on;
  $("#btnListSelect").classList.toggle("active", on);
  postPreview({ type: "listSelectMode", on });
}

function sendListMarks() {
  const cid = S.currentChapter;
  const data = S.candidates[cid] || { candidates: [], lists: [] };
  postPreview({
    type: "listMarks",
    candidates: (data.candidates || []).map((c) => c.item_paths),
    lists: data.lists || [],
  });
}

$("#btnListSelect").onclick = () => {
  setListSelectMode(!S.listSelectMode);
  if (S.listSelectMode)
    toast("框选模式：依次点击连续区间的第一个和最后一个节点（同父兄弟）");
};

$("#preview").addEventListener("load", () => {
  if (S.pendingHighlight) {
    $("#preview").contentWindow.postMessage(
      { type: "highlight", path: S.pendingHighlight }, "*");
    S.pendingHighlight = null;
  }
  postPreview({ type: "listSelectMode", on: S.listSelectMode });
  sendListMarks();
});

window.addEventListener("message", (e) => {
  if (!e.data || e.data.type === "locate") {
    if (e.data.type !== "locate") return;
    // 预览中点击 → 向上回溯找到最近的已索引节点
    let path = e.data.path;
    const cid = S.currentChapter;
    const paths = new Set((S.nodes[cid] || []).map((n) => n.dom_path));
    while (path && !paths.has(path)) {
      const i = path.lastIndexOf("/");
      path = i > 0 ? path.slice(0, i) : "";
    }
    if (path) selectNode(cid, path, false);
    else toast("点击位置不是可索引的阅读节点", true);
    return;
  }
  if (e.data.type === "listPick") {
    handleListPick(e.data);
  }
});

function handleListPick(d) {
  const cid = S.currentChapter;
  setListSelectMode(false);
  if (!d.sameParent || !d.paths || d.paths.length < 1) {
    toast("框选的两个节点必须是同一父节点下的连续兄弟；请重新框选", true);
    return;
  }
  // 只保留可作为列表项/拦截元素的已索引节点
  const indexed = new Set((S.nodes[cid] || []).map((n) => n.dom_path));
  const paths = d.paths.filter((p) => indexed.has(p));
  if (paths.length < 1) { toast("框选范围内没有可组合的节点", true); return; }
  openListWorkspaceCreate(cid, paths, null);
}

function highlightInPreview(path) {
  const w = $("#preview").contentWindow;
  if (!w) return;
  w.postMessage({ type: "highlight", path }, "*");
}

/* ---------------- 节点选择与编辑器 ---------------- */
function findNode(cid, path) {
  return (S.nodes[cid] || []).find((n) => n.dom_path === path);
}

function selectNode(cid, path, sendToPreview = true) {
  if (cid !== S.currentChapter) {
    S.pendingHighlight = path;
    selectChapter(cid);
  }
  S.selected = { chapterId: cid, path };
  renderTree();
  if (sendToPreview) highlightInPreview(path);
  let n = findNode(cid, path);
  if (!n && path === "html[1]") {
    // <html> 根元素不在阅读节点表中，构造伪节点以编辑其 lang
    n = { tag: "html", dom_path: "html[1]", lang: S.htmlLang[cid] || null,
          el_id: null, text: "<html> 根元素" };
  }
  if (n) openEditor(n);
}

function openEditor(n) {
  $("#editor").classList.add("open");
  $("#edPath").textContent = n.dom_path;
  const F = [];
  F.push(`<div>类型：<b>${esc(n.tag)}</b>${n.el_id ? "　id: <code>" + esc(n.el_id) + "</code>" : ""}</div>`);
  if (n.tag.match(/^h[1-6]$/)) {
    F.push(`<label>标题级别 <select id="f_level">${[1,2,3,4,5,6].map((l) =>
      `<option value="${l}"${l === n.level ? " selected" : ""}>h${l}</option>`).join("")}</select></label>`);
  }
  F.push(`<label>语言 lang <input id="f_lang" value="${esc(n.lang || "")}" placeholder="如 zh-CN / en / fr"></label>`);
  if (n.tag === "img")
    F.push(`<label>替代文本 alt <input id="f_alt" value="${esc(n.alt ?? "")}" placeholder="装饰图可留空"></label>`);
  F.push(`<label>epub:type（地标/语义） <input id="f_etype" list="etypes" value="${esc(n.epub_type || "")}"
    placeholder="如 chapter / sidebar / footnote"><datalist id="etypes">
    <option value="chapter"><option value="sidebar"><option value="footnote"><option value="footnotes">
    <option value="bodymatter"><option value="cover"><option value="title-page"><option value="toc">
    </datalist></label>`);
  if (n.tag === "table")
    F.push(`<button id="f_table_ws" type="button">打开表格工作区（表头/合并/关联）</button>`);
  if (n.tag === "ul" || n.tag === "ol")
    F.push(`<button id="f_list_ws" type="button">打开列表工作区（层级/接续/起始序号）</button>`);
  $("#edFields").innerHTML = F.join("");
  const twsBtn = $("#f_table_ws");
  if (twsBtn) twsBtn.onclick = () => openTableWorkspace(S.selected.chapterId, n.dom_path);
  const lwsBtn = $("#f_list_ws");
  if (lwsBtn) lwsBtn.onclick = () => openListWorkspaceEdit(S.selected.chapterId, n.dom_path);
  $("#edSave").onclick = () => saveEditor(n);
  $("#edLocate").onclick = () => highlightInPreview(n.dom_path);
}

async function saveEditor(n) {
  const updates = {};
  const lvl = $("#f_level");
  if (lvl && Number(lvl.value) !== n.level) updates.heading_level = Number(lvl.value);
  const lang = $("#f_lang");
  if (lang && lang.value.trim() !== (n.lang || "")) updates.lang = lang.value.trim();
  const alt = $("#f_alt");
  if (alt && alt.value !== (n.alt ?? "")) updates.alt = alt.value;
  const et = $("#f_etype");
  if (et && et.value.trim() !== (n.epub_type || "")) updates.epub_type = et.value.trim();
  if (!Object.keys(updates).length) { toast("没有改动"); return; }
  const r = await api(`/api/books/${S.bookId}/node/update`, "POST", {
    chapter_id: S.selected.chapterId, dom_path: n.dom_path, updates });
  S.nodes[S.selected.chapterId] = r.nodes;
  S.htmlLang[S.selected.chapterId] = r.html_lang;
  applyState(r.state); renderAll(); reloadPreview();
  $("#editor").classList.remove("open");
  toast("已保存；重跑检查: " + (r.checks_ran.map(labelOf).join("、") || "无"));
}

/* ---------------- 朗读顺序 ---------------- */
function renderOrder() {
  const box = $("#orderList");
  let html = "", i = 0;
  for (const ch of S.chapters) {
    html += `<div class="group-head">📄 ${esc(ch.title)}</div>`;
    for (const n of S.nodes[ch.id] || []) {
      i++;
      html += `<div class="item" data-cid="${ch.id}" data-path="${esc(n.dom_path)}">
        <span class="ordernum">${i}</span>${tagBadge(n)}${nodeLabel(n)}${issueFlags(ch.href, n.dom_path)}</div>`;
    }
  }
  box.innerHTML = html || '<div class="item">（无内容）</div>';
  box.querySelectorAll(".item[data-path]").forEach((el) => {
    el.onclick = () => selectNode(Number(el.dataset.cid), el.dataset.path);
  });
}

/* ---------------- 问题 ---------------- */
function labelOf(check) { return S.checkLabels[check] || check; }

function renderIssues() {
  $("#issueCount").textContent = S.issues.length || "";
  const box = $("#issueList");
  if (!S.issues.length) { box.innerHTML = '<div class="item">🎉 没有未解决的问题</div>'; return; }
  const groups = {};
  S.issues.forEach((it) => (groups[it.check_name] ??= []).push(it));
  let html = "";
  for (const [check, items] of Object.entries(groups)) {
    html += `<div class="group-head">${esc(labelOf(check))}（${items.length}）</div>`;
    for (const it of items) {
      html += `<div class="item sev-${it.severity}" data-href="${esc(it.chapter_href || "")}"
        data-path="${esc(it.node_path || "")}" data-check="${esc(it.check_name)}">
        <div>${esc(it.message)}</div>
        <div class="sub">${esc(it.chapter_href || "全书")} ${it.node_path ? "· " + esc(it.node_path) : ""}</div></div>`;
    }
  }
  box.innerHTML = html;
  box.querySelectorAll(".item[data-href]").forEach((el) => {
    el.onclick = () => {
      const ch = S.chapters.find((c) => c.href === el.dataset.href);
      if (!ch) return;
      if (el.dataset.check === "table_a11y" && el.dataset.path)
        openTableWorkspace(ch.id, el.dataset.path);  // 表格问题 → 打开表格工作区
      else if (el.dataset.check === "list_a11y")
        openListIssue(ch.id, el.dataset.path);       // 列表问题 → 打开列表工作区
      else if (el.dataset.path) selectNode(ch.id, el.dataset.path);
      else selectChapter(ch.id);
    };
  });
}

/* ---------------- 地标 ---------------- */
function renderLandmarks() {
  const box = $("#landmarkList");
  if (!S.landmarks.length) { box.innerHTML = '<div class="item">（暂无地标，可在节点上设置 epub:type）</div>'; return; }
  box.innerHTML = S.landmarks.map((lm, i) => {
    const ch = S.chapters.find((c) => c.href === lm.chapter);
    return `<div class="item">
      <span class="tag-badge">${esc(lm.tag)}</span><b>${esc(lm.epub_type)}</b> ${esc(lm.text)}
      <div class="sub">${esc(lm.chapter)} · ${esc(lm.path)}
      ${ch ? `<a href="#" data-lm="${i}" class="lm-locate">定位</a>
              <a href="#" data-lm="${i}" class="lm-del del-landmark">删除地标</a>` : ""}</div></div>`;
  }).join("");
  box.querySelectorAll(".lm-locate").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      const lm = S.landmarks[Number(a.dataset.lm)];
      const ch = S.chapters.find((c) => c.href === lm.chapter);
      if (ch) selectNode(ch.id, lm.path);
    };
  });
  box.querySelectorAll(".lm-del").forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      const lm = S.landmarks[Number(a.dataset.lm)];
      const ch = S.chapters.find((c) => c.href === lm.chapter);
      const r = await api(`/api/books/${S.bookId}/node/update`, "POST", {
        chapter_id: ch.id, dom_path: lm.path, updates: { epub_type: "" } });
      S.nodes[ch.id] = r.nodes;
      S.htmlLang[ch.id] = r.html_lang;
      applyState(r.state); renderAll(); reloadPreview();
      toast("地标已删除；重跑检查: " + r.checks_ran.map(labelOf).join("、"));
    };
  });
}

/* ---------------- 变更记录 ---------------- */
function renderChanges() {
  const box = $("#changeList");
  if (!S.changes.length) { box.innerHTML = '<div class="item">（尚无修改）</div>'; return; }
  box.innerHTML = S.changes.map((c) =>
    `<div class="item ${c.undone ? "change-undone" : ""}">
      <div>${esc(c.summary)}</div><div class="sub">${esc(c.ts)}${c.undone ? " · 已撤销" : ""}</div>
    </div>`).join("");
}

/* ---------------- 顶部按钮 ---------------- */
$("#bookSelect").onchange = (e) => e.target.value && openBook(Number(e.target.value));
$("#btnImport").onclick = () => $("#fileInput").click();
$("#fileInput").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const r = await fetch("/api/import", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) { toast(d.error, true); return; }
  await loadBooks();
  await openBook(d.book_id);
  toast("导入完成");
};
$("#btnSample").onclick = async () => {
  const d = await api("/api/load_sample", "POST");
  await loadBooks();
  await openBook(d.book_id);
  toast("样例书已载入");
};
$("#btnUndo").onclick = async () => {
  const r = await api(`/api/books/${S.bookId}/undo`, "POST");
  applyState(r.state);
  S.nodes = {}; S.htmlLang = {};
  await Promise.all(S.chapters.map(async (c) => {
    const d = await api(`/api/books/${S.bookId}/chapters/${c.id}/nodes`);
    S.nodes[c.id] = d.nodes; S.htmlLang[c.id] = d.html_lang;
  }));
  renderAll(); reloadPreview();
  toast("已撤销: " + r.undone);
};
$("#btnExportEpub").onclick = () => S.bookId && (location.href = `/api/books/${S.bookId}/export/epub`);
$("#btnExportChanges").onclick = () => S.bookId && (location.href = `/api/books/${S.bookId}/export/changes`);
$("#btnExportReport").onclick = () => S.bookId && (location.href = `/api/books/${S.bookId}/export/report`);

/* ---------------- 标签页 ---------------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tabpage").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("#tab-" + t.dataset.tab).classList.add("active");
  };
});

loadBooks();
