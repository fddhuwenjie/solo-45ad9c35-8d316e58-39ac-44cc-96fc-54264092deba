"""EPUB 解析、DOM 路径定位、无障碍检查与编辑操作。

内存中以 BeautifulSoup(xml 模式) 持有各 XHTML/OPF 文档树作为编辑真源，
每次编辑后调用 save() 将整包写回工作副本 working.epub，保证可随时重新打开。
"""
import os
import posixpath
import re
import zipfile
from urllib.parse import unquote

from bs4 import BeautifulSoup

# 阅读顺序中关注的块级节点
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "img", "figure",
              "aside", "table", "ul", "ol", "blockquote", "pre"}
# 仅当带有 epub:type / id / class 时才视为节点的容器
COND_TAGS = {"section", "nav", "div"}
HEADING_RE = re.compile(r"^h[1-6]$")

CHECK_LABELS = {
    "toc": "目录漏项/次序冲突",
    "heading_hierarchy": "标题层级跳跃",
    "img_alt": "图片缺少替代文本",
    "lang": "语言标记缺失",
    "duplicate_id": "重复 ID",
    "broken_anchor": "失效锚点",
    "footnote_backlink": "脚注回链缺失",
    "table_a11y": "表格表头与关联",
    "list_a11y": "列表语义与结构",
}


# ---------------- DOM 路径（与前端 JS 算法保持一致） ----------------
def dom_path(el):
    """形如 html[1]/body[1]/section[2]/p[3]，下标在同名兄弟中从 1 开始。"""
    parts = []
    while el is not None and getattr(el, "name", None) and el.name != "[document]":
        count = 0
        for sib in el.find_previous_siblings():
            if getattr(sib, "name", None) == el.name:
                count += 1
        parts.append("%s[%d]" % (el.name, count + 1))
        el = el.parent
    return "/".join(reversed(parts))


def find_by_path(soup, path):
    cur = soup
    for part in path.split("/"):
        m = re.match(r"^(.+)\[(\d+)\]$", part)
        if not m:
            return None
        name, idx = m.group(1), int(m.group(2))
        children = [c for c in cur.find_all(name, recursive=False)]
        if len(children) < idx:
            return None
        cur = children[idx - 1]
    return cur


def _issue(chapter, path, check, severity, message):
    return {"chapter": chapter, "path": path, "check": check,
            "severity": severity, "message": message}


