"""EPUB 无障碍阅读顺序校修工具 —— Flask 后端。

所有处理均在本机完成：EPUB 解压到 data/books/<id>/working.epub，
编辑直接作用于内存中的文档树并即时写回工作副本，可随时导出重新打开。
"""
import io
import json
import os
import posixpath
import re
import shutil

from flask import (Flask, jsonify, render_template, request, send_file,
                   send_from_directory, abort)

import db
import epublib
import listlib
from epublib import Book, CHECK_LABELS, CHECKS, AFFECTED_BY_ATTR, \
    AFFECTED_BY_MOVE, AFFECTED_BY_SPINE, apply_updates, move_node, undo_move, \
    reorder_spine, rebuild_landmarks_nav, find_by_path, dom_path, \
    apply_table_edits, preview_table_edits, restore_table, check_one_table, \
    table_model_json
from listlib import (candidates_json as list_candidates_json,
                     list_model_json, create_list, apply_op,
                     restore_region, pretty_region)
from sample_book import create_sample

# 列表语义检查（检查端与列表工作区共用 listlib 的同一组发现）
CHECKS["list_a11y"] = {"scope": "chapter", "fn": listlib.ch_list_a11y}

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
BOOKS_DIR = os.path.join(DATA, "books")

app = Flask(__name__)
BOOKS = {}  # book_id -> Book（内存缓存）


# ---------------- 基础 ----------------
def get_book(book_id):
    if book_id in BOOKS:
        return BOOKS[book_id]
    row = db.get_book(book_id)
    if not row or not os.path.exists(row["path"]):
        abort(404, "书籍不存在")
    book = Book(book_id, row["path"])
    BOOKS[book_id] = book
    return book


def chapter_href(book_id, chapter_id):
    ch = db.get_chapter(book_id, chapter_id)
    if not ch:
        abort(404, "章节不存在")
    return ch["href"]


def run_checks(book, check_names, chapter_href=None):
    """只重跑指定的检查：先清除该检查（及章节范围内）的旧问题，再写入新结果。
    chapter_href 为 None 时，章节级检查遍历 spine 全部章节。"""
    ran = []
    for name in check_names:
        spec = CHECKS[name]
        if spec["scope"] == "chapter" and chapter_href is not None:
            db.delete_issues(book.id, name, chapter_href)
            issues = spec["fn"](book, chapter_href)
        elif spec["scope"] == "chapter":
            db.delete_issues(book.id, name)
            issues = []
            for href in book.spine:
                if href in book.raw:
                    issues.extend(spec["fn"](book, href))
        else:
            db.delete_issues(book.id, name)  # 全书级：整类重算
            issues = spec["fn"](book)
        db.add_issues(book.id, issues)
        ran.append(name)
    return ran


def rescan_chapter(book, href):
    """结构或属性变化后重建该章节点表（派生数据，非检查）。"""
    chs = {c["href"]: c for c in db.list_chapters(book.id)}
    ch = chs.get(href)
    if not ch:
        return
    nodes = book.extract_nodes(href)
    db.replace_nodes(book.id, ch["id"], nodes)


def recheck_table(book, href, table_path):
    """表格编辑后只重检受影响的那张表：先删该表路径下的旧问题，再写入新结果。"""
    db.delete_issues_at_path(book.id, "table_a11y", href, table_path)
    db.add_issues(book.id, check_one_table(book, href, table_path))


def scan_book(book):
    """导入时全量扫描：章节、节点、全部检查。"""
    chapters = []
    for i, href in enumerate(book.spine):
        title = None
        if href in book.raw:
            h = book.soup(href).find(re.compile("^h[12]$"))
            title = h.get_text(strip=True)[:40] if h else None
        chapters.append({"href": href, "title": title or os.path.basename(href),
                         "order_index": i})
    db.replace_chapters(book.id, chapters)
    for ch in db.list_chapters(book.id):
        if ch["href"] in book.raw:
            db.replace_nodes(book.id, ch["id"], book.extract_nodes(ch["href"]))
    run_checks(book, list(CHECKS.keys()))


