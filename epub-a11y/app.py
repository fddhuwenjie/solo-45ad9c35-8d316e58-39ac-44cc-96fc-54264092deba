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
from epublib import Book, CHECK_LABELS, CHECKS, AFFECTED_BY_ATTR, \
    AFFECTED_BY_MOVE, AFFECTED_BY_SPINE, apply_updates, move_node, undo_move, \
    reorder_spine, find_by_path, dom_path
from sample_book import create_sample

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


@app.route("/api/books/<int:book_id>/chapters/<int:chapter_id>/nodes")
def chapter_nodes(book_id, chapter_id):
    return jsonify(db.list_nodes(book_id, chapter_id))


# ---------------- 编辑 ----------------
@app.route("/api/books/<int:book_id>/node/update", methods=["POST"])
def node_update(book_id):
    """修改节点属性：alt / lang / heading_level / epub_type（增删地标）。"""
    d = request.get_json(force=True)
    book = get_book(book_id)
    href = chapter_href(book_id, d["chapter_id"])
    path, updates = d["dom_path"], d["updates"]
    try:
        old = apply_updates(book, href, path, updates)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    affected = sorted({c for a in updates for c in AFFECTED_BY_ATTR.get(a, [])})
    inverse = {"kind": "updates", "chapter": href, "path": path,
               "old": old, "affected": affected}
    desc = "、".join("%s: %r→%r" % (a, old[a], updates[a]) for a in updates)
    db.add_change(book_id, "updates", "修改 %s @%s（%s）" % (path, href, desc), inverse)

    rescan_chapter(book, href)
    run_checks(book, affected, href)
    book.save()
    return jsonify({"ok": True, "checks_ran": affected,
                    "nodes": db.list_nodes(book_id, d["chapter_id"]),
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
    run_checks(book, AFFECTED_BY_SPINE)
    book.save()
    return jsonify({"ok": True, "checks_ran": AFFECTED_BY_SPINE,
                    "state": state(book_id)})


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
        run_checks(book, inv["affected"])
    db.mark_change_undone(change["id"])
    book.save()
    return jsonify({"ok": True, "undone": change["summary"],
                    "checks_ran": inv["affected"], "state": state(book_id)})


# ---------------- 预览与资源 ----------------
PREVIEW_SCRIPT = """
<style>.a11y-hl{outline:3px solid #e0245e !important;outline-offset:2px;}</style>
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
var last=null;
document.addEventListener('click', function(e){
  e.preventDefault(); e.stopPropagation();
  parent.postMessage({type:'locate', path:pathOf(e.target)}, '*');
}, true);
window.addEventListener('message', function(e){
  if(e.data && e.data.type==='highlight'){
    var el=findByPath(e.data.path);
    if(last) last.classList.remove('a11y-hl');
    if(el){ el.classList.add('a11y-hl'); el.scrollIntoView({block:'center'}); last=el; }
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
    kind_label = {"updates": "属性修改", "move": "节点移动", "spine": "章节排序"}
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