# ---------------- Book ----------------
class Book:
    def __init__(self, book_id, epub_path):
        self.id = book_id
        self.path = epub_path
        with zipfile.ZipFile(epub_path) as zf:
            self.raw = {n: zf.read(n) for n in zf.namelist()}
        self.soups = {}   # path -> BeautifulSoup（懒加载，编辑真源）
        self.dirty = set()
        self._parse_opf()

    # ---- 文件访问 ----
    def soup(self, path):
        if path not in self.soups:
            self.soups[path] = BeautifulSoup(self.raw[path], "xml")
        return self.soups[path]

    def mark_dirty(self, path):
        self.dirty.add(path)

    def resolve(self, from_path, href):
        return posixpath.normpath(
            posixpath.join(posixpath.dirname(from_path), unquote(href)))

    # ---- OPF / 目录解析 ----
    def _parse_opf(self):
        container = BeautifulSoup(self.raw["META-INF/container.xml"], "xml")
        self.opf_path = container.find("rootfile")["full-path"]
        self.opf_dir = posixpath.dirname(self.opf_path)
        self.opf = self.soup(self.opf_path)

        t = self.opf.find("dc:title")
        self.title = t.get_text(strip=True) if t else "未命名"
        lang = self.opf.find("dc:language")
        self.language = lang.get_text(strip=True) if lang else None

        self.manifest = {}
        for it in self.opf.find_all("item"):
            full = posixpath.normpath(
                posixpath.join(self.opf_dir, unquote(it["href"])))
            self.manifest[it["id"]] = {
                "href": full,
                "media_type": it.get("media-type", ""),
                "properties": (it.get("properties") or "").split(),
            }
        spine_el = self.opf.find("spine")
        self.spine = [self.manifest[ir["idref"]]["href"]
                      for ir in spine_el.find_all("itemref")
                      if ir.get("idref") in self.manifest]

        self.nav_path = None
        self.ncx_path = None
        for m in self.manifest.values():
            if "nav" in m["properties"]:
                self.nav_path = m["href"]
            if m["media_type"] == "application/x-dtbncx+xml":
                self.ncx_path = m["href"]

    def toc(self):
        """目录条目 [{text, href(已解析, 含#fragment)}]，EPUB3 nav 优先，回退 NCX。"""
        entries = []
        if self.nav_path and self.nav_path in self.raw:
            nav = self.soup(self.nav_path)
            toc_nav = nav.find("nav", attrs={"epub:type": "toc"}) or nav.find("nav")
            if toc_nav:
                for a in toc_nav.find_all("a", href=True):
                    entries.append({"text": a.get_text(strip=True),
                                    "href": self._resolve_href(self.nav_path, a["href"])})
        elif self.ncx_path and self.ncx_path in self.raw:
            ncx = self.soup(self.ncx_path)
            for np_ in ncx.find_all("navPoint"):
                label = np_.find("navLabel")
                content = np_.find("content")
                if content and content.get("src"):
                    entries.append({
                        "text": label.get_text(strip=True) if label else "",
                        "href": self._resolve_href(self.ncx_path, content["src"])})
        return entries

    def _resolve_href(self, from_path, href):
        if "#" in href:
            f, frag = href.split("#", 1)
            return self.resolve(from_path, f) + "#" + frag
        return self.resolve(from_path, href)

    # ---- 节点提取 ----
    def extract_nodes(self, href):
        soup = self.soup(href)
        body = soup.find("body")
        nodes = []
        if not body:
            return nodes

        def walk(el, parent_path, inside_nav):
            for child in el.find_all(recursive=False):
                if not getattr(child, "name", None):
                    continue
                if child.name == "nav":
                    continue  # 导航结构不参与线性朗读
                path = dom_path(child)
                is_node = child.name in BLOCK_TAGS or (
                    child.name in COND_TAGS and
                    (child.get("epub:type") or child.get("id") or child.get("class")))
                if is_node:
                    nodes.append(self._node_info(child, path, parent_path))
                    walk(child, path, inside_nav)
                else:
                    walk(child, parent_path, inside_nav)
        walk(body, None, False)
        return nodes

    @staticmethod
    def _node_info(el, path, parent_path):
        m = HEADING_RE.match(el.name)
        return {
            "dom_path": path,
            "parent_path": parent_path,
            "tag": el.name,
            "text": "" if el.name == "img" else el.get_text(" ", strip=True)[:80],
            "level": int(el.name[1]) if m else None,
            "lang": el.get("lang") or el.get("xml:lang"),
            "alt": el.get("alt") if el.name == "img" else None,
            "epub_type": el.get("epub:type"),
            "img_src": el.get("src") if el.name == "img" else None,
            "el_id": el.get("id"),
        }

    def collect_landmarks(self):
        """spine 文档中的地标元素（导航文档自身的镜像条目不计入）。"""
        out = []
        for href in self.spine:
            if href not in self.raw:
                continue
            for el in self.soup(href).find_all(attrs={"epub:type": True}):
                t = (el.get("epub:type") or "").strip()
                if not t or t in ("noteref", "footnote", "endnote", "toc", "landmarks"):
                    continue
                out.append({"chapter": href, "path": dom_path(el), "tag": el.name,
                            "epub_type": t, "el_id": el.get("id"),
                            "text": el.get_text(" ", strip=True)[:40]})
        return out

    def landmarks(self):
        return [{k: v for k, v in lm.items() if k != "el_id"}
                for lm in self.collect_landmarks()]

    # ---- 序列化 / 保存 ----
    def save(self):
        tmp = self.path + ".tmp"
        with zipfile.ZipFile(tmp, "w") as z:
            if "mimetype" in self.raw:  # EPUB 规范：mimetype 须为首项且不压缩
                z.writestr(zipfile.ZipInfo("mimetype"), self.raw["mimetype"],
                           compress_type=zipfile.ZIP_STORED)
            for name, data in self.raw.items():
                if name == "mimetype":
                    continue
                if name in self.dirty and name in self.soups:
                    data = serialize(self.soups[name])
                z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(tmp, self.path)
        self.dirty.clear()
        with zipfile.ZipFile(self.path) as zf:
            self.raw = {n: zf.read(n) for n in zf.namelist()}


def serialize(soup):
    out = str(soup)
    if not out.lstrip().startswith("<?xml"):
        out = '<?xml version="1.0" encoding="utf-8"?>\n' + out
    return out.encode("utf-8")


# ---------------- 检查 ----------------
def ch_img_alt(book, href):
    out = []
    for img in book.soup(href).find_all("img"):
        if img.get("alt") is None:
            out.append(_issue(href, dom_path(img), "img_alt", "error",
                              "图片缺少 alt 替代文本: %s" % img.get("src")))
    return out


def ch_heading_hierarchy(book, href):
    out, prev = [], 0
    for h in book.soup(href).find_all(HEADING_RE):
        lvl = int(h.name[1])
        if prev and lvl > prev + 1:
            out.append(_issue(href, dom_path(h), "heading_hierarchy", "warning",
                              "标题层级从 h%d 跳到 h%d:「%s」"
                              % (prev, lvl, h.get_text(strip=True)[:30])))
        prev = lvl
    return out


