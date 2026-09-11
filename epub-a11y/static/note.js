/* 注释关系图工作区：正文引用 ↔ 脚注/尾注 两栏连线。
   - 点击任一端在排版预览中定位；节点可标成 noteref/footnote/endnote、重新连线；
   - 为多次引用分别建立回程目标；提交前试听朗读顺序；
   - 批量套用只接受编号连续且关系唯一的候选；环路/跨书目/删除仍被引用的注释
     由后端阻止并把来源列在问题区。
   依赖 app.js 全局：S / api / toast / esc / applyState / renderAll / reloadPreview /
   selectChapter / highlightInPreview。 */
"use strict";

const N = {
  model: null,
  selected: null,       // 选中节点 {side:"ref"|"note", key}
  linkFrom: null,       // 连线模式：先点引用 {side,key}，再点注释
  edits: null,          // 暂存编辑
  playing: false,
  playLine: -1,
  playTimer: null,
};

const NOTE_KINDS = [
  { v: "noteref", label: "引用 noteref" },
  { v: "footnote", label: "脚注 footnote" },
  { v: "endnote", label: "尾注 endnote" },
];

function noteChapterName(href) {
  const ch = (N.model?.chapters || []).find((c) => c.href === href);
  return ch ? ch.name : (href || "").split("/").pop();
}
function noteByKey(k) { return (N.model.notes || []).find((n) => n.key === k) || null; }
function refByKey(k) { return (N.model.refs || []).find((r) => r.key === k) || null; }
function noteNode(key) {
  return document.querySelector(`.nt-node[data-key="${CSS.escape(key)}"]`);
}

async function openNoteWorkspace(focusKey) {
  N.edits = { marks: [], unmarks: [], links: [], unlinks: [], backlinks: [], deletes: [] };
  N.selected = null;
  N.linkFrom = null;
  N.playLine = -1;
  stopPlay();
  await loadNoteModel();
  $("#noteModal").classList.add("open");
  renderNoteWorkspace();
  if (focusKey) {
    N.selected = noteByKey(focusKey)
      ? { side: "note", key: focusKey }
      : (refByKey(focusKey) ? { side: "ref", key: focusKey } : null);
    renderNoteWorkspace();
  }
}

function closeNoteWorkspace() {
  stopPlay();
  $("#noteModal").classList.remove("open");
}

async function loadNoteModel() {
  N.model = await api(`/api/books/${S.bookId}/note/model`);
}

function nHasEdits() {
  const e = N.edits;
  return e.marks.length + e.unmarks.length + e.links.length + e.unlinks.length
    + e.backlinks.length + e.deletes.length > 0;
}

/* ---------------- 渲染 ---------------- */
function renderNoteWorkspace() {
  renderNoteColumns();
  drawNoteLinks();
  renderNoteDetail();
  renderNoteIssues();
  renderNoteBatches();
  renderNoteSequence();
  $("#ntSave").classList.toggle("disabled", !nHasEdits());
}

function nodeFlags(node) {
  const f = [];
  if (node.external) f.push('<span class="nt-flag nt-ext" title="跨书目链接">🌐</span>');
  if (node.target_missing) f.push('<span class="nt-flag" title="目标缺失">✖</span>');
  if (node.nested) f.push('<span class="nt-flag nt-warn" title="嵌套注释">⧉</span>');
  if (node.orphan) f.push('<span class="nt-flag nt-warn" title="孤立（无引用）">孤</span>');
  if (node.mixed_plain) f.push('<span class="nt-flag nt-warn" title="混入注释区的普通列表项">杂</span>');
  return f.join("");
}

function refSorters() {
  return (a, b) => a.spine_pos - b.spine_pos || a.chapter.localeCompare(b.chapter) || a.order - b.order;
}

function renderNoteColumns() {
  const refs = [...N.model.refs].sort(refSorters());
  const notes = [...N.model.notes].sort(
    (a, b) => a.spine_pos - b.spine_pos || a.chapter.localeCompare(b.chapter)
      || (a.number ?? 1e9) - (b.number ?? 1e9) || a.order - b.order);

  const refBox = $("#ntRefs"), noteBox = $("#ntNotes");
  refBox.innerHTML = groupHtml(refs, "ref");
  noteBox.innerHTML = groupHtml(notes, "note");
  refBox.querySelectorAll(".nt-node").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      onNoteNodeClick("ref", el.dataset.key);
    };
  });
  noteBox.querySelectorAll(".nt-node").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      onNoteNodeClick("note", el.dataset.key);
    };
  });
}

