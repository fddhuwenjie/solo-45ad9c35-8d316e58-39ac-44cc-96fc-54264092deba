/* 复杂表格无障碍工作区 —— 网格视图 + 表头编辑 + 批量推断 + 逐格朗读预览。
   依赖 app.js 中的全局：S / api / toast / esc / applyState / renderAll / reloadPreview */
"use strict";

const T = {
  chapterId: null,
  path: null,
  edits: null,      // {caption, cells:{"r,c":{row,col,tag?,scope?}}, links:{"r,c":{row,col,keep,headers:[[r,c]..]}}, infer:[]}
  model: null,
  conflicts: [],
  selected: null,   // 选中的单元格下标
  linkMode: false,  // 点选关联表头模式
};

function twHasEdits() {
  const e = T.edits;
  return e.caption !== null || Object.keys(e.cells).length > 0 ||
         Object.keys(e.links).length > 0 || e.infer.length > 0;
}

function cellAt(r, c) {
  return T.model.cells.find((x) => x.row === r && x.col === c) || null;
}

function coordOfId(h) {
  const c = T.model.cells.find((x) => x.id === h);
  return c ? [c.row, c.col] : null;
}

async function openTableWorkspace(chapterId, path) {
  T.chapterId = chapterId;
  T.path = path;
  T.edits = { caption: null, cells: {}, links: {}, infer: [] };
  T.selected = null;
  T.linkMode = false;
  T.conflicts = [];
  T.model = await api(`/api/books/${S.bookId}/table/${chapterId}?path=${encodeURIComponent(path)}`);
  $("#tableModal").classList.add("open");
  renderTableWorkspace();
}

function closeTableWorkspace() {
  $("#tableModal").classList.remove("open");
}

/* 每次暂存编辑后向服务器请求预览（应用→计算→回滚），保证网格与朗读始终为服务端口径 */
async function twRefresh() {
  if (!twHasEdits()) {
    T.model = await api(`/api/books/${S.bookId}/table/${T.chapterId}?path=${encodeURIComponent(T.path)}`);
    T.conflicts = [];
  } else {
    const r = await api(`/api/books/${S.bookId}/table/preview`, "POST",
      { chapter_id: T.chapterId, path: T.path, edits: T.edits });
    T.model = r.model;
    T.conflicts = r.conflicts;
  }
  renderTableWorkspace();
}

/* ---------------- 渲染 ---------------- */
function renderTableWorkspace() {
  const m = T.model;
  $("#twPath").textContent = m.path;
  const cap = $("#twCaption");
  if (document.activeElement !== cap)
    cap.value = T.edits.caption !== null ? T.edits.caption : (m.caption || "");

  // 网格：rowspan/colspan 占位，只在每格的左上角渲染
  const occ = {};
  m.cells.forEach((c, i) => {
    for (let dr = 0; dr < c.rowspan; dr++)
      for (let dc = 0; dc < c.colspan; dc++)
        occ[(c.row + dr) + "," + (c.col + dc)] = i;
  });
  let html = "<table class='tw-grid'><tbody>";
  for (let r = 0; r < m.rows; r++) {
    html += "<tr>";
    for (let col = 0; col < m.cols; col++) {
      const i = occ[r + "," + col];
      if (i === undefined) { html += "<td class='tw-hole'></td>"; continue; }
      const c = m.cells[i];
      if (c.row !== r || c.col !== col) continue;  // 被上方/左方合并格占用
      const cls = ["tw-cell", "tw-" + c.tag];
      if (T.selected === i) cls.push("sel");
      if (T.selected !== null) {
        const sel = m.cells[T.selected];
        if (sel.covered_by.includes(i)) cls.push("hdr");   // 选中格的表头
        if (sel.covers.includes(i)) cls.push("cov");       // 选中表头覆盖的数据格
      }
      html += `<td class="${cls.join(" ")}" data-i="${i}"`
        + (c.rowspan > 1 ? ` rowspan="${c.rowspan}"` : "")
        + (c.colspan > 1 ? ` colspan="${c.colspan}"` : "") + ">"
        + `<span class="tw-tag">${c.tag}</span> ${esc(c.text) || "（空）"}`
        + (c.scope ? ` <span class="tw-scope">${esc(c.scope)}</span>` : "")
        + (c.id ? ` <span class="tw-id">#${esc(c.id)}</span>` : "")
        + `</td>`;
    }
    html += "</tr>";
  }
  html += "</tbody></table>";
  $("#twGridWrap").innerHTML = html;
  $("#twGridWrap").querySelectorAll(".tw-cell").forEach((el) => {
    el.onclick = () => twCellClick(Number(el.dataset.i));
  });
  renderTwSide();
}