def register_book(epub_path):
    """把 epub 复制为工作副本并登记入库，返回 book_id。"""
    book_id = db.create_book("（解析中）", None, "")
    work = os.path.join(BOOKS_DIR, str(book_id))
    os.makedirs(work, exist_ok=True)
    work_path = os.path.join(work, "working.epub")
    shutil.copyfile(epub_path, work_path)
    book = Book(book_id, work_path)
    BOOKS[book_id] = book
    with db.connect() as c:
        c.execute("UPDATE books SET title=?, language=?, path=? WHERE id=?",
                  (book.title, book.language, work_path, book_id))
    scan_book(book)
    return book_id


def state(book_id):
    return {
        "book": db.get_book(book_id),
        "chapters": db.list_chapters(book_id),
        "issues": db.list_issues(book_id),
        "landmarks": get_book(book_id).landmarks(),
        "changes": db.list_changes(book_id),
        "check_labels": CHECK_LABELS,
    }


# ---------------- 页面 ----------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------- 书籍管理 ----------------
@app.route("/api/books")
def books():
    return jsonify(db.list_books())


@app.route("/api/import", methods=["POST"])
def import_epub():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".epub"):
        return jsonify({"error": "请上传 .epub 文件"}), 400
    tmp = os.path.join(DATA, "upload_tmp.epub")
    f.save(tmp)
    try:
        book_id = register_book(tmp)
    except Exception as e:
        return jsonify({"error": "EPUB 解析失败: %s" % e}), 400
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return jsonify({"book_id": book_id, "state": state(book_id)})


@app.route("/api/load_sample", methods=["POST"])
def load_sample():
    sample = os.path.join(DATA, "sample.epub")
    create_sample(sample)
    book_id = register_book(sample)
    return jsonify({"book_id": book_id, "state": state(book_id)})


@app.route("/api/books/<int:book_id>")
def book_state(book_id):
    get_book(book_id)
    return jsonify(state(book_id))


def chapter_html_lang(book, href):
    html = book.soup(href).find("html")
    if html is None:
        return None
    return html.get("lang") or html.get("xml:lang")


@app.route("/api/books/<int:book_id>/chapters/<int:chapter_id>/nodes")
def chapter_nodes(book_id, chapter_id):
    book = get_book(book_id)
    href = chapter_href(book_id, chapter_id)
    return jsonify({"nodes": db.list_nodes(book_id, chapter_id),
                    "html_lang": chapter_html_lang(book, href)})