function groupHtml(nodes, side) {
  let html = "", last = null;
  for (const n of nodes) {
    if (n.chapter !== last) {
      html += `<div class="nt-group">📄 ${esc(noteChapterName(n.chapter))}</div>`;
      last = n.chapter;
    }
    html += nodeHtml(n, side);
  }
  return html || '<div class="nt-empty">（无）</div>';
}

function nodeHtml(n, side) {
  const isRef = side === "ref";
  const cls = ["nt-node", "nt-" + side];
  if (N.selected?.key === n.key) cls.push("sel");
  if (N.linkFrom?.key === n.key) cls.push("linkfrom");
  if (!n.declared) cls.push("undecl");
  if (isRef && n.external) cls.push("ext");
  const kind = isRef ? "noteref" : (n.note_type || "footnote");
  const num = isRef
    ? `<span class="nt-num">${esc(n.marker || ("#" + (n.number ?? "?")))}</span>`
    : `<span class="nt-num">${esc(n.number != null ? String(n.number) : (n.el_id ? "#" + n.el_id : "?"))}</span>`;
  const incoming = isRef ? null : n.ref_keys.length;
  const bl = isRef ? null : n.backlinks.length;
  const text = esc((n.context || n.text || "").slice(0, 42));
  const id = n.el_id ? ` <code>${esc(n.el_id)}</code>` : ' <code class="nt-autoid">（无 id）</code>';
  return `<div class="${cls.join(" ")}" data-key="${esc(n.key)}" title="${esc(n.path)}">
    <div class="nt-line">${num}
      <span class="nt-kind nt-kind-${kind}">${esc(kind)}</span>${id}${nodeFlags(n)}
    </div>
    <div class="nt-text">${text || "（空）"}</div>
    ${isRef
      ? `<div class="nt-sub">${n.target_key ? "→ " + esc(noteChapterName(noteByKey(n.target_key)?.chapter || "")) + " " + esc((noteByKey(n.target_key)?.el_id) || "")
          : (n.external ? "🌐 " + esc(n.href || "") : "— 未连线 —")}</div>`
      : `<div class="nt-sub">${incoming ? incoming + " 处引用" : "无引用"} · 回程 ${bl}${incoming && bl < incoming ? " ⚠" : ""}</div>`}
  </div>`;
}

/* ---------------- 连线（SVG 贝塞尔曲线） ---------------- */
function drawNoteLinks() {
  const svg = $("#ntLines");
  const body = $("#ntBody").getBoundingClientRect();
  svg.setAttribute("width", body.width);
  svg.setAttribute("height", Math.max($("#ntRefs").scrollHeight,
                                      $("#ntNotes").scrollHeight, body.height));
  let html = "";
  const drawn = new Set();
  for (const e of N.model.edges) {
    const k = e.ref_key + ">" + e.note_key;
    if (drawn.has(k)) continue;
    drawn.add(k);
    const a = noteNode(e.ref_key), b = noteNode(e.note_key);
    if (!a || !b) continue;
    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
    const x1 = ar.right - body.left, y1 = ar.top - body.top + ar.height / 2;
    const x2 = br.left - body.left, y2 = br.top - body.top + br.height / 2;
    const mx = (x1 + x2) / 2;
    const bad = refByKey(e.ref_key)?.external;
    html += `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"
      class="nt-link${bad ? " bad" : ""}" data-r="${esc(e.ref_key)}" data-n="${esc(e.note_key)}"/>`;
  }
  svg.innerHTML = html;
  svg.querySelectorAll("path").forEach((p) => {
    p.onclick = () => {
      N.selected = { side: "ref", key: p.dataset.r };
      renderNoteWorkspace();
    };
  });
}

/* ---------------- 交互 ---------------- */
function onNoteNodeClick(side, key) {
  if (N.linkFrom) {
    if (side === "note" && N.linkFrom.side === "ref") {
      stageLink(N.linkFrom.key, key);
    } else if (side === "ref" && N.linkFrom.side === "note") {
      stageLink(key, N.linkFrom.key);
    } else {
      N.linkFrom = { side, key };
    }
    renderNoteWorkspace();
    return;
  }
  N.selected = { side, key };
  renderNoteWorkspace();
}

function pushEdit(arr, item, keyFields) {
  // 同类操作去重（以 key 字段判等）
  const i = N.edits[arr].findIndex((x) => keyFields.every((f) => x[f] === item[f]));
  if (i >= 0) N.edits[arr].splice(i, 1);
  else N.edits[arr].push(item);
}

