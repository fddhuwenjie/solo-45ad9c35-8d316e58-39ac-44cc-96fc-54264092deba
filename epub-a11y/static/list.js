/* 列表语义工作区：框选连续节点组合为 ul/ol；调整边界、缩进/提升、拆分/接续、
   设置起始序号；左右对照 DOM 变化与模拟朗读。依赖 app.js 的全局 S/api/toast/esc/
   applyState/renderAll/reloadPreview。 */
"use strict";

const L = {
  chapterId: null,
  mode: "edit",          // "create" | "edit"
  path: null,            // edit：列表路径；create：null
  model: null,
  selected: new Set(),   // create 模式下选中的兄弟节点路径
  blocks: {},            // create 模式：拦截元素路径 -> 处置方式
  selectedItem: null,    // edit 模式下选中的 li 路径
  draftType: "ul",
};

const LIST_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "img", "figure", "aside"];

/* ---------------- 打开工作区 ---------------- */
async function openListWorkspaceCreate(chapterId, paths, candidate) {
  L.chapterId = chapterId;
  L.mode = "create";
  L.path = null;
  L.model = null;
  L.selected = new Set(paths || []);
  L.blocks = {};
  L.selectedItem = null;
  if (candidate) {
    L.draftType = candidate.suggested || "ul";
    (candidate.blocks || []).forEach((b) => { L.blocks[b.path] = b.placement || "into_prev"; });
  }
  $("#listModal").classList.add("open");
  await renderListWorkspace();
}

async function openListWorkspaceEdit(chapterId, path) {
  L.chapterId = chapterId;
  L.mode = "edit";
  L.path = path;
  L.selected = new Set();
  L.blocks = {};
  L.selectedItem = null;
  $("#listModal").classList.add("open");
  await loadListModel();
}

async function loadListModel() {
  L.model = await api(
    `/api/books/${S.bookId}/list/model/${L.chapterId}?path=${encodeURIComponent(L.path)}`);
  renderListWorkspace();
}

function closeListWorkspace() {
  $("#listModal").classList.remove("open");
}

/* ---------------- 渲染 ---------------- */
function renderListWorkspace() {
  const create = L.mode === "create";
  $("#lwMode").textContent = create ? "框选组合（新建列表）" : "编辑既有列表";
  $("#lwPath").textContent = L.path || "(预览中框选的连续节点)";
  $("#lwCreate").classList.toggle("hidden", !create);
  $("#lwStrip").parentElement.classList.toggle("hidden", !create);
  $("#lwTypeWrap").classList.remove("hidden");  // 两种模式都可切换 ul/ol
  // create：用 lwType 选类型；edit：用同一下拉触发 set_type
  $("#lwType").disabled = false;
  $("#lwType").value = create ? L.draftType : (L.model ? L.model.type : "ul");
  const isOl = $("#lwType").value === "ol";
  $("#lwStartWrap").classList.toggle("hidden", !isOl);
  if (create) {
    renderCreateMode();
  } else {
    renderEditMode();
  }
  $("#lwBefore").textContent = "";
  $("#lwAfter").textContent = "";
  renderReading();
  renderIssues();
}

function renderReading() {
  const r = L.model ? L.model.reading : "（组合后此处显示屏幕阅读器的模拟朗读：列表项数、序号与层级）";
  $("#lwReading").textContent = r;
}

function renderIssues() {
  const box = $("#lwIssues");
  if (!L.model || !L.model.issues || !L.model.issues.length) {
    box.innerHTML = L.model ? '<div class="sub">该列表当前没有结构性问题。</div>' : "";
    return;
  }
  box.innerHTML = '<b>当前问题（' + L.model.issues.length + '）</b>' +
    L.model.issues.map((i) =>
      `<div class="tw-issue sev-${i.severity}">${esc(i.message)}</div>`).join("");
}