def ch_lang(book, href):
    out = []
    html = book.soup(href).find("html")
    if html is not None and not (html.get("lang") or html.get("xml:lang")):
        out.append(_issue(href, "html[1]", "lang", "error",
                          "文档 <html> 缺少 lang/xml:lang 语言标记"))
    return out


def ch_duplicate_id(book, href):
    out, seen = [], {}
    for el in book.soup(href).find_all(attrs={"id": True}):
        i = el["id"]
        if i in seen:
            out.append(_issue(href, dom_path(el), "duplicate_id", "error",
                              "重复的 id「%s」（首次出现于 %s）" % (i, seen[i])))
        else:
            seen[i] = dom_path(el)
    return out


def ch_broken_anchor(book, href):
    out = []
    for a in book.soup(href).find_all("a", href=True):
        h = a["href"]
        if h.startswith(("http://", "https://", "mailto:")):
            continue
        file, frag = (h.split("#", 1) + [""])[:2] if "#" in h else (h, "")
        target = book.resolve(href, file) if file else href
        if file and target not in book.raw:
            out.append(_issue(href, dom_path(a), "broken_anchor", "error",
                              "链接目标文件不存在: %s" % h))
            continue
        if frag and target in book.raw and target.endswith((".xhtml", ".html", ".htm")):
            if not book.soup(target).find(id=frag):
                out.append(_issue(href, dom_path(a), "broken_anchor", "error",
                                  "失效锚点 #%s（链接 %s）" % (frag, h)))
    return out


def ch_footnote_backlink(book):
    """noteref 指向的脚注必须含有回到该 noteref 的回链（支持跨章）。全书级检查。"""
    out = []
    for href in book.spine:
        if href not in book.raw:
            continue
        for a in book.soup(href).find_all("a", attrs={"epub:type": "noteref"}):
            refid = a.get("id")
            if not refid or not a.get("href"):
                continue
            h = a["href"]
            file, frag = (h.split("#", 1) + [""])[:2] if "#" in h else (h, "")
            target = book.resolve(href, file) if file else href
            if target not in book.raw:
                continue
            note = book.soup(target).find(id=frag)
            if note is None:
                continue  # 由 broken_anchor 报告
            container = note if note.name in ("aside", "li", "section", "div") \
                else (note.find_parent(["aside", "li", "section", "div"]) or note)
            ok = False
            for link in container.find_all("a", href=True):
                lh = link["href"]
                if "#" not in lh:
                    continue
                lf, lfrag = lh.split("#", 1)
                lt = book.resolve(target, lf) if lf else target
                if lt == href and lfrag == refid:
                    ok = True
                    break
            if not ok:
                out.append(_issue(target, dom_path(note), "footnote_backlink",
                                  "warning",
                                  "脚注 #%s 缺少返回正文（%s#%s）的回链"
                                  % (frag, os.path.basename(href), refid)))
    return out


def ch_toc(book):
    """目录与 spine 的漏项 / 次序冲突。全书级检查。"""
    out = []
    entries = book.toc()
    entry_paths = [e["href"].split("#")[0] for e in entries]

    seq = [book.spine.index(p) for p in entry_paths if p in book.spine]
    if seq != sorted(seq):
        out.append(_issue(None, None, "toc", "error",
                          "目录条目顺序与 spine 阅读顺序冲突（目录: %s）"
                          % " → ".join(e["text"] for e in entries)))

    for sp in book.spine:
        if sp not in entry_paths:
            out.append(_issue(sp, None, "toc", "warning",
                              "章节未在目录中: %s" % os.path.basename(sp)))

    referenced = {e["href"] for e in entries}
    referenced_docs = set(entry_paths)
    for sp in book.spine:
        if sp not in book.raw:
            continue
        seen_first_h1 = False
        for h in book.soup(sp).find_all(re.compile("^h[12]$")):
            if h.find_parent("nav"):
                continue
            # 整章链接（无 #fragment）只覆盖章内第一个 h1（文档顶端主标题）；
            # 同章后续的 h1 与所有 h2 都必须有显式 #id 目录条目
            is_first_h1 = h.name == "h1" and not seen_first_h1
            if h.name == "h1":
                seen_first_h1 = True
            hid = h.get("id")
            if not hid:
                continue
            explicit = "%s#%s" % (sp, hid) in referenced
            covered = explicit or (is_first_h1 and sp in referenced_docs)
            if not covered:
                out.append(_issue(sp, dom_path(h), "toc", "warning",
                                  "标题「%s」未收录进目录"
                                  % h.get_text(strip=True)[:30]))
    return out