function stageLink(refKey, noteKey) {
  N.linkFrom = null;
  // 若已有该引用的连线，记为断开旧线
  const r = refByKey(refKey);
  if (r?.target_key)
    pushEdit("unlinks", { key: refKey }, ["key"]);
  pushEdit("links", { ref_key: refKey, note_key: noteKey },
           ["ref_key", "note_key"]);
  // 为该次引用建立独立回程目标（多次引用各自一条）
  pushEdit("backlinks", { ref_key: refKey, note_key: noteKey },
           ["ref_key", "note_key"]);
  toast("已暂存连线与回程目标，可继续调整或试听后提交");
}

async function ntRefresh(message) {
  if (!nHasEdits()) {
    await loadNoteModel();
  } else {
    const r = await api(`/api/books/${S.bookId}/note/preview`, "POST",
                        { edits: N.edits });
    N.model = r.model;
  }
  renderNoteWorkspace();
  if (message) toast(message);
}

/* ---------------- 侧栏：选中节点操作 ---------------- */
function renderNoteDetail() {
  const box = $("#ntDetail");
  if (!N.selected) {
    box.innerHTML = '<div class="sub">点击任一端节点：在预览中定位、标记类型或开始连线。<br>'
      + '连线方式：点「开始连线」后再点另一栏的目标节点；每次连线都会为该次引用建立独立回程目标。</div>';
    return;
  }
  const { side, key } = N.selected;
  const n = side === "ref" ? refByKey(key) : noteByKey(key);
  if (!n) { box.innerHTML = '<div class="sub">节点已不存在（可能刚被编辑）。</div>'; return; }
  const isRef = side === "ref";
  let html = `<div class="nt-dhead">${isRef ? "正文引用" : "注释"}　<code>${esc(n.path)}</code></div>`;
  html += `<div class="nt-drow">章节：${esc(noteChapterName(n.chapter))}${n.el_id ? "　id: <code>" + esc(n.el_id) + "</code>" : ""}</div>`;
  html += `<div class="nt-drow">可见：${esc(n.marker || ("#" + (n.number ?? "?")))}　声明：${n.declared ? "是" : "<b class='nt-warn'>否</b>"}</div>`;

  html += '<div class="nt-drow">标记为：';
  for (const k of NOTE_KINDS) {
    const active = (isRef && k.v === "noteref") || (!isRef && n.note_type === k.v);
    html += `<button class="nt-tagbtn${active ? " on" : ""}" data-mark="${k.v}">${k.label}</button>`;
  }
  if (!isRef && n.declared)
    html += `<button class="nt-tagbtn" data-unmark="${n.note_type}">取消声明</button>`;
  html += "</div>";

  html += '<div class="nt-drow"><button id="ntLocate">在预览中定位</button> '
    + `<button id="ntLink">${N.linkFrom ? "取消连线" : "开始连线"}</button></div>`;

  if (isRef) {
    const t = n.target_key ? noteByKey(n.target_key) : null;
    html += `<div class="nt-drow">当前连线：${t ? esc(t.note_type) + " #" + esc(t.el_id || "?") : "（无）"}</div>`;
    if (t) html += '<div class="nt-drow"><button id="ntUnlink">断开连线</button></div>';
    if (n.external)
      html += '<div class="nt-drow nt-blocked">🌐 跨书目链接，必须先断开才能改为本书注释</div>';
  } else {
    html += `<div class="nt-drow">引用（${n.ref_keys.length}）与回程（${n.backlinks.length}）：</div>`;
    n.ref_keys.forEach((rk) => {
      const r = refByKey(rk);
      const hasBl = n.backlinks.some((b) => b.ref_key === rk);
      html += `<div class="nt-backrow">${esc(r ? r.marker : "?")}
        <span class="sub">${esc(r ? noteChapterName(r.chapter) : "")}${r?.el_id ? " #" + esc(r.el_id) : ""}</span>
        ${hasBl
          ? `<button data-rmbl="${esc(rk)}">移除回程</button>`
          : `<button data-addbl="${esc(rk)}">建立回程</button>`}</div>`;
    });
    const used = new Set();
    const incoming = n.ref_keys.map((k) => refByKey(k)).filter(Boolean);
    if (incoming.some((r) => !r.el_id))
      html += '<div class="nt-drow nt-warn">部分引用没有 id，建立回程时将自动生成稳定 id。</div>';
    html += `<div class="nt-drow"><button id="ntDelete" class="nt-danger">删除该注释</button></div>`;
  }
  box.innerHTML = html;

  $("#ntLocate").onclick = () => locateNoteNode(n);
  const lb = $("#ntLink");
  if (lb) lb.onclick = () => {
    N.linkFrom = N.linkFrom ? null : { side, key };
    renderNoteWorkspace();
  };
  box.querySelectorAll("[data-mark]").forEach((b) => {
    b.onclick = () => {
      pushEdit("marks", { key, kind: b.dataset.mark }, ["key", "kind"]);
      ntRefresh();
    };
  });
  box.querySelectorAll("[data-unmark]").forEach((b) => {
    b.onclick = () => {
      pushEdit("unmarks", { key, kind: b.dataset.unmark }, ["key", "kind"]);
      ntRefresh();
    };
  });
  const ub = $("#ntUnlink");
  if (ub) ub.onclick = () => {
    pushEdit("unlinks", { key }, ["key"]);
    ntRefresh();
  };
  box.querySelectorAll("[data-addbl]").forEach((b) => {
    b.onclick = () => {
      pushEdit("backlinks", { ref_key: b.dataset.addbl, note_key: key },
               ["ref_key", "note_key"]);
      ntRefresh();
    };
  });
  box.querySelectorAll("[data-rmbl]").forEach((b) => {
    b.onclick = () => {
      pushEdit("backlinks", { ref_key: b.dataset.rmbl, note_key: key, remove: true },
               ["ref_key", "note_key", "remove"]);
      ntRefresh();
    };
  });
  const db = $("#ntDelete");
  if (db) db.onclick = () => {
    const sources = incomingSources(n);
    if (sources.length) {
      toast("删除被阻止：仍有引用指向该注释 —— " + sources.join("；"), true);
      return;
    }
    if (!confirm("删除注释「" + (n.text || n.el_id) + "」？可在提交后整批撤销。")) return;
    pushEdit("deletes", { key }, ["key"]);
    N.selected = null;
    ntRefresh();
  };
}