# ---------------- 编辑 ----------------
@app.route("/api/books/<int:book_id>/node/update", methods=["POST"])
def node_update(book_id):
    """修改节点属性：alt / lang / heading_level / epub_type（增删地标）。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    href = chapter_href(book_id, d["chapter_id"])
    path, updates = d["dom_path"], d["updates"]
    try:
        old, new_path = apply_updates(book, href, path, updates)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    affected = sorted({c for a in updates for c in AFFECTED_BY_ATTR.get(a, [])})
    inverse = {"kind": "updates", "chapter": href, "path": new_path,
               "old": old, "affected": affected}
    desc = "、".join("%s: %r→%r" % (a, old[a], updates[a]) for a in updates)
    db.add_change(book_id, "updates", "修改 %s @%s（%s）" % (path, href, desc), inverse)

    if "epub_type" in updates:  # 增删地标 → 同步导航文档的 landmarks 链接
        rebuild_landmarks_nav(book)
    rescan_chapter(book, href)
    run_checks(book, affected, href)
    book.save()
    return jsonify({"ok": True, "checks_ran": affected,
                    "nodes": db.list_nodes(book_id, d["chapter_id"]),
                    "html_lang": chapter_html_lang(book, href),
                    "state": state(book_id)})


@app.route("/api/books/<int:book_id>/node/move", methods=["POST"])
def node_move(book_id):
    """拖拽调整章内节点顺序：移动到 before_path 之前，或 parent_path 末尾。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    href = chapter_href(book_id, d["chapter_id"])
    try:
        info = move_node(book, href, d["dom_path"],
                         before_path=d.get("before_path"),
                         parent_path=d.get("parent_path"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    inverse = {"kind": "move", "chapter": href, "new_path": info["new_path"],
               "old_parent": info["old_parent"], "old_index": info["old_index"],
               "affected": AFFECTED_BY_MOVE}
    db.add_change(book_id, "move", "移动节点 %s → %s（%s）"
                  % (d["dom_path"], info["new_path"], href), inverse)

    rescan_chapter(book, href)
    run_checks(book, AFFECTED_BY_MOVE, href)
    book.save()
    return jsonify({"ok": True, "checks_ran": AFFECTED_BY_MOVE,
                    "nodes": db.list_nodes(book_id, d["chapter_id"]),
                    "state": state(book_id)})


@app.route("/api/books/<int:book_id>/spine/reorder", methods=["POST"])
def spine_reorder(book_id):
    """拖拽调整章节（spine）顺序。order 为章节 id 列表。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    chs = {c["id"]: c["href"] for c in db.list_chapters(book_id)}
    ordered = [chs[i] for i in d["order"] if i in chs]
    if len(ordered) != len(chs):
        return jsonify({"error": "章节列表不完整"}), 400
    old = reorder_spine(book, ordered)

    inverse = {"kind": "spine", "old_order": old, "affected": AFFECTED_BY_SPINE}
    db.add_change(book_id, "spine", "调整章节顺序: %s"
                  % " → ".join(os.path.basename(h) for h in ordered), inverse)

    db.update_chapter_order(book_id, ordered)
    rebuild_landmarks_nav(book)  # 地标链接顺序跟随 spine
    run_checks(book, AFFECTED_BY_SPINE)
    book.save()
    return jsonify({"ok": True, "checks_ran": AFFECTED_BY_SPINE,
                    "state": state(book_id)})


# ---------------- 表格工作区 ----------------
@app.route("/api/books/<int:book_id>/table/<int:chapter_id>")
def table_model(book_id, chapter_id):
    """表格工作区模型：网格、合并关系、表头覆盖、逐格朗读上下文。"""
    book = get_book(book_id)
    href = chapter_href(book_id, chapter_id)
    try:
        return jsonify(table_model_json(book, href, request.args.get("path", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/books/<int:book_id>/table/preview", methods=["POST"])
def table_preview(book_id):
    """把暂存编辑应用到内存副本上计算结果（网格/朗读/冲突）后回滚，不落盘。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    href = chapter_href(book_id, d["chapter_id"])
    try:
        model, conflicts = preview_table_edits(book, href, d["path"],
                                               d.get("edits") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"model": model, "conflicts": conflicts})


@app.route("/api/books/<int:book_id>/table/save", methods=["POST"])
def table_save(book_id):
    """应用一批表格编辑：记入变更（可撤销）、只重检该表、写回工作副本。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    href = chapter_href(book_id, d["chapter_id"])
    path = d["path"]
    table = find_by_path(book.soup(href), path)
    if table is None or table.name != "table":
        return jsonify({"error": "表格不存在: %s" % path}), 404
    old_html = str(table)
    try:
        result = apply_table_edits(book, href, path, d.get("edits") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if result["summary"]:  # 有实际改动才记入变更记录
        inverse = {"kind": "table", "chapter": href, "path": path,
                   "old_html": old_html, "affected": ["table_a11y"]}
        db.add_change(book_id, "table",
                      "表格校修 %s @%s（%s）"
                      % (path, href, "；".join(result["summary"])), inverse)
    rescan_chapter(book, href)
    recheck_table(book, href, path)
    book.save()
    return jsonify({"ok": True, "conflicts": result["conflicts"],
                    "model": table_model_json(book, href, path),
                    "nodes": db.list_nodes(book_id, d["chapter_id"]),
                    "html_lang": chapter_html_lang(book, href),
                    "state": state(book_id)})


# ---------------- 列表工作区 ----------------
LIST_AFFECTED = ["list_a11y", "heading_hierarchy", "duplicate_id"]


@app.route("/api/books/<int:book_id>/list/candidates/<int:chapter_id>")
def list_candidates(book_id, chapter_id):
    """本章列表候选与既有列表（供预览框选高亮、问题面板跳转）。"""
    book = get_book(book_id)
    href = chapter_href(book_id, chapter_id)
    return jsonify(list_candidates_json(book, href))


@app.route("/api/books/<int:book_id>/list/model/<int:chapter_id>")
def list_model_route(book_id, chapter_id):
    book = get_book(book_id)
    href = chapter_href(book_id, chapter_id)
    try:
        return jsonify(list_model_json(book, href, request.args.get("path", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/books/<int:book_id>/list/create", methods=["POST"])
def list_create(book_id):
    """框选连续节点组合为 ul/ol。拒绝跨章/交叉嵌套/可能丢字的提交。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    href = chapter_href(book_id, d["chapter_id"])
    try:
        inverse, new_path, summary = create_list(
            book, href, d.get("paths") or [],
            list_type=d.get("type", "ul"), start=d.get("start"),
            strip_markers=bool(d.get("strip_markers", True)),
            blocks=d.get("blocks") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.add_change(book_id, "list", "组合列表 %s @%s（%s）"
                  % (new_path, href, "；".join(summary)), inverse)
    rescan_chapter(book, href)
    run_checks(book, LIST_AFFECTED, href)
    book.save()
    m = list_model_json(book, href, new_path)
    return jsonify({"ok": True, "model": m, "new_path": new_path,
                    "before_pretty": pretty_snapshot(inverse["before_html"]),
                    "after_pretty": pretty_region(
                        [find_by_path(book.soup(href), p)
                         for p in inverse["after_paths"]]),
                    "nodes": db.list_nodes(book_id, d["chapter_id"]),
                    "html_lang": chapter_html_lang(book, href),
                    "state": state(book_id)})


@app.route("/api/books/<int:book_id>/list/op", methods=["POST"])
def list_op(book_id):
    """单个列表操作：缩进/提升、纳邻/缩界、拆开/接续、起始序号/value、归整/删空。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    href = chapter_href(book_id, d["chapter_id"])
    try:
        inverse, new_path, summary = apply_op(
            book, href, d["path"], d["op"], d.get("params") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.add_change(book_id, "list",
                  "列表%s %s @%s（%s）"
                  % (d["op"], d["path"], href, "；".join(summary)), inverse)
    rescan_chapter(book, href)
    run_checks(book, LIST_AFFECTED, href)
    book.save()
    payload = {"ok": True, "new_path": new_path,
               "before_pretty": pretty_snapshot(inverse["before_html"]),
               "after_pretty": pretty_region(
                   [find_by_path(book.soup(href), p)
                    for p in inverse["after_paths"]]),
               "nodes": db.list_nodes(book_id, d["chapter_id"]),
               "html_lang": chapter_html_lang(book, href),
               "state": state(book_id)}
    if new_path:
        payload["model"] = list_model_json(book, href, new_path)
    return jsonify(payload)


def pretty_snapshot(before_html):
    from bs4 import BeautifulSoup as BS
    return pretty_region([next(c for c in BS(h, "xml").contents if c.name)
                          for h in before_html])


@app.route("/api/books/<int:book_id>/undo", methods=["POST"])
def undo(book_id):
    change = db.last_active_change(book_id)
    if not change:
        return jsonify({"error": "没有可撤销的操作"}), 400
    book = get_book(book_id)
    inv = json.loads(change["inverse"])
    kind = inv["kind"]
    if kind == "updates":
        apply_updates(book, inv["chapter"], inv["path"], inv["old"])
        if "epub_type" in inv["old"]:  # 撤销地标增删 → 同步导航文档
            rebuild_landmarks_nav(book)
        rescan_chapter(book, inv["chapter"])
        run_checks(book, inv["affected"], inv["chapter"])
    elif kind == "move":
        undo_move(book, inv["chapter"], inv["new_path"],
                  inv["old_parent"], inv["old_index"])
        rescan_chapter(book, inv["chapter"])
        run_checks(book, inv["affected"], inv["chapter"])
    elif kind == "spine":
        reorder_spine(book, inv["old_order"])
        db.update_chapter_order(book_id, inv["old_order"])
        rebuild_landmarks_nav(book)
        run_checks(book, inv["affected"])
    elif kind == "table":
        restore_table(book, inv["chapter"], inv["path"], inv["old_html"])
        rescan_chapter(book, inv["chapter"])
        recheck_table(book, inv["chapter"], inv["path"])
    elif kind == "list":
        restore_region(book, inv["chapter"], inv["parent_path"], inv["index0"],
                       inv["after_paths"], inv["before_html"])
        rescan_chapter(book, inv["chapter"])
        run_checks(book, inv["affected"], inv["chapter"])
    db.mark_change_undone(change["id"])
    book.save()
    return jsonify({"ok": True, "undone": change["summary"],
                    "checks_ran": inv["affected"], "state": state(book_id)})


# ---------------- 预览与资源 ----------------
PREVIEW_SCRIPT = """
<style>
.a11y-hl{outline:3px solid #e0245e !important;outline-offset:2px;}
.a11y-cand{outline:2px dashed #0969da !important;outline-offset:1px;background:rgba(9,105,218,.06);}
.a11y-list{outline:2px solid #1a7f37 !important;outline-offset:1px;}
.a11y-pick{outline:3px solid #bf3989 !important;outline-offset:1px;background:rgba(191,57,137,.10) !important;}
body.a11y-selecting, body.a11y-selecting *{cursor:crosshair !important;}
</style>
<script>
function pathOf(el){
  var parts=[];
  while(el && el.nodeType===1 && el.tagName.toLowerCase()!=='html'){
    var tag=el.tagName.toLowerCase(), idx=1, sib=el.previousElementSibling;
    while(sib){ if(sib.tagName.toLowerCase()===tag) idx++; sib=sib.previousElementSibling; }
    parts.unshift(tag+'['+idx+']'); el=el.parentElement;
  }
  return 'html[1]/'+parts.join('/');
}
function findByPath(path){
  var parts=path.split('/'), cur=document.documentElement;
  for(var i=1;i<parts.length;i++){
    var m=parts[i].match(/(.+)\\[(\\d+)\\]/); if(!m) return null;
    var tag=m[1], idx=+m[2], kids=cur.children, found=null, c=0;
    for(var k=0;k<kids.length;k++)
      if(kids[k].tagName.toLowerCase()===tag && ++c===idx){found=kids[k];break;}
    if(!found) return null; cur=found;
  }
  return cur;
}
var LIST_TAGS={ul:1,ol:1}, ITEM_TAGS={p:1,li:1,div:1};
var last=null, selecting=false, pickStart=null, pickEnd=null;
function clearMarks(cls){ document.querySelectorAll('.'+cls).forEach(function(el){el.classList.remove(cls);}); }
function setMarks(paths, cls, on){
  paths.forEach(function(p){ var el=findByPath(p); if(el) el.classList.toggle(cls, !!on); });
}
function orderedBlockSiblings(){
  // 取 body 内所有可列表化块的路径（扁平），供父页面框选时取连续区间
  var out=[];
  document.querySelectorAll('p,li,div,ul,ol,h1,h2,h3,h4,h5,h6,img,figure,aside').forEach(function(el){
    if(el.closest('nav')) return;
    out.push(pathOf(el));
  });
  return out;
}
function topmostPick(el){
  // 框选命中时回溯到一个可作为列表项的顶层块（p/孤立li/div），避免选到行内元素
  var node=el;
  while(node && node.nodeType===1 && node.tagName.toLowerCase()!=='body'){
    var t=node.tagName.toLowerCase();
    if(ITEM_TAGS[t] && !(node.parentElement && LIST_TAGS[node.parentElement.tagName.toLowerCase()]))
      return node;
    if(LIST_TAGS[t]) return node;
    node=node.parentElement;
  }
  return el;
}
document.addEventListener('click', function(e){
  e.preventDefault(); e.stopPropagation();
  if(selecting){
    var el=topmostPick(e.target);
    if(!pickStart){ pickStart=el; el.classList.add('a11y-pick'); }
    else { pickEnd=el; finishPick(); }
    return;
  }
  parent.postMessage({type:'locate', path:pathOf(e.target)}, '*');
}, true);
function finishPick(){
  var a=pickStart, b=pickEnd;
  // 必须同父；收集二者之间的兄弟区间
  var paths=null;
  if(a && b && a.parentElement===b.parentElement){
    var kids=Array.prototype.filter.call(a.parentElement.children,function(n){return n.nodeType===1;});
    var ia=kids.indexOf(a), ib=kids.indexOf(b); if(ia>ib){var t=ia;ia=ib;ib=t;}
    paths=kids.slice(ia,ib+1).map(pathOf);
  }
  parent.postMessage({type:'listPick', sameParent:a&&b?a.parentElement===b.parentElement:false,
                      anchor: a?pathOf(a):null, paths:paths}, '*');
  clearMarks('a11y-pick'); pickStart=pickEnd=null;
}
window.addEventListener('message', function(e){
  var d=e.data; if(!d) return;
  if(d.type==='highlight'){
    var el=findByPath(d.path);
    if(last) last.classList.remove('a11y-hl');
    if(el){ el.classList.add('a11y-hl'); el.scrollIntoView({block:'center'}); last=el; }
  } else if(d.type==='listSelectMode'){
    selecting=!!d.on; pickStart=pickEnd=null; clearMarks('a11y-pick');
    document.body.classList.toggle('a11y-selecting', selecting);
  } else if(d.type==='listMarks'){
    clearMarks('a11y-cand'); clearMarks('a11y-list');
    (d.candidates||[]).forEach(function(paths,ci){
      paths.forEach(function(p){ var el=findByPath(p); if(el){ el.classList.add('a11y-cand'); el.setAttribute('title','疑似列表项（点击问题面板中的该项可一键组合）'); } });
    });
    (d.lists||[]).forEach(function(p){ var el=findByPath(p); if(el){ el.classList.add('a11y-list'); el.setAttribute('title','列表（可在列表工作区校修）'); } });
  } else if(d.type==='clearMarks'){
    clearMarks('a11y-cand'); clearMarks('a11y-list'); clearMarks('a11y-pick');
  }
});
</script>
"""


@app.route("/api/books/<int:book_id>/preview/<path:href>")
def preview(book_id, href):
    book = get_book(book_id)
    if href not in book.raw:
        abort(404)
    out = str(book.soup(href))
    cdir = posixpath.dirname(href)
    base = '<base href="/api/books/%d/res/%s/"/>' % (book_id, cdir)
    if "<head>" in out:
        out = out.replace("<head>", "<head>" + base, 1)
    else:
        out = re.sub(r"(<head[^>]*>)", r"\1" + base, out, count=1)
    out = out.replace("</body>", PREVIEW_SCRIPT + "</body>")
    return out, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/books/<int:book_id>/res/<path:sub>")
def resource(book_id, sub):
    book = get_book(book_id)
    full = posixpath.normpath(sub)
    if full not in book.raw:
        abort(404)
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml", ".css": "text/css",
            ".xhtml": "application/xhtml+xml"}.get(
        os.path.splitext(full)[1].lower(), "application/octet-stream")
    return book.raw[full], 200, {"Content-Type": mime}


# ---------------- 导出 ----------------
@app.route("/api/books/<int:book_id>/export/epub")
def export_epub(book_id):
    book = get_book(book_id)
    book.save()
    return send_file(book.path, as_attachment=True,
                     download_name="fixed_book_%d.epub" % book_id)


@app.route("/api/books/<int:book_id>/export/changes")
def export_changes(book_id):
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "类型", "说明", "已撤销"])
    kind_label = {"updates": "属性修改", "move": "节点移动", "spine": "章节排序",
                  "table": "表格校修", "list": "列表校修"}
    for ch in reversed(db.list_changes(book_id, limit=10000)):
        w.writerow([ch["ts"], kind_label.get(ch["kind"], ch["kind"]),
                    ch["summary"], "是" if ch["undone"] else "否"])
    data = buf.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name="changes_book_%d.csv" % book_id,
                     mimetype="text/csv")


@app.route("/api/books/<int:book_id>/export/report")
def export_report(book_id):
    row = db.get_book(book_id)
    issues = db.list_issues(book_id)
    groups = {}
    for it in issues:
        groups.setdefault(it["check_name"], []).append(it)
    parts = ["""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>未解决问题报告</title><style>
body{font-family:sans-serif;max-width:900px;margin:2em auto;}
table{border-collapse:collapse;width:100%%;} td,th{border:1px solid #ccc;padding:4px 8px;text-align:left;}
.error{color:#c00;} .warning{color:#a60;} h2{margin-top:2em;}
</style></head><body><h1>未解决问题报告：%s</h1><p>共 %d 项未解决问题。</p>"""
             % (row["title"], len(issues))]
    for check, items in groups.items():
        parts.append("<h2>%s（%d）</h2><table><tr><th>严重度</th><th>章节</th><th>节点</th><th>说明</th></tr>"
                     % (CHECK_LABELS.get(check, check), len(items)))
        for it in items:
            parts.append("<tr><td class='%s'>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td></tr>"
                         % (it["severity"], it["severity"],
                            it["chapter_href"] or "-", it["node_path"] or "-",
                            it["message"]))
        parts.append("</table>")
    parts.append("</body></html>")
    data = "".join(parts).encode("utf-8")
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name="report_book_%d.html" % book_id,
                     mimetype="text/html")


if __name__ == "__main__":
    db.init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)