# ---------------- 复杂表格无障碍 ----------------
def _table_rows(table):
    """本表的 tr（排除嵌套表格中的行）。"""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def table_grid(table):
    """把 table 展开为网格模型：rowspan/colspan 向下/向右占位。
    返回 {"rows","cols","cells"}；cell: {el,row,col,rowspan,colspan,tag,id,headers,scope,text}"""
    cells = []
    occ = {}   # (r,c) -> cell 下标
    nrows = ncols = 0
    for r, tr in enumerate(_table_rows(table)):
        c = 0
        for el in tr.find_all(["th", "td"], recursive=False):
            while (r, c) in occ:
                c += 1
            try:
                rs = max(1, int(el.get("rowspan") or 1))
            except ValueError:
                rs = 1
            try:
                cs = max(1, int(el.get("colspan") or 1))
            except ValueError:
                cs = 1
            cells.append({
                "el": el, "row": r, "col": c, "rowspan": rs, "colspan": cs,
                "tag": el.name, "id": el.get("id"),
                "headers": (el.get("headers") or "").split(),
                "scope": (el.get("scope") or "").strip().lower() or None,
                "text": el.get_text(" ", strip=True)[:40],
            })
            idx = len(cells) - 1
            for dr in range(rs):
                for dc in range(cs):
                    occ[(r + dr, c + dc)] = idx
            c += cs
            ncols = max(ncols, c)
            nrows = max(nrows, r + rs)
    return {"rows": nrows, "cols": ncols, "cells": cells}


def _resolved_scope(cell):
    """显式 scope 优先；缺省时按位置推断：首行 → 列方向，其余 → 行方向。"""
    if cell["scope"] in ("row", "col", "rowgroup", "colgroup"):
        return cell["scope"]
    return "col" if cell["row"] == 0 else "row"


def _overlap(a_start, a_span, b_start, b_span):
    return a_start < b_start + b_span and b_start < a_start + a_span


def _covers(th, cell):
    """th 是否（按 scope/位置规则）覆盖 cell。"""
    sc = _resolved_scope(th)
    if sc in ("col", "colgroup"):
        return cell["row"] > th["row"] and _overlap(
            cell["col"], cell["colspan"], th["col"], th["colspan"])
    return cell["col"] > th["col"] and _overlap(
        cell["row"], cell["rowspan"], th["row"], th["rowspan"])


def _associations(model):
    """表头关联：covered_by[数据格]=[表头下标]，covers[表头]=[数据格下标]。
    带 headers 属性的格按显式引用（保持书写顺序）；其余按 scope/位置推断。"""
    cells = model["cells"]
    by_id = {c["id"]: i for i, c in enumerate(cells) if c["id"]}
    th_idx = [i for i, c in enumerate(cells) if c["tag"] == "th"]
    covered_by = {i: [] for i in range(len(cells))}
    covers = {i: [] for i in range(len(cells))}
    for i, c in enumerate(cells):
        if c["tag"] != "td":
            continue
        if c["headers"]:
            # 显式 headers 只接受 <th> 作为表头目标；指向 td 的引用无效
            covered_by[i] = [by_id[h] for h in c["headers"]
                             if h in by_id and cells[by_id[h]]["tag"] == "th"]
        else:
            covered_by[i] = sorted(
                (j for j in th_idx if _covers(cells[j], c)),
                key=lambda j: (cells[j]["row"], cells[j]["col"]))
    for i, lst in covered_by.items():
        for j in lst:
            covers[j].append(i)
    return covered_by, covers


def _reading(model, covered_by, i):
    """模拟屏幕阅读器朗读该格时的上下文（表头序列 + 数据）。"""
    cells = model["cells"]
    c = cells[i]
    if c["tag"] == "th":
        kind = "列" if _resolved_scope(c) in ("col", "colgroup") else "行"
        return "%s表头：%s" % (kind, c["text"] or "（空）")
    heads = [cells[j]["text"] for j in covered_by.get(i, [])]
    if heads:
        return "%s：%s" % ("，".join(heads), c["text"] or "（空）")
    return "（无关联表头）%s" % (c["text"] or "（空）")


def _header_cycles(edges):
    """headers 引用图中的环（Tarjan 强连通分量：大小>1 或自环）。edges: 下标 -> [下标]。"""
    index, low, on_stack, stack, out = {}, {}, set(), [], []
    counter = [0]

    def connect(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, []):
            if w not in index:
                connect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1 or scc[0] in edges.get(scc[0], []):
                out.append(sorted(scc))

    for v in list(edges):
        if v not in index:
            connect(v)
    return out