function incomingSources(n) {
  // 与后端同源口径：暂存预览模型里仍指向该注释的引用
  const out = [];
  for (const r of N.model.refs)
    if (r.target_key === n.key)
      out.push(noteChapterName(r.chapter) + " " + (r.marker || ""));
  return out;
}

function locateNoteNode(n) {
  const ch = S.chapters.find((c) => c.href === n.chapter);
  if (!ch) { toast("章节未在 spine 中", true); return; }
  if (S.currentChapter !== ch.id) selectChapter(ch.id);
  highlightInPreview(n.path);
}

/* ---------------- 问题 / 阻止来源 ---------------- */
function renderNoteIssues() {
  const box = $("#ntIssues");
  const issues = N.model.issues || [];
  let html = `<b>问题（${issues.length}）—— 与 EPUB 导出、变更明细、问题报告同源</b>`;
  const blocked = [];
  (N.model.blocks?.cycles || []).forEach((scc) =>
    blocked.push("注释环路：" + scc.map((k) => "#" + (noteByKey(k)?.el_id || "?")).join(" → ")));
  (N.model.blocks?.external || []).forEach((e) =>
    blocked.push("跨书目链接：" + noteChapterName(e.chapter) + " " + (e.marker || "") + " → " + e.href));
  (N.model.blocks?.unmatched || []).forEach((u) =>
    blocked.push("批量被阻止：" + noteChapterName(u.chapter) + " 注号 " + u.number + "（" + u.reason + "）"));
  if (blocked.length)
    html += blocked.map((t) => `<div class="nt-blocked">⛔ ${esc(t)}</div>`).join("");
  html += issues.map((i) =>
    `<div class="tw-issue sev-${i.severity}">${esc(i.message)}</div>`).join("")
    || '<div class="sub">🎉 关系图无问题</div>';
  box.innerHTML = html;
}

/* ---------------- 批量套用 ---------------- */
function renderNoteBatches() {
  const box = $("#ntBatches");
  const groups = N.model.batches || [];
  if (!groups.length) {
    box.innerHTML = '<div class="sub">没有可批量套用的候选（需编号连续且关系唯一）。</div>';
    return;
  }
  box.innerHTML = groups.map((g) => {
    const items = g.items.map((it) =>
      `<span class="nt-chip${it.applicable ? "" : " no"}">${it.number}</span>`).join("");
    return `<div class="nt-batch">
      <b>${g.note_type === "endnote" ? "尾注" : "脚注"} ${g.start}–${g.end}</b>
      ${items}
      <button data-batch="${esc(g.id)}" ${g.applicable ? "" : "disabled"}
        title="${g.applicable ? "标注双方 + 连线 + 每注一条回程" : "编号不连续或关系不唯一"}">
        批量套用${g.applicable ? "" : "（被阻止）"}</button>
      <div class="sub">${esc(noteChapterName(g.chapter))}</div></div>`;
  }).join("");
  box.querySelectorAll("[data-batch]").forEach((b) => {
    b.onclick = async () => {
      N.edits = { marks: [], unmarks: [], links: [], unlinks: [], backlinks: [],
                  deletes: [], batch_group: b.dataset.batch };
      const r = await api(`/api/books/${S.bookId}/note/preview`, "POST",
                          { edits: N.edits });
      N.model = r.model;
      renderNoteWorkspace();
      toast("已暂存批量套用，请试听顺序后提交");
    };
  });
}