function renderTwSide() {
  const m = T.model;
  let html = "";
  if (T.selected !== null) {
    const c = m.cells[T.selected];
    const key = c.row + "," + c.col;
    const linked = T.edits.links[key]
      ? T.edits.links[key].headers
      : c.headers.map(coordOfId).filter(Boolean);
    html += `<div class="tw-sec"><b>选中：第${c.row + 1}行第${c.col + 1}列</b>
      <span class="tag-badge">${c.tag}</span>${c.scope ? " scope=" + esc(c.scope) : ""}</div>`;
    html += `<div class="tw-sec">类型：
      <button class="tw-tagbtn${c.tag === "th" ? " on" : ""}" data-tag="th">th 表头</button>
      <button class="tw-tagbtn${c.tag === "td" ? " on" : ""}" data-tag="td">td 数据</button></div>`;
    html += `<div class="tw-sec"><label>scope <select id="twScope">`
      + ["", "row", "col", "rowgroup", "colgroup"].map((s) =>
        `<option value="${s}"${(c.scope || "") === s ? " selected" : ""}>${s || "（无）"}</option>`).join("")
      + `</select></label></div>`;
    html += `<div class="tw-sec">关联表头：${linked.length
      ? linked.map((rc) => {
          const t = cellAt(rc[0], rc[1]);
          return `<span class="tw-chip">第${rc[0] + 1}行${rc[1] + 1}列 ${esc(t ? t.text : "")}</span>`;
        }).join("")
      : "（无）"}
      <button id="twLinkMode">${T.linkMode ? "结束点选" : "点选关联表头"}</button>
      ${T.linkMode ? '<div class="sub">点击网格中的单元格，切换为当前格的表头；保存时自动生成稳定的 id/headers</div>' : ""}</div>`;
    html += `<div class="tw-sec"><b>朗读预览</b><div class="tw-reading">${esc(c.reading)}</div></div>`;
  } else {
    html += '<div class="tw-sec sub">点击网格中的单元格进行编辑。黄色＝选中格的表头，绿色＝选中表头覆盖的数据格。</div>';
  }
  if (T.conflicts.length) {
    html += `<div class="tw-sec"><b>推断冲突（已保留人工选择）</b>` +
      T.conflicts.map((x) =>
        `<div class="tw-conflict">第${x.row}行第${x.col}列：${esc(x.reason)}</div>`).join("") + `</div>`;
  }
  if (m.issues.length) {
    html += `<div class="tw-sec"><b>当前问题（${m.issues.length}）</b>` +
      m.issues.map((i) => `<div class="tw-issue sev-${i.severity}">${esc(i.message)}</div>`).join("") + `</div>`;
  }
  html += `<div class="tw-sec"><b>逐格朗读</b>` +
    m.cells.filter((c) => c.tag === "td").map((c) =>
      `<div class="tw-reading">第${c.row + 1}行第${c.col + 1}列 → ${esc(c.reading)}</div>`).join("")
    + `</div>`;
  $("#twSide").innerHTML = html;

  $("#twSide").querySelectorAll(".tw-tagbtn").forEach((b) => {
    b.onclick = () => twSetTag(b.dataset.tag);
  });
  const sc = $("#twScope");
  if (sc) sc.onchange = () => twSetScope(sc.value);
  const lm = $("#twLinkMode");
  if (lm) lm.onclick = twToggleLinkMode;
}

/* ---------------- 交互 ---------------- */
function twCellClick(i) {
  if (T.linkMode && T.selected !== null && i !== T.selected) {
    const sel = T.model.cells[T.selected];
    const key = sel.row + "," + sel.col;
    const c = T.model.cells[i];
    const list = T.edits.links[key].headers;
    const k = list.findIndex((x) => x[0] === c.row && x[1] === c.col);
    if (k >= 0) list.splice(k, 1); else list.push([c.row, c.col]);
    twRefresh();
    return;
  }
  T.selected = i;
  renderTableWorkspace();
}

function twSetTag(tag) {
  const c = T.model.cells[T.selected];
  const key = c.row + "," + c.col;
  T.edits.cells[key] = Object.assign(T.edits.cells[key] || { row: c.row, col: c.col }, { tag });
  twRefresh();
}

function twSetScope(scope) {
  const c = T.model.cells[T.selected];
  const key = c.row + "," + c.col;
  T.edits.cells[key] = Object.assign(T.edits.cells[key] || { row: c.row, col: c.col }, { scope });
  twRefresh();
}

function twToggleLinkMode() {
  if (!T.linkMode && T.selected !== null) {
    const c = T.model.cells[T.selected];
    const key = c.row + "," + c.col;
    if (!T.edits.links[key]) {
      const keep = [];   // 无法解析到表内坐标的既有引用（如表外/失效 id），保存时原样保留
      const headers = [];
      for (const h of c.headers) {
        const rc = coordOfId(h);
        if (rc) headers.push(rc); else keep.push(h);
      }
      T.edits.links[key] = { row: c.row, col: c.col, keep, headers };
    }
    T.linkMode = true;
  } else {
    T.linkMode = false;
  }
  renderTableWorkspace();
}

$("#twCaption").onchange = (e) => { T.edits.caption = e.target.value; twRefresh(); };
$("#twInferCol").onclick = () => {
  if (!T.edits.infer.includes("col")) T.edits.infer.push("col");
  twRefresh();
};
$("#twInferRow").onclick = () => {
  if (!T.edits.infer.includes("row")) T.edits.infer.push("row");
  twRefresh();
};
$("#twClose").onclick = closeTableWorkspace;
$("#tableModal").onclick = (e) => { if (e.target.id === "tableModal") closeTableWorkspace(); };

$("#twSave").onclick = async () => {
  if (!twHasEdits()) { toast("没有改动"); return; }
  const r = await api(`/api/books/${S.bookId}/table/save`, "POST",
    { chapter_id: T.chapterId, path: T.path, edits: T.edits });
  S.nodes[T.chapterId] = r.nodes;
  S.htmlLang[T.chapterId] = r.html_lang;
  applyState(r.state);
  renderAll();
  reloadPreview();
  closeTableWorkspace();
  toast("表格已保存" +
    (r.conflicts.length ? `；${r.conflicts.length} 处推断冲突已保留人工选择` : ""));
};