/* ---- create 模式：选择区间 / 缺口 / 拦截元素 ---- */
async function renderCreateMode() {
  const nodes = S.nodes[L.chapterId] || [];
  const chosen = nodes.filter((n) => L.selected.has(n.dom_path));
  $("#lwItems").innerHTML = chosen.length
    ? chosen.map((n) =>
        `<div class="lw-pick">${tagBadge(n)}${nodeLabel(n)}
           <a href="#" class="lw-del" data-p="${esc(n.dom_path)}">移除</a></div>`).join("")
    : '<div class="sub">尚未框选节点。可在左侧预览中拖选连续段落，或从问题列表的“疑似列表”进入。</div>';
  $("#lwItems").querySelectorAll(".lw-del").forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); L.selected.delete(a.dataset.p); renderListWorkspace(); };
  });

  // 计算区间内未选中的“缺口”与拦截元素
  const gap = await computeGap(chosen);
  const gapBox = $("#lwGap"), blockBox = $("#lwBlocks");
  blockBox.innerHTML = "";
  if (!chosen.length) { gapBox.textContent = ""; return; }
  if (gap.error) { gapBox.textContent = "⚠ " + gap.error; $("#lwCreate").disabled = true; return; }
  $("#lwCreate").disabled = false;
  if (gap.blocks.length) {
    gapBox.textContent = `区间内有 ${gap.blocks.length} 个标题/图片/注释，需要指定处置方式，否则提交将被拒绝。`;
    blockBox.innerHTML = '<b>区间内的标题/图片/注释</b>' + gap.blocks.map((b) =>
      `<div class="lw-block"><span class="tag-badge">${esc(b.tag)}</span>${esc(b.text || b.tag)}
        <select data-p="${esc(b.path)}">
          <option value="into_prev">并入上一项（成为该项内容）</option>
          <option value="move_before">移到列表之前</option>
          <option value="move_after">移到列表之后</option>
        </select></div>`).join("");
    blockBox.querySelectorAll("select").forEach((sel) => {
      sel.value = L.blocks[sel.dataset.p] || "into_prev";
      L.blocks[sel.dataset.p] = sel.value;
      sel.onchange = () => { L.blocks[sel.dataset.p] = sel.value; };
    });
  } else {
    gapBox.textContent = "";
  }
  $("#lwItemTools").innerHTML = "";
  $("#lwPrev").innerHTML = ""; $("#lwNext").innerHTML = "";
}

async function computeGap(chosen) {
  if (chosen.length < 1) return { blocks: [] };
  const parents = new Set(chosen.map((n) => n.parent_path || ""));
  if (parents.size > 1)
    return { error: "所选节点不属于同一父节点（交叉嵌套），不能组合。" };
  const all = S.nodes[L.chapterId] || [];
  const sameParent = all.filter((n) => (n.parent_path || "") === (chosen[0].parent_path || ""));
  const idx = chosen.map((n) => sameParent.indexOf(n));
  if (idx.some((x) => x < 0)) return { error: "节点定位失败。" };
  const lo = Math.min(...idx), hi = Math.max(...idx);
  const span = sameParent.slice(lo, hi + 1);
  const missing = span.filter((n) => !L.selected.has(n.dom_path));
  if (missing.length)
    return { error: `区间不完整：缺少 ${missing.length} 个节点（如 ${missing[0].tag}「${(missing[0].text || "").slice(0, 10)}」），请把它们也纳入或缩小边界。` };
  const blocks = span.filter((n) => LIST_BLOCK_TAGS.includes(n.tag) ||
    (n.tag === "p" && !n.text));
  return { blocks, span };
}

/* ---- edit 模式：项目树 / 边界 / 操作 ---- */
function renderEditMode() {
  const m = L.model;
  $("#lwStart").value = m.start ?? "";
  $("#lwStart").disabled = m.type !== "ol";

  let html = "";
  m.items.forEach((it) => {
    const cls = ["lw-item", "lw-depth" + it.depth];
    if (L.selectedItem === it.path) cls.push("sel");
    if (it.illegal) cls.push("illegal");
    const num = m.type === "ol"
      ? `<span class="lw-ord" title="实际播报序号">${it.ordinal}${it.value != null ? "=" + esc(it.value) : ""}</span>`
      : '<span class="lw-ord">•</span>';
    const badges =
      (it.has_id ? '<span class="lw-flag" title="保留了 id">#id</span>' : "") +
      (it.has_link ? '<span class="lw-flag" title="含链接">🔗</span>' : "") +
      (it.has_note ? '<span class="lw-flag" title="含脚注/语义标记">📎</span>' : "") +
      (it.has_style ? '<span class="lw-flag" title="含行内样式">🎨</span>' : "");
    html += `<div class="${cls.join(" ")}" data-p="${esc(it.path)}">
      ${num}<span class="lw-text">${esc(it.text || "(空)")}</span>${badges}
      ${it.visible_number != null ? `<span class="lw-marker" title="可见编号与实际序号不符">可见「${esc(it.marker)}」</span>` : ""}
    </div>`;
  });
  $("#lwItems").innerHTML = html || '<div class="sub">（空列表）</div>';
  $("#lwItems").querySelectorAll(".lw-item").forEach((el) => {
    el.onclick = () => { L.selectedItem = el.dataset.p; renderEditMode(); };
  });

  renderItemTools();
  renderBoundaries();
}