def check_table_el(book, href, table, table_path):
    """单张表格检查：caption 缺失、视觉表头仍为 td、scope 与合并结构冲突、
    headers 引用失效/循环、数据格无法关联表头。问题定位在表格路径上。"""
    soup = book.soup(href)
    out = []
    model = table_grid(table)
    cells = model["cells"]
    if not cells:
        return out

    def coord(c):
        return "第%d行第%d列" % (c["row"] + 1, c["col"] + 1)

    cap = table.find("caption", recursive=False)
    if cap is None or not cap.get_text(strip=True):
        out.append(_issue(href, table_path, "table_a11y", "warning",
                          "表格缺少 <caption> 标题，屏幕阅读器只能读出一串失去上下文的数据"))

    ths = [c for c in cells if c["tag"] == "th"]
    visual = set()
    if not ths:
        first = [c for c in cells if c["row"] == 0]
        if first and all(c["tag"] == "td" for c in first) \
                and all(c["el"].find(["strong", "b"]) for c in first):
            visual = {(c["row"], c["col"]) for c in first}
            out.append(_issue(href, table_path, "table_a11y", "warning",
                              "首行是视觉表头（加粗）却仍使用 <td>，应改为 <th> 并设置 scope"))

    for c in cells:
        if c["tag"] == "td" and c["scope"]:
            out.append(_issue(href, table_path, "table_a11y", "warning",
                              "%s 是 <td> 却设置了 scope=\"%s\"：表头单元格应使用 <th>"
                              % (coord(c), c["scope"])))

    for c in ths:
        if c["scope"] == "col" and c["colspan"] > 1:
            out.append(_issue(href, table_path, "table_a11y", "warning",
                              "%s：scope=\"col\" 与 colspan=%d 冲突，跨列合并表头应使用 scope=\"colgroup\""
                              % (coord(c), c["colspan"])))
        if c["scope"] == "row" and c["rowspan"] > 1:
            out.append(_issue(href, table_path, "table_a11y", "warning",
                              "%s：scope=\"row\" 与 rowspan=%d 冲突，跨行合并表头应使用 scope=\"rowgroup\""
                              % (coord(c), c["rowspan"])))

    by_id = {c["id"]: i for i, c in enumerate(cells) if c["id"]}
    for c in cells:
        for hid in c["headers"]:
            if hid in by_id:
                continue
            if soup.find(id=hid) is not None:
                out.append(_issue(href, table_path, "table_a11y", "warning",
                                  "%s 的 headers 引用了表外元素 id「%s」，关联在朗读时不生效"
                                  % (coord(c), hid)))
            else:
                out.append(_issue(href, table_path, "table_a11y", "error",
                                  "%s 的 headers 引用了不存在的 id「%s」" % (coord(c), hid)))

    edges = {}
    for i, c in enumerate(cells):
        refs = [by_id[h] for h in c["headers"] if h in by_id]
        if refs:
            edges[i] = refs
    for scc in _header_cycles(edges):
        out.append(_issue(href, table_path, "table_a11y", "error",
                          "headers 引用循环：%s"
                          % " ↔ ".join(coord(cells[i]) for i in scc)))

    # headers 引用了表内 td：显式关联无效，该数据格实际没有任何表头
    for i, c in enumerate(cells):
        if c["tag"] != "td" or not c["headers"]:
            continue
        resolved = [by_id[h] for h in c["headers"] if h in by_id]
        if resolved and not any(cells[j]["tag"] == "th" for j in resolved):
            out.append(_issue(href, table_path, "table_a11y", "warning",
                              "%s 的数据格「%s」的 headers 未关联到有效表头（指向的目标须为 <th>）"
                              % (coord(c), c["text"][:20] or "（空）")))

    covered_by, _ = _associations(model)
    for i, c in enumerate(cells):
        if c["tag"] != "td" or c["headers"]:
            continue
        if (c["row"], c["col"]) in visual:
            continue
        if not covered_by.get(i):
            out.append(_issue(href, table_path, "table_a11y", "warning",
                              "%s 的数据格「%s」无法关联任何表头"
                              % (coord(c), c["text"][:20] or "（空）")))
    return out


def ch_table_a11y(book, href):
    out = []
    for table in book.soup(href).find_all("table"):
        if table.find_parent("nav") is not None:
            continue  # 导航结构中的表格不参与线性朗读
        if (table.get("role") or "").strip().lower() == "presentation":
            continue  # 排版用表格无需表头语义
        out.extend(check_table_el(book, href, table, dom_path(table)))
    return out


def check_one_table(book, href, table_path):
    """只重检一张表（表格编辑后调用）。"""
    table = find_by_path(book.soup(href), table_path)
    if table is None or table.name != "table":
        return []
    return check_table_el(book, href, table, table_path)