/* ---------------- 试听顺序 ---------------- */
function renderNoteSequence() {
  const box = $("#ntSequence");
  const seq = N.model.sequence || [];
  box.innerHTML = seq.map((l, i) =>
    `<div class="nt-seq nt-seq-${l.kind}${i === N.playLine ? " on" : ""}" data-i="${i}">${esc(l.text)}</div>`
  ).join("") || '<div class="sub">（无注释引用）</div>';
  box.querySelectorAll(".nt-seq").forEach((el) => {
    el.onclick = () => {
      const l = seq[Number(el.dataset.i)];
      const ch = S.chapters.find((c) => c.href === l.chapter);
      if (ch) {
        if (S.currentChapter !== ch.id) selectChapter(ch.id);
        highlightInPreview(l.path);
      }
    };
  });
  $("#ntPlay").textContent = N.playing ? "⏸ 停止试听" : "▶ 试听顺序";
}

function stopPlay() {
  N.playing = false;
  if (N.playTimer) { clearTimeout(N.playTimer); N.playTimer = null; }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  N.playLine = -1;
}

$("#ntPlay").onclick = () => {
  if (N.playing) { stopPlay(); renderNoteSequence(); return; }
  const seq = N.model.sequence || [];
  if (!seq.length) { toast("没有可试听的顺序"); return; }
  N.playing = true;
  N.playLine = -1;
  const speak = window.speechSynthesis
    ? (t) => {
        const u = new SpeechSynthesisUtterance(t.replace(/[⚠↳↩]/g, ""));
        u.lang = "zh-CN";
        window.speechSynthesis.speak(u);
        return new Promise((res) => { u.onend = res; u.onerror = res; });
      }
    : (t) => new Promise((res) => setTimeout(res, 700));
  (async () => {
    for (let i = 0; i < seq.length && N.playing; i++) {
      N.playLine = i;
      renderNoteSequence();
      const line = seq[i];
      const ch = S.chapters.find((c) => c.href === line.chapter);
      if (ch) {
        if (S.currentChapter !== ch.id) selectChapter(ch.id);
        highlightInPreview(line.path);
      }
      await speak(line.text);
    }
    stopPlay();
    renderNoteSequence();
  })();
};

/* ---------------- 提交 / 放弃 ---------------- */
$("#ntSave").onclick = async () => {
  if (!nHasEdits() && !N.edits.batch_group) { toast("没有改动"); return; }
  let r;
  try {
    r = await api(`/api/books/${S.bookId}/note/save`, "POST", { edits: N.edits });
  } catch (e) {
    // 后端阻止（环路/跨书目/删除仍被引用）：丢弃暂存并刷新真实关系，来源在问题区
    await loadNoteModel();
    N.edits = { marks: [], unmarks: [], links: [], unlinks: [], backlinks: [], deletes: [] };
    renderNoteWorkspace();
    return;
  }
  N.model = r.model;
  N.edits = { marks: [], unmarks: [], links: [], unlinks: [], backlinks: [], deletes: [] };
  N.selected = null; N.linkFrom = null;
  applyState(r.state);
  renderAll();
  reloadPreview();
  renderNoteWorkspace();
  toast("注释关系已提交（可整批撤销，恢复原 ID 与链接）");
};

$("#ntReset").onclick = async () => {
  stopPlay();
  N.edits = { marks: [], unmarks: [], links: [], unlinks: [], backlinks: [], deletes: [] };
  N.selected = null; N.linkFrom = null;
  await loadNoteModel();
  renderNoteWorkspace();
};

$("#ntClose").onclick = closeNoteWorkspace;
$("#noteModal").onclick = (e) => { if (e.target.id === "noteModal") closeNoteWorkspace(); };
window.addEventListener("resize", () => {
  if ($("#noteModal").classList.contains("open")) drawNoteLinks();
});