function renderItemTools() {
  const m = L.model;
  const sel = m.items.find((i) => i.path === L.selectedItem);
  const tools = [];
  tools.push(`<button id="lwIndent" title="缩进为上一项的子项">→ 缩进</button>`);
  tools.push(`<button id="lwLift" title="提升到外层列表">← 提升</button>`);
  tools.push(`<button id="lwSplit" title="从该项起拆成第二个列表">✂ 拆分</button>`);
  tools.push(`<button id="lwShrink" title="把末项移出列表成为段落">缩界</button>`);
  if (m.type === "ol" && sel)
    tools.push(`<input id="lwValue" type="number" placeholder="该项 value" title="设置该项 value" style="width:84px">
                <button id="lwValueBtn">设 value</button>`);
  $("#lwItemTools").innerHTML = tools.join(" ");
  $("#lwIndent").onclick = () => op("indent", { path: L.selectedItem });
  $("#lwLift").onclick = () => op("outdent", { path: L.selectedItem });
  $("#lwSplit").onclick = () => op("split", { path: L.selectedItem });
  $("#lwShrink").onclick = () => op("shrink", { side: "after" });
  const vb = $("#lwValueBtn");
  if (vb) vb.onclick = () => op("set_value",
    { path: L.selectedItem, value: Number($("#lwValue").value) });
}

function neighborHtml(side, nb) {
  if (!nb) return `<div class="lw-b-${side} sub">${side === "prev" ? "↑ 前面" : "↓ 后面"}已是章节边界</div>`;
  let action = "";
  if (nb.action === "join")
    action = `<button data-side="${side}" class="lw-join">接续相邻 ${esc(nb.tag)}（${nb.count} 项）</button>`;
  else if (nb.action === "extend")
    action = `<button data-side="${side}" class="lw-extend">纳入此节点</button>`;
  else if (nb.action === "blocked")
    action = `<select data-side="${side}" class="lw-blocksel">
        <option value="">处置${esc(nb.tag)}…</option>
        <option value="into_prev">并入相邻项</option>
        <option value="move_before">移到列表前</option>
        <option value="move_after">移到列表后</option>
      </select>`;
  else action = '<span class="sub">（不适合作为列表项）</span>';
  return `<div class="lw-b-${side}"><span class="tag-badge">${esc(nb.tag)}</span>${esc(nb.text || "")} ${action}</div>`;
}

function renderBoundaries() {
  const b = L.model.boundaries || {};
  $("#lwPrev").innerHTML = neighborHtml("prev", b.prev);
  $("#lwNext").innerHTML = neighborHtml("next", b.next);
  $("#lwPrev").querySelectorAll(".lw-join,.lw-extend").forEach((btn) => {
    btn.onclick = () => op(btn.classList.contains("lw-join") ? "join" : "extend",
      { side: "prev" });
  });
  $("#lwNext").querySelectorAll(".lw-join,.lw-extend").forEach(btn => {
    btn.onclick = () => op(btn.classList.contains("lw-join") ? "join" : "extend",
      { side: "next" });
  });
  document.querySelectorAll(".lw-blocksel").forEach((sel) => {
    sel.onchange = () => {
      if (!sel.value) return;
      op("extend", { side: sel.dataset.side, placement: sel.value });
    };
  });
}

/* ---------------- 提交操作 ---------------- */
async function createFromSelection() {
  if (!L.selected.size) { toast("请先框选至少一个节点", true); return; }
  const body = {
    paths: [...L.selected],
    type: $("#lwType").value,
    strip_markers: $("#lwStrip").checked,
    blocks: L.blocks,
  };
  if (body.type === "ol" && $("#lwStart").value)
    body.start = Number($("#lwStart").value);
  let r;
  try {
    r = await api(`/api/books/${S.bookId}/list/create`, "POST", body);
  } catch (e) { await renderListWorkspace(); return; }
  showDiff(r.before_pretty, r.after_pretty);
  afterListCommit(r);
  L.mode = "edit"; L.path = r.new_path; L.model = r.model; L.selectedItem = null;
  renderListWorkspace();
  toast("已组合为列表（可撤销）");
}