def table_model_json(book, href, table_path):
    """表格工作区模型：网格、合并关系、表头覆盖范围、逐格朗读上下文与实时问题。"""
    soup = book.soup(href)
    table = find_by_path(soup, table_path)
    if table is None or table.name != "table":
        raise ValueError("表格不存在: %s" % table_path)
    model = table_grid(table)
    covered_by, covers = _associations(model)
    cells = []
    for i, c in enumerate(model["cells"]):
        cells.append({
            "row": c["row"], "col": c["col"],
            "rowspan": c["rowspan"], "colspan": c["colspan"],
            "tag": c["tag"], "id": c["id"], "scope": c["scope"],
            "headers": c["headers"], "text": c["text"],
            "covers": sorted(covers[i]),
            "covered_by": list(covered_by[i]),
            "reading": _reading(model, covered_by, i),
        })
    cap = table.find("caption", recursive=False)
    return {
        "path": table_path,
        "caption": cap.get_text(strip=True) if cap is not None else None,
        "rows": model["rows"], "cols": model["cols"],
        "cells": cells,
        "issues": check_table_el(book, href, table, table_path),
    }


def _ensure_cell_id(soup, tidx, cell):
    """为表头格生成稳定 id：由表序与网格坐标派生，同一文档状态下结果不变。"""
    el = cell["el"]
    if el.get("id"):
        return el["id"]
    base = "tbl%d-r%dc%d" % (tidx, cell["row"] + 1, cell["col"] + 1)
    hid, n = base, 2
    while soup.find(id=hid) is not None:
        hid = "%s-%d" % (base, n)
        n += 1
    el["id"] = hid
    return hid


def _iter_edits(coll):
    """cells/links 负载契约：前端按坐标键控的对象（{"r,c": {...}}）或数组，
    统一归一化为值列表迭代。"""
    if isinstance(coll, dict):
        return list(coll.values())
    return list(coll or [])


def apply_table_edits(book, href, table_path, edits, dirty=True):
    """应用表格工作区的一批编辑：caption / 逐格 tag、scope / 点选表头关联 / 批量推断。
    cells 与 links 既接受数组，也接受前端按坐标键控的对象。
    推断与人工指定冲突时保留人工选择并在 conflicts 中说明原因。
    返回 {"summary": [已应用的改动说明], "conflicts": [...]}。"""
    soup = book.soup(href)
    table = find_by_path(soup, table_path)
    if table is None or table.name != "table":
        raise ValueError("表格不存在: %s" % table_path)
    model = table_grid(table)
    by_coord = {(c["row"], c["col"]): c for c in model["cells"]}
    summary, conflicts, manual = [], [], set()

    def coord(c):
        return "第%d行第%d列" % (c["row"] + 1, c["col"] + 1)

    if "caption" in edits and edits["caption"] is not None:
        text = str(edits["caption"]).strip()
        cap = table.find("caption", recursive=False)
        old = cap.get_text(strip=True) if cap is not None else ""
        if text != old:
            if text:
                if cap is None:
                    cap = soup.new_tag("caption")
                    table.insert(0, cap)
                cap.string = text
                summary.append("caption「%s」" % text)
            else:
                cap.extract()
                summary.append("删除 caption")

    for ce in _iter_edits(edits.get("cells")):
        c = by_coord.get((ce.get("row"), ce.get("col")))
        if c is None:
            continue
        manual.add((c["row"], c["col"]))
        el = c["el"]
        if ce.get("tag") in ("th", "td") and el.name != ce["tag"]:
            summary.append("%s %s→%s" % (coord(c), el.name, ce["tag"]))
            el.name = ce["tag"]
        if "scope" in ce:
            v = (ce.get("scope") or "").strip().lower()
            cur = (el.get("scope") or "").strip().lower()
            if v != cur:
                if v:
                    el["scope"] = v
                elif el.has_attr("scope"):
                    del el["scope"]
                summary.append("%s scope: %s→%s" % (coord(c), cur or "（无）", v or "（无）"))

    tidx = soup.find_all("table").index(table) + 1
    for lk in _iter_edits(edits.get("links")):
        c = by_coord.get((lk.get("row"), lk.get("col")))
        if c is None:
            continue
        el = c["el"]
        ids = []
        for hid in lk.get("keep") or []:  # 无法解析到表内坐标的既有引用，原样保留
            if hid not in ids:
                ids.append(hid)
        for rc in lk.get("headers") or []:
            if not isinstance(rc, (list, tuple)) or len(rc) != 2:
                continue
            t = by_coord.get((rc[0], rc[1]))
            if t is None or t is c:
                continue
            hid = _ensure_cell_id(soup, tidx, t)
            if hid not in ids:
                ids.append(hid)
        cur = (el.get("headers") or "").split()
        if ids != cur:
            if ids:
                el["headers"] = " ".join(ids)
                summary.append("%s 关联表头 id: %s" % (coord(c), " ".join(ids)))
            else:
                del el["headers"]
                summary.append("%s 清除 headers 关联" % coord(c))

    kinds = edits.get("infer") or []
    if kinds:
        n, conf = _infer_headers(model, kinds, manual)
        conflicts.extend(conf)
        if n:
            summary.append("批量推断表头 %d 格" % n)
    if dirty:
        book.mark_dirty(href)
    return {"summary": summary, "conflicts": conflicts}


