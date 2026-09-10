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


CHECKS = {
    "img_alt":            {"scope": "chapter", "fn": ch_img_alt},
    "heading_hierarchy":  {"scope": "chapter", "fn": ch_heading_hierarchy},
    "lang":               {"scope": "chapter", "fn": ch_lang},
    "duplicate_id":       {"scope": "chapter", "fn": ch_duplicate_id},
    "broken_anchor":      {"scope": "chapter", "fn": ch_broken_anchor},
    "footnote_backlink":  {"scope": "book",    "fn": ch_footnote_backlink},
    "toc":                {"scope": "book",    "fn": ch_toc},
}

# 编辑动作 → 受影响的检查（只重跑这些）
AFFECTED_BY_ATTR = {
    "alt":           ["img_alt"],
    "lang":          ["lang"],
    "heading_level": ["heading_hierarchy", "toc"],
    "epub_type":     ["footnote_backlink", "toc"],
}
AFFECTED_BY_MOVE = ["heading_hierarchy", "toc", "footnote_backlink"]
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