async function op(name, params) {
  if (!L.path) { toast("请先选择一个列表项", true); return; }
  if ((name === "indent" || name === "outdent" || name === "split" || name === "set_value")
      && !L.selectedItem) { toast("请先在列表中点击一个项目", true); return; }
  let r;
  try {
    r = await api(`/api/books/${S.bookId}/list/op`, "POST",
      { chapter_id: L.chapterId, path: L.path, op: name, params });
  } catch (e) { await loadListModelSafe(); return; }
  showDiff(r.before_pretty, r.after_pretty);
  // split 后新列表是第二个；join 后主体可能就是 L.path
  if (r.model) { L.model = r.model; L.path = r.new_path || L.path; }
  afterListCommit(r);
  renderListWorkspace();
  reloadPreview();
  toast("列表已更新（可撤销）");
}

async function loadListModelSafe() {
  try { await loadListModel(); } catch (e) { /* 路径失效时关闭 */ closeListWorkspace(); }
}

function afterListCommit(r) {
  S.nodes[L.chapterId] = r.nodes;
  if (r.html_lang !== undefined) S.htmlLang[L.chapterId] = r.html_lang;
  applyState(r.state);
  renderAll();
  refreshCandidates(L.chapterId);
  reloadPreview();
}

function showDiff(before, after) {
  $("#lwBefore").textContent = before || "";
  $("#lwAfter").textContent = after || "";
}

/* ---------------- 控件绑定 ---------------- */
$("#lwClose").onclick = closeListWorkspace;
$("#listModal").onclick = (e) => { if (e.target.id === "listModal") closeListWorkspace(); };
$("#lwCreate").onclick = createFromSelection;
$("#lwType").onchange = async () => {
  if (L.mode === "create") {
    L.draftType = $("#lwType").value;
    renderListWorkspace();
  } else if (L.model && $("#lwType").value !== L.model.type) {
    await op("set_type", { type: $("#lwType").value });
  }
};
$("#lwStart").onchange = async () => {
  if (L.mode === "edit" && L.model && L.model.type === "ol") {
    const v = $("#lwStart").value;
    await op("set_start", { value: v === "" ? null : Number(v) });
  }
};

// 键盘：选中项 →/← 缩进提升
document.addEventListener("keydown", (e) => {
  if (!$("#listModal").classList.contains("open") || L.mode !== "edit" || !L.selectedItem) return;
  if (e.key === "ArrowRight") { e.preventDefault(); op("indent", { path: L.selectedItem }); }
  if (e.key === "ArrowLeft") { e.preventDefault(); op("outdent", { path: L.selectedItem }); }
});

/* ---------------- 预览框选消息（app.js 转发） ---------------- */
window.listHandleSelection = async function (chapterId, paths) {
  // 过滤为可作为列表项的连续兄弟（p / li / div），失败则提示
  await openListWorkspaceCreate(chapterId, paths, null);
};

/* 从“疑似列表”问题一键进入组合工作区（携带候选信息） */
window.openCandidateList = async function (chapterId, candidateId) {
  const data = await api(`/api/books/${S.bookId}/list/candidates/${chapterId}`);
  const cand = data.candidates.find((c) => c.id === candidateId);
  if (!cand) { toast("候选已不存在（可能已修复）", true); return; }
  await openListWorkspaceCreate(chapterId, cand.item_paths, cand);
};

/* 列表类问题：路径落在既有列表内 → 编辑工作区；否则按候选打开组合工作区 */
window.openListIssue = async function (chapterId, path) {
  if (!path) { selectChapterById(chapterId); return; }
  const data = S.candidates[chapterId] ||
    await api(`/api/books/${S.bookId}/list/candidates/${chapterId}`);
  S.candidates[chapterId] = data;
  // 命中既有列表（或其后代）→ 编辑该列表
  const hit = (data.lists || []).find((lp) => path === lp || path.startsWith(lp + "/"));
  if (hit) { await openListWorkspaceEdit(chapterId, hit); return; }
  // 命中候选（候选 id 是其首节点路径）→ 携带候选组合
  const cand = (data.candidates || []).find((c) => c.id === path);
  if (cand) { await openListWorkspaceCreate(chapterId, cand.item_paths, cand); return; }
  // 孤立 li/拦截元素等：定位到预览
  const ch = S.chapters.find((c) => c.id === chapterId);
  if (ch && S.currentChapter !== chapterId) selectChapter(chapterId);
  highlightInPreview(path);
  toast("请在预览中框选连续节点，或用“框选组合列表”按钮组合", false);
};

function selectChapterById(chapterId) {
  const ch = S.chapters.find((c) => c.id === chapterId);
  if (ch) selectChapter(chapterId);
}