def _infer_headers(model, kinds, manual):
    """批量推断：首行 → 列表头（th + scope=col/colgroup），首列 → 行表头（th + scope=row/rowgroup）。
    与人工指定或既有语义冲突时保留原状并解释。返回 (改动格数, 冲突列表)。"""
    cells = model["cells"]
    conflicts = []
    changed = 0
    family = {"col": "col", "colgroup": "col", "row": "row", "rowgroup": "row"}

    def label(c):
        return "第%d行第%d列" % (c["row"] + 1, c["col"] + 1)

    def infer(c, want, kind_label):
        nonlocal changed
        el = c["el"]
        if (c["row"], c["col"]) in manual:
            conflicts.append({
                "row": c["row"] + 1, "col": c["col"] + 1,
                "kept": el.name, "inferred": "th scope=%s" % want,
                "reason": "%s 有人工指定的标记，保留人工选择，未采纳%s推断"
                          % (label(c), kind_label)})
            return
        if el.name == "th":
            cur = (el.get("scope") or "").strip().lower()
            if not cur:
                el["scope"] = want
                changed += 1
            elif family.get(cur) != family[want]:
                conflicts.append({
                    "row": c["row"] + 1, "col": c["col"] + 1,
                    "kept": "th scope=%s" % cur, "inferred": "th scope=%s" % want,
                    "reason": "%s 已标记为%s表头，与推断的%s冲突，保留原标记"
                              % (label(c),
                                 "行" if family.get(cur) == "row" else "列", kind_label)})
            return
        if el.get("headers"):
            conflicts.append({
                "row": c["row"] + 1, "col": c["col"] + 1,
                "kept": "td", "inferred": "th scope=%s" % want,
                "reason": "%s 带有 headers 显式关联，推断其作为数据格使用，保留 <td>"
                          % label(c)})
            return
        el.name = "th"
        el["scope"] = want
        changed += 1

    if "col" in kinds:
        for c in cells:
            if c["row"] == 0:
                infer(c, "colgroup" if c["colspan"] > 1 else "col", "列表头")
    if "row" in kinds:
        for c in cells:
            if c["col"] == 0 and c["row"] > 0:
                infer(c, "rowgroup" if c["rowspan"] > 1 else "row", "行表头")
    return changed, conflicts


def preview_table_edits(book, href, table_path, edits):
    """把暂存编辑应用到内存中的表格上，计算结果模型（网格/朗读/冲突）后回滚，不落盘。"""
    soup = book.soup(href)
    table = find_by_path(soup, table_path)
    if table is None or table.name != "table":
        raise ValueError("表格不存在: %s" % table_path)
    snapshot = BeautifulSoup(str(table), "xml").find("table")
    try:
        result = apply_table_edits(book, href, table_path, edits, dirty=False)
        model = table_model_json(book, href, table_path)
    finally:
        table.replace_with(snapshot)
    return model, result["conflicts"]


def restore_table(book, href, table_path, old_html):
    """撤销表格校修：用修改前序列化的 HTML 整体替换当前表格。"""
    soup = book.soup(href)
    table = find_by_path(soup, table_path)
    if table is None or table.name != "table":
        raise ValueError("表格不存在: %s" % table_path)
    table.replace_with(BeautifulSoup(old_html, "xml").find("table"))
    book.mark_dirty(href)


CHECKS = {
    "img_alt":            {"scope": "chapter", "fn": ch_img_alt},
    "heading_hierarchy":  {"scope": "chapter", "fn": ch_heading_hierarchy},
    "lang":               {"scope": "chapter", "fn": ch_lang},
    "duplicate_id":       {"scope": "chapter", "fn": ch_duplicate_id},
    "broken_anchor":      {"scope": "chapter", "fn": ch_broken_anchor},
    "footnote_backlink":  {"scope": "book",    "fn": ch_footnote_backlink},
    "toc":                {"scope": "book",    "fn": ch_toc},
    "table_a11y":         {"scope": "chapter", "fn": ch_table_a11y},
}

# 列表语义检查在 listlib 中实现（检查端与编辑工作区共用同一组发现），
# 因依赖顺序延后注册。
def _register_list_check():
    import listlib
    CHECKS["list_a11y"] = {"scope": "chapter", "fn": listlib.ch_list_a11y}


_register_list_check()

# 编辑动作 → 受影响的检查（只重跑这些）
AFFECTED_BY_ATTR = {
    "alt":           ["img_alt"],
    "lang":          ["lang"],
    "heading_level": ["heading_hierarchy", "toc"],
    "epub_type":     ["footnote_backlink", "toc"],
}
AFFECTED_BY_MOVE = ["heading_hierarchy", "toc", "footnote_backlink", "table_a11y",
                    "list_a11y"]
AFFECTED_BY_SPINE = ["toc"]


# ---------------- 编辑操作（返回逆操作所需信息） ----------------
def apply_updates(book, chapter, path, updates):
    """修改节点属性；updates: {alt?, lang?, heading_level?, epub_type?}。
    返回 (修改前旧值字典, 修改后的 dom_path)——标题改名会改变路径，
    撤销定位必须以修改后的路径为准。"""
    el = find_by_path(book.soup(chapter), path)
    if el is None:
        raise ValueError("节点不存在: %s" % path)
    old = {}
    for attr, value in updates.items():
        if attr == "heading_level":
            old[attr] = el.name
            v = str(value)
            el.name = v if v.startswith("h") else "h%d" % int(v)
        elif attr == "epub_type":
            old[attr] = el.get("epub:type")
            if value:
                el["epub:type"] = value
            elif el.has_attr("epub:type"):
                del el["epub:type"]
        elif attr == "alt":
            old[attr] = el.get("alt")
            if value is None:  # 撤销时恢复原状：原本没有 alt 就删除该属性
                if el.has_attr("alt"):
                    del el["alt"]
            else:
                el["alt"] = value
        elif attr == "lang":
            old[attr] = el.get("lang") or el.get("xml:lang")
            if value:
                el["lang"] = value
            elif el.has_attr("lang"):
                del el["lang"]
        else:
            raise ValueError("不支持的属性: %s" % attr)
    book.mark_dirty(chapter)
    return old, dom_path(el)


def move_node(book, chapter, path, before_path=None, parent_path=None):
    """把节点移动到 before_path 之前；before_path 为 None 时追加到 parent_path 末尾。"""
    soup = book.soup(chapter)
    el = find_by_path(soup, path)
    if el is None:
        raise ValueError("节点不存在: %s" % path)
    parent = el.parent
    siblings = [c for c in parent.find_all(recursive=False)]
    old = {"old_parent": dom_path(parent), "old_index": siblings.index(el)}
    el.extract()
    if before_path:
        target = find_by_path(soup, before_path)
        if target is None:
            raise ValueError("目标位置不存在: %s" % before_path)
        target.insert_before(el)
    else:
        new_parent = find_by_path(soup, parent_path) if parent_path else soup.find("body")
        new_parent.append(el)
    old["new_path"] = dom_path(el)
    book.mark_dirty(chapter)
    return old


def undo_move(book, chapter, new_path, old_parent, old_index):
    soup = book.soup(chapter)
    el = find_by_path(soup, new_path)
    parent = find_by_path(soup, old_parent) if old_parent else soup.find("body")
    el.extract()
    children = [c for c in parent.find_all(recursive=False)]
    if old_index < len(children):
        children[old_index].insert_before(el)
    else:
        parent.append(el)
    book.mark_dirty(chapter)


def reorder_spine(book, ordered_hrefs):
    old = list(book.spine)
    spine_el = book.opf.find("spine")
    by_href = {}
    for ir in spine_el.find_all("itemref"):
        m = book.manifest.get(ir.get("idref"))
        if m:
            by_href[m["href"]] = ir
    for ir in list(spine_el.find_all("itemref")):
        ir.extract()
    for h in ordered_hrefs:
        if h in by_href:
            spine_el.append(by_href[h])
    book.spine = list(ordered_hrefs)
    book.mark_dirty(book.opf_path)
    return old


def rebuild_landmarks_nav(book):
    """根据 spine 文档当前的地标，重建导航文档中 epub:type="landmarks" 的链接列表。
    在增删地标（epub:type 编辑）或调整章节顺序后调用，保证 nav 与正文同步。"""
    if not book.nav_path or book.nav_path not in book.raw:
        return
    nav = book.soup(book.nav_path)
    body = nav.find("body")
    if body is None:
        return

    nav_dir = posixpath.dirname(book.nav_path)
    entries = []
    for lm in book.collect_landmarks():
        rel = posixpath.relpath(lm["chapter"], nav_dir) if nav_dir else lm["chapter"]
        if lm["el_id"]:
            rel += "#" + lm["el_id"]
        entries.append((lm["epub_type"], rel, lm["text"] or lm["epub_type"]))

    old_nav = nav.find("nav", attrs={"epub:type": "landmarks"})
    if old_nav is not None:
        old_nav.extract()
    if entries:
        nav_el = nav.new_tag("nav")
        nav_el["epub:type"] = "landmarks"
        nav_el["id"] = "landmarks"
        h = nav.new_tag("h1")
        h.string = "地标"
        nav_el.append(h)
        ol = nav.new_tag("ol")
        for epub_type, rel, text in entries:
            li = nav.new_tag("li")
            a = nav.new_tag("a", href=rel)
            a["epub:type"] = epub_type
            a.string = text
            li.append(a)
            ol.append(li)
        nav_el.append(ol)
        body.append(nav_el)
    book.mark_dirty(book.nav_path)
