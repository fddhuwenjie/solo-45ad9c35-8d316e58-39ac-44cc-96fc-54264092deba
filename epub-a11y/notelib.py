"""注释关系图：引用（noteref）与脚注/尾注的扫描、检查、两栏工作区编辑。

设计要点（与 listlib / 表格工作区同一套思路）：
- 检查端 ch_note_a11y 与工作区模型 note_model_json 共用 build_graph 的同一组发现：
  未声明的引用/注释、一号多注、孤立尾注、嵌套注释、回链遗漏、跨章错指、
  跨书目链接、注释环路、尾注区混入普通列表项。EPUB 导出、修改明细与问题报告
  都消费这同一份关系结果。
- 扫描同时识别 epub:type（noteref/footnote/endnote/footnotes/endnotes 词元）、
  ARIA role（doc-noteref/doc-footnote/doc-endnote）、id、href 与可见注号
  （[1]、【1】、〔1〕、（注1）、行首 “1.” 等）。
- 编辑（标类型、连线、为多次引用分别建立回程目标、删除、批量套用）都走
  commit_note_edits：先快照所有受影响文档，结构改完后重算关系图做守恒校验
  （环路 / 跨书目 / 删仍被引用的注释一律拒绝并回滚），逆操作整体还原文档，
  因此每次提交都可撤销，撤销后原 ID 与链接逐字恢复。
"""
import posixpath
import re

from bs4 import BeautifulSoup

from epublib import dom_path, find_by_path

NOTE_CHECK = "note_a11y"

REF_TOKEN = "noteref"
NOTE_TYPES = ("footnote", "endnote")
ROLE_OF = {"noteref": "doc-noteref", "footnote": "doc-footnote",
           "endnote": "doc-endnote"}
SECTION_OF = {"footnote": "footnotes", "endnote": "endnotes"}
# 可承载注释语义的元素；普通 <p> 只是注释正文，不能自身成为注释节点
NOTE_EL_TAGS = ("aside", "section", "div", "li")

# 可见注号：[1] 【1】 〔1〕 (1) （注1）……
_MARK_BRACKET = re.compile(
    r"[\[【〔（(]\s*(?:注|註)?\s*0*(\d{1,4})\s*[\]】〕）)]")
_MARK_BRACKET_FULL = re.compile(r"^\s*" + _MARK_BRACKET.pattern + r"\s*$")
# 注释正文行首：1. / 1、 / 注1． ……
_MARK_LEAD = re.compile(
    r"^\s*(?:注|註)?\s*0*(\d{1,4})\s*[.．、]")
_EXTERNAL_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)

# ---------------- 基础工具 ----------------


def _tokens(el):
    return set((el.get("epub:type") or "").split())


def _role(el):
    return (el.get("role") or "").strip().lower()


def _is_ref(el):
    return el.name == "a" and (
        REF_TOKEN in _tokens(el) or _role(el) == ROLE_OF["noteref"])


def _own_note_type(el):
    """元素自身声明的注释类型：epub:type 词元优先，其次 ARIA role。"""
    toks = _tokens(el)
    for t in NOTE_TYPES:
        if t in toks:
            return t
    role = _role(el)
    for t in NOTE_TYPES:
        if role == ROLE_OF[t]:
            return t
    return None


def _section_note_type(el):
    """祖先 epub:type="footnotes/endnotes" 区段所声明的注释类型。"""
    p = el.parent
    while p is not None and getattr(p, "name", None):
        toks = _tokens(p)
        if "endnotes" in toks:
            return "endnote"
        if "footnotes" in toks:
            return "footnote"
        p = p.parent
    return None


def _note_ancestor(el):
    """最近的、自身带有注释语义的祖先元素。"""
    p = el.parent
    while p is not None and getattr(p, "name", None):
        if _own_note_type(p):
            return p
        p = p.parent
    return None


def note_number(text, full=False):
    """从可见文本取注号。full=True 时整段必须恰好是一个带括号注号（用于引用）。"""
    if not text:
        return None
    s = text.strip()
    if full:
        m = _MARK_BRACKET_FULL.match(s)
        if m:
            return int(m.group(1))
        return None
    m = _MARK_BRACKET.match(s)
    if m:
        return int(m.group(1))
    m = _MARK_LEAD.match(s)
    if m:
        return int(m.group(1))
    return None


def _marker_text(el):
    m = _MARK_BRACKET.match((el.get_text() or "").strip())
    return m.group(0) if m else ""


def _issue(chapter, path, severity, message, keys=None):
    d = {"chapter": chapter, "path": path, "check": NOTE_CHECK,
         "severity": severity, "message": message}
    if keys:
        d["keys"] = keys
    return d


def _is_external(href):
    if not href:
        return False
    h = href.strip().lower()
    return h.startswith(("http://", "https://", "mailto:", "tel:", "ftp://")) \
        or bool(_EXTERNAL_RE.match(h)) and not h.startswith("#")


# ---------------- 关系图 ----------------
def build_graph(book):
    """扫描全书 spine 文档，返回注释关系图（检查 / 模型 / 试听 / 提交校验共用）。"""
    docs = [h for h in book.spine if h in book.raw and h != book.nav_path]
    refs, notes = [], []
    note_by_el = {}   # id(el) -> note 记录

    for spi, href in enumerate(docs):
        soup = book.soup(href)
        body = soup.find("body")
        if body is None:
            continue
        all_els = body.find_all(True)
        order_of = {id(el): i for i, el in enumerate(all_els)}

        # ---- 注释节点 ----
        for el in all_els:
            own = _own_note_type(el)
            sec = _section_note_type(el)
            if not own and not sec:
                continue
            if el.name == "nav" or el.find_parent("nav"):
                continue
            # 注释正文（普通 p/span 等）与容器内后代不再另算一个注释节点；
            # 嵌套注释只在后代自身用 aside/section/div/li 声明时识别
            parent_note = _note_ancestor(el)
            if parent_note is not None and not (
                    own and el.name in NOTE_EL_TAGS):
                continue
            if el.name not in NOTE_EL_TAGS:
                continue
            text = el.get_text(" ", strip=True)
            num = note_number(text)
            declared = bool(own)
            ntype = own or sec
            mixed_plain = False
            if not declared and el.name == "li" and num is None:
                # 尾注/脚注区的列表项却没有注号：普通列表项混入
                mixed_plain = True
            if not declared and num is None and not mixed_plain:
                continue
            rec = {
                "key": "%s|%s" % (href, dom_path(el)),
                "chapter": href, "spine_pos": spi,
                "order": order_of[id(el)], "path": dom_path(el),
                "tag": el.name, "el_id": el.get("id"),
                "declared": declared, "note_type": ntype,
                "in_section": bool(sec),
                "number": num, "marker": _marker_text(el),
                "nested": parent_note is not None,
                "parent_note_key": None,
                "text": text[:60],
                "ref_keys": [], "backlinks": [],
                "orphan": False, "mixed_plain": mixed_plain,
            }
            if parent_note is not None:
                rec["parent_note_key"] = "%s|%s" % (
                    href, dom_path(parent_note))
            notes.append(rec)
            note_by_el[id(el)] = rec

        # ---- 引用节点（先登记声明的 noteref，再补可见注号，避免重复） ----
        for el in all_els:
            if el.name == "nav" or el.find_parent("nav"):
                continue
            declared = _is_ref(el)
            text = el.get_text(" ", strip=True)
            num = note_number(text, full=True)
            in_note = _note_ancestor(el)
            href_attr = el.get("href") if el.name == "a" else None
            # 已被 noteref 锚点包裹的 sup/span（标记/连线时自动包裹产生）不再另算
            anc_a = el.find_parent("a") if el.name != "a" else None
            if anc_a is not None and _is_ref(anc_a):
                continue
            if not declared:
                # 可见注号：<sup>[1]</sup> 总是算；span 仅在 class 带 note/fn 提示时算；
                # <a> 只要注号完整且目标落在注释上（下方解析时再判定）也算。
                if el.name == "sup" and num is not None:
                    pass
                elif el.name == "span" and num is not None and \
                        re.search(r"note|fn|footnote|endnote",
                                  el.get("class") and " ".join(el.get("class")) or "",
                                  re.I):
                    pass
                elif el.name == "a" and num is not None and href_attr:
                    pass  # 是否为未声明引用，取决于目标是否为注释
                else:
                    continue
            rec = {
                "key": "%s|%s" % (href, dom_path(el)),
                "chapter": href, "spine_pos": spi,
                "order": order_of[id(el)], "path": dom_path(el),
                "tag": el.name, "el_id": el.get("id"),
                "declared": declared, "number": num,
                "marker": _marker_text(el) or text[:8],
                "href": href_attr, "external": _is_external(href_attr or ""),
                "within_note": "%s|%s" % (href, dom_path(in_note))
                if in_note is not None else None,
                "target_key": None, "target_kind": None,
                "target_missing": False,
                "context": _ref_context(el),
            }
            refs.append(rec)

    # ---- 解析引用目标，建立连线 ----
    edges = []
    for r in refs:
        h = r["href"]
        if not h:
            continue
        if r["external"]:
            r["target_kind"] = "external"
            continue
        frag = h.split("#", 1)[1] if "#" in h else ""
        file_part = h.split("#", 1)[0] if "#" in h else ""
        target_doc = book.resolve(r["chapter"], file_part) if file_part \
            else r["chapter"]
        if target_doc not in book.raw:
            r["target_missing"] = True
            r["target_kind"] = "missing_doc"
            continue
        if not frag:
            continue
        tel = book.soup(target_doc).find(id=frag)
        if tel is None:
            r["target_missing"] = True
            r["target_kind"] = "missing_frag"
            continue
        tn = note_by_el.get(id(tel))
        if tn is None:
            anc = _note_ancestor(tel)
            tn = note_by_el.get(id(anc)) if anc is not None else None
            if tn is not None:
                r["target_kind"] = "note_inner"
        if tn is None:
            # 未声明 <a>[n]→非注释</a>：不作为注释关系候选，交给失效锚点等检查
            if not r["declared"]:
                continue
            r["target_kind"] = "nonnote"
            continue
        r["target_key"] = tn["key"]
        if r["target_kind"] is None:
            r["target_kind"] = "note"
        tn["ref_keys"].append(r["key"])
        edges.append({"ref_key": r["key"], "note_key": tn["key"]})

    # ---- 回链解析：注释内每个指向引用的 <a href="#id"> ----
    for n in notes:
        soup = book.soup(n["chapter"])
        el = find_by_path(soup, n["path"])
        if el is None:
            continue
        for a in el.find_all("a", href=True):
            lh = a["href"]
            if _is_external(lh):
                n["backlinks"].append({
                    "href": lh, "external": True, "ref_key": None,
                    "text": a.get_text(" ", strip=True)[:12]})
                continue
            if "#" not in lh:
                continue
            lf, lfrag = lh.split("#", 1)
            lt = book.resolve(n["chapter"], lf) if lf else n["chapter"]
            target_ref = next((rr for rr in refs
                               if rr["chapter"] == lt and rr["el_id"] == lfrag),
                              None)
            n["backlinks"].append({
                "href": lh, "external": False,
                "ref_key": target_ref["key"] if target_ref else None,
                "target_doc": lt, "target_frag": lfrag,
                "text": a.get_text(" ", strip=True)[:12]})

    graph = {"docs": docs, "refs": refs, "notes": notes, "edges": edges,
             "issues": [], "blocks": {"cycles": [], "external": [],
                                      "unmatched": []}}
    graph["issues"] = _derive_issues(book, graph)
    graph["batches"] = _batch_candidates(graph)
    graph["sequence"] = _listening_sequence(graph)
    return graph


def _ref_context(el):
    """引用处前后若干字，供试听顺序与两栏预览使用。"""
    txt = el.parent.get_text(" ", strip=True) if el.parent else ""
    return txt[:40]


def _derive_issues(book, g):
    issues = []
    refs, notes = g["refs"], g["notes"]
    note_by_key = {n["key"]: n for n in notes}
    ref_by_key = {r["key"]: r for r in refs}

    # 1) 未声明的引用
    for r in refs:
        if r["declared"]:
            continue
        if r["tag"] == "a" and r["target_key"]:
            issues.append(_issue(
                r["chapter"], r["path"], "warning",
                "可见注号「%s」链到了注释 %s，但引用本身缺少 epub:type=\"noteref\""
                "/role=\"doc-noteref\" 声明（未声明的引用）"
                % (r["marker"] or r["number"],
                   "#" + (note_by_key[r["target_key"]]["el_id"] or "")),
                ["ref:" + r["key"], "note:" + r["target_key"]]))
        elif r["tag"] != "a":
            issues.append(_issue(
                r["chapter"], r["path"], "warning",
                "可见注号「%s」没有 noteref 语义也没有连线（未声明的引用）"
                % (r["marker"] or r["number"]),
                ["ref:" + r["key"]]))

    # 2) 未声明的注释 / 尾注区混入普通列表项
    for n in notes:
        if n["mixed_plain"]:
            issues.append(_issue(
                n["chapter"], n["path"], "warning",
                "%s区的列表项「%s」没有注号也没有注释语义，是混入尾注/脚注区的"
                "普通列表项" % ("尾注" if n["note_type"] == "endnote" else "脚注",
                              n["text"][:16]),
                ["note:" + n["key"]]))
        elif not n["declared"]:
            issues.append(_issue(
                n["chapter"], n["path"], "warning",
                "可见注号 %s 的「%s」缺少 epub:type=\"%s\"/role 声明"
                "（未声明的注释）"
                % (n["number"], n["text"][:16], n["note_type"] or "footnote"),
                ["note:" + n["key"]]))

    # 3) 连线问题：跨书目 / 目标非注释 / 跨章错指
    for r in refs:
        if r["external"]:
            issues.append(_issue(
                r["chapter"], r["path"], "error",
                "注释引用「%s」指向 EPUB 之外的跨书目链接 %s，读屏无法在本书内跳转"
                % (r["marker"] or r["number"], r["href"]),
                ["ref:" + r["key"]]))
            g["blocks"]["external"].append({
                "chapter": r["chapter"], "path": r["path"],
                "href": r["href"], "marker": r["marker"]})
        elif r["target_kind"] == "nonnote":
            frag = r["href"].split("#", 1)[-1]
            issues.append(_issue(
                r["chapter"], r["path"], "error",
                "注释引用「%s」的目标 #%s 不是注释节点（缺少 footnote/endnote 语义）"
                % (r["marker"] or r["number"], frag),
                ["ref:" + r["key"]]))
        elif r["target_key"] and r["declared"]:
            tn = note_by_key[r["target_key"]]
            # 脚注应与引用它的正文同章；目标章本身设有脚注区却被跨章引用即错指
            if tn["note_type"] == "footnote" and tn["chapter"] != r["chapter"] \
                    and _chapter_has_notes_section(book, tn["chapter"]):
                issues.append(_issue(
                    r["chapter"], r["path"], "error",
                    "注号「%s」是脚注引用，却跨章指向 %s 的脚注 #%s（跨章错指）："
                    "该章脚注应与正文同章"
                    % (r["marker"] or r["number"],
                       posixpath.basename(tn["chapter"]), tn["el_id"]),
                    ["ref:" + r["key"], "note:" + tn["key"]]))

    # 4) 回链遗漏 + 多次引用共用回程
    for n in notes:
        incoming = [ref_by_key[k] for k in n["ref_keys"]
                    if k in ref_by_key]
        n["orphan"] = not incoming
        have = {b.get("ref_key") for b in n["backlinks"] if not b.get("external")}
        for r in incoming:
            if not r["el_id"]:
                issues.append(_issue(
                    r["chapter"], r["path"], "warning",
                    "注号「%s」的引用没有 id，无法建立回到此处的回程目标"
                    % (r["marker"] or r["number"]),
                    ["ref:" + r["key"], "note:" + n["key"]]))
            elif r["key"] not in have:
                if len(incoming) > 1:
                    issues.append(_issue(
                        n["chapter"], n["path"], "warning",
                        "注释 #%s 被 %d 处引用（%s），但缺少回到注号「%s」"
                        "（%s）的回程链接；共用回链只能把读者送回第一处"
                        % (n["el_id"] or n["number"], len(incoming),
                           "、".join(x["marker"] or str(x["number"])
                                     for x in incoming),
                           r["marker"] or r["number"],
                           posixpath.basename(r["chapter"])),
                        ["note:" + n["key"], "ref:" + r["key"]]))
                else:
                    issues.append(_issue(
                        n["chapter"], n["path"], "warning",
                        "注释 #%s 缺少回到注号「%s」（%s）的回程链接"
                        % (n["el_id"] or n["number"],
                           r["marker"] or r["number"],
                           posixpath.basename(r["chapter"])),
                        ["note:" + n["key"], "ref:" + r["key"]]))
        if len(incoming) > 1:
            issues.append(_issue(
                n["chapter"], n["path"], "warning",
                "一号多引：注号「%s」在正文中出现 %d 次，需为每次引用分别建立"
                "回程目标" % (incoming[0]["marker"] or n["number"],
                            len(incoming)),
                ["note:" + n["key"]] + ["ref:" + r["key"] for r in incoming]))
        for b in n["backlinks"]:
            if b.get("external"):
                issues.append(_issue(
                    n["chapter"], n["path"], "error",
                    "注释 #%s 的回程链接指向 EPUB 之外：%s（跨书目链接）"
                    % (n["el_id"] or n["number"], b["href"]),
                    ["note:" + n["key"]]))

    # 5) 一号多注：脚注按章去重，尾注全书去重（只比可见注号）
    groups = {}
    for n in notes:
        if n["number"] is None or not n["note_type"]:
            continue
        scope = n["chapter"] if n["note_type"] == "footnote" else "*"
        groups.setdefault((n["note_type"], scope, n["number"]), []).append(n)
    for (ntype, scope, num), members in groups.items():
        if len(members) < 2:
            continue
        for n in members[1:]:
            others = "、".join("#%s" % (m["el_id"] or "?") for m in members
                               if m is not n)
            issues.append(_issue(
                n["chapter"], n["path"], "error",
                "%s注号 %s 一号多注：%s 与 %s 同号"
                % ("脚" if ntype == "footnote" else "尾", num,
                   "#" + (n["el_id"] or "?"), others),
                ["note:" + x["key"] for x in members]))

    # 6) 孤立注释 / 嵌套注释（嵌套注释的问题以嵌套错误为准，不再叠加孤立告警）
    for n in notes:
        if n["orphan"] and not n["mixed_plain"] and not n["nested"]:
            label = "孤立尾注" if n["note_type"] == "endnote" else "孤立脚注"
            issues.append(_issue(
                n["chapter"], n["path"], "warning",
                "%s：%s「%s」没有被任何正文引用指向"
                % (label, "#" + (n["el_id"] or str(n["number"] or "?")),
                   n["text"][:18]),
                ["note:" + n["key"]]))
        if n["nested"]:
            issues.append(_issue(
                n["chapter"], n["path"], "error",
                "注释嵌套：注释「%s」位于另一注释 %s 之内，读屏无法正确返回"
                % (n["text"][:16],
                   "#" + (note_by_key[n["parent_note_key"]]["el_id"] or "?")
                   if n["parent_note_key"] in note_by_key else "?"),
                ["note:" + n["key"],
                 "note:" + n["parent_note_key"]] if n["parent_note_key"]
                else ["note:" + n["key"]]))

    # 7) 注释环路（注释内的 noteref 又指回注释链上的祖先）
    adj = {}
    for r in refs:
        if r["within_note"] and r["target_key"]:
            adj.setdefault(r["within_note"], []).append(r["target_key"])
    for scc in _cycles(adj, [n["key"] for n in notes]):
        g["blocks"]["cycles"].append(scc)
        chain = " → ".join(
            "#%s" % (note_by_key[k]["el_id"] or "?") for k in scc + scc[:1])
        sources = "、".join(sorted(set(
            posixpath.basename(note_by_key[k]["chapter"]) for k in scc)))
        for k in scc:
            n = note_by_key[k]
            issues.append(_issue(
                n["chapter"], n["path"], "error",
                "注释引用环路：%s，跳转关系永远无法回到正文（来源：%s）"
                % (chain, sources),
                ["note:" + k for k in scc]))
    return issues


def _chapter_has_notes_section(book, href):
    soup = book.soup(href)
    for el in soup.find_all(attrs={"epub:type": True}):
        if "footnotes" in (el.get("epub:type") or "").split():
            return True
    return False


def _cycles(adj, nodes):
    """Tarjan 强连通分量：大小>1 或自环即环路。"""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]

    def connect(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in adj.get(v, []):
            if w not in index:
                connect(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1 or v in adj.get(v, []):
                out.append(scc)

    for v in nodes:
        if v not in index:
            connect(v)
    return out


# ---------------- 批量套用候选 ----------------
def _batch_candidates(g):
    """编号连续且关系唯一的引用↔注释配对才可批量套用。

    脚注按章配对、尾注全书配对；注释侧优先取未声明注释，其次取尚无引用的
    已声明注释（关系仍唯一）。返回组内每项都带 applicable/reason，
    不满足条件的候选进入 blocked.unmatched 并列出来源。"""
    groups, unmatched = [], []
    by_chapter = {}
    for r in g["refs"]:
        if r["declared"] or r["number"] is None or r["within_note"] \
                or r["external"] or r["target_key"]:
            continue
        by_chapter.setdefault(r["chapter"], []).append(r)

    gid = 0
    for chapter, rs in by_chapter.items():
        rs.sort(key=lambda r: r["order"])
        ref_count = {}
        for r in rs:
            ref_count[r["number"]] = ref_count.get(r["number"], 0) + 1
        matched = []
        for r in rs:
            if ref_count[r["number"]] > 1:
                unmatched.append({"chapter": chapter, "path": r["path"],
                                   "number": r["number"],
                                   "reason": "同章存在多个注号 %s，关系不唯一"
                                   % r["number"]})
                continue
            cand = _match_note(g, chapter, r["number"])
            if cand is None:
                unmatched.append({"chapter": chapter, "path": r["path"],
                                   "number": r["number"],
                                   "reason": "注号 %s 找不到唯一的注释配对"
                                   % r["number"]})
                continue
            note, ntype = cand
            matched.append((r, note, ntype))
        matched.sort(key=lambda x: (x[0]["number"], x[2]))
        run = []
        for item in matched:
            if run and (item[0]["number"] != run[-1][0]["number"] + 1
                        or item[2] != run[-1][2]):
                _emit_batch(groups, gid, chapter, run)
                gid += 1
                run = []
            run.append(item)
        if run:
            _emit_batch(groups, gid, chapter, run)
            gid += 1
    g["blocks"]["unmatched"] = unmatched
    return groups


def _match_note(g, chapter, number):
    """为某章某注号找唯一注释目标：未声明注释优先，其次尚无引用的已声明注释。"""
    und = [n for n in g["notes"]
           if not n["declared"] and not n["mixed_plain"]
           and n["number"] == number and (
               n["note_type"] == "endnote"
               or (n["note_type"] == "footnote" and n["chapter"] == chapter))]
    if len(und) == 1:
        return und[0], und[0]["note_type"]
    if und:
        return None
    free = [n for n in g["notes"]
            if n["declared"] and n["number"] == number and not n["ref_keys"]
            and (n["note_type"] == "endnote"
                 or (n["note_type"] == "footnote"
                     and n["chapter"] == chapter))]
    if len(free) == 1:
        return free[0], free[0]["note_type"]
    return None


def _emit_batch(groups, gid, chapter, run):
    ntype = run[0][2]
    items = [{
        "number": r["number"], "ref_key": r["key"], "note_key": n["key"],
        "note_type": t, "applicable": t == ntype,
        "reason": "" if t == ntype else "同组内脚注/尾注类型不一致",
    } for r, n, t in run]
    groups.append({
        "id": "batch-%d" % gid, "chapter": chapter, "note_type": ntype,
        "start": run[0][0]["number"], "end": run[-1][0]["number"],
        "applicable": all(i["applicable"] for i in items),
        "items": items,
    })


# ---------------- 试听顺序 ----------------
def _listening_sequence(g):
    """模拟线性朗读：按 spine/文档顺序逐条“正文注号 → 注释 → 回程”。"""
    lines = []
    refs = sorted([r for r in g["refs"] if not r["within_note"]],
                  key=lambda r: (r["spine_pos"], r["order"]))
    note_by_key = {n["key"]: n for n in g["notes"]}
    ref_by_key = {r["key"]: r for r in g["refs"]}
    for r in refs:
        lines.append({
            "kind": "ref", "chapter": r["chapter"], "path": r["path"],
            "text": "正文注号「%s」：%s"
                    % (r["marker"] or r["number"] or "?", r["context"])})
        if not r["declared"]:
            lines.append({"kind": "warn", "chapter": r["chapter"],
                          "path": r["path"],
                          "text": "　⚠ 未声明的引用，读屏不会把它识别为可跳转注释"})
            continue
        if r["external"]:
            lines.append({"kind": "warn", "chapter": r["chapter"],
                          "path": r["path"],
                          "text": "　⚠ 跨书目链接：%s（本书内无法到达注释）"
                                  % r["href"]})
            continue
        tn = note_by_key.get(r["target_key"])
        if tn is None:
            lines.append({"kind": "warn", "chapter": r["chapter"],
                          "path": r["path"],
                          "text": "　⚠ 引用未连线到任何注释"})
            continue
        lines.append({
            "kind": "note", "chapter": tn["chapter"], "path": tn["path"],
            "text": "　↳ %s：%s" % ("尾注" if tn["note_type"] == "endnote"
                                    else "脚注", tn["text"])})
        bl = next((b for b in tn["backlinks"]
                   if b.get("ref_key") == r["key"]), None)
        if bl:
            lines.append({"kind": "back", "chapter": tn["chapter"],
                          "path": tn["path"],
                          "text": "　　↩ 回到本引用处（%s）"
                                  % (r["marker"] or r["number"])})
        else:
            lines.append({
                "kind": "warn", "chapter": tn["chapter"], "path": tn["path"],
                "text": "　　⚠ 没有回到本引用的回程链接%s"
                        % ("（共用回链只回到第一处）"
                           if len(tn["ref_keys"]) > 1 else "")})
    for n in g["notes"]:
        if n["orphan"] and not n["mixed_plain"] and not n["nested"]:
            lines.append({"kind": "warn", "chapter": n["chapter"],
                          "path": n["path"],
                          "text": "孤立%s（不会被朗读顺序到达）：%s"
                                  % ("尾注" if n["note_type"] == "endnote"
                                     else "脚注", n["text"][:30])})
    return lines


# ---------------- 模型 JSON ----------------
def _ref_json(r):
    return {k: r[k] for k in (
        "key", "chapter", "path", "tag", "el_id", "declared", "number",
        "marker", "href", "external", "within_note", "target_key",
        "target_kind", "target_missing", "context", "spine_pos", "order")}


def _note_json(n):
    return {k: n[k] for k in (
        "key", "chapter", "path", "tag", "el_id", "declared", "note_type",
        "in_section", "number", "marker", "nested", "parent_note_key",
        "text", "ref_keys", "backlinks", "orphan", "mixed_plain",
        "spine_pos", "order")}


def note_model_json(book, graph=None):
    g = graph or build_graph(book)
    return {
        "chapters": [{"href": h, "name": posixpath.basename(h)}
                     for h in g["docs"]],
        "refs": [_ref_json(r) for r in g["refs"]],
        "notes": [_note_json(n) for n in g["notes"]],
        "edges": g["edges"],
        "issues": g["issues"],
        "blocks": g["blocks"],
        "batches": g["batches"],
        "sequence": g["sequence"],
    }


def ch_note_a11y(book):
    return build_graph(book)["issues"]


# ---------------- 编辑提交 ----------------
def _split_key(key):
    chapter, path = key.split("|", 1)
    return chapter, path


def _resolve(book, key):
    chapter, path = _split_key(key)
    if chapter not in book.raw:
        raise ValueError("节点所在章节不在书中：%s" % chapter)
    el = find_by_path(book.soup(chapter), path)
    if el is None:
        raise ValueError("节点定位失败（可能已被先前的改动移动）：%s" % path)
    return chapter, el


def _ensure_id(soup, el, base):
    if el.get("id"):
        return el["id"]
    cand = base
    n = 2
    while soup.find(id=cand) is not None:
        cand = "%s-%d" % (base, n)
        n += 1
    el["id"] = cand
    return cand


def _set_semantic(el, kind):
    toks = _tokens(el)
    toks.discard("noteref")
    toks.discard("footnote")
    toks.discard("endnote")
    toks.add(kind)
    el["epub:type"] = " ".join(sorted(toks))
    el["role"] = ROLE_OF[kind]


def _clear_semantic(el, kind):
    toks = _tokens(el)
    if kind in toks:
        toks.discard(kind)
        if toks:
            el["epub:type"] = " ".join(sorted(toks))
        elif el.has_attr("epub:type"):
            del el["epub:type"]
    if _role(el) == ROLE_OF[kind] and el.has_attr("role"):
        del el["role"]


def _relative_href(from_doc, to_doc, frag):
    if from_doc == to_doc:
        return "#" + frag
    base = posixpath.relpath(to_doc, posixpath.dirname(from_doc))
    return "%s#%s" % (base, frag)


def _as_anchor(book, chapter, el, old_key=None, remap=None):
    """把可见注号（sup/span/a 等）变成带 noteref 语义的 <a>，非锚点就地包裹。"""
    if el.name != "a":
        a = book.soup(chapter).new_tag("a")
        el.wrap(a)
        el = a
        if remap is not None and old_key:
            remap[old_key] = "%s|%s" % (chapter, dom_path(el))
    _set_semantic(el, "noteref")
    return el


def _cycle_signatures(graph):
    """环路的稳定签名：用注释的 el_id（删除/移动兄弟时 dom_path 会变，id 不会）。"""
    by_key = {n["key"]: n for n in graph["notes"]}
    out = set()
    for scc in graph["blocks"]["cycles"]:
        ids = tuple(sorted(
            (by_key[k]["el_id"] or by_key[k]["path"]) for k in scc
            if k in by_key))
        if ids:
            out.add(ids)
    return out


def _guard_graph(book, before_graph):
    """守恒校验：本次修改若新形成注释环路或新引入跨书目链接则抛 ValueError。
    提交前就已存在的问题仍由问题报告列出，不阻止无关修复。环路按稳定 id
    比较（删除同区其他注释会改变 dom_path，但既有环路不应被误判为新环路）。"""
    g = build_graph(book)
    new_cycle_ids = _cycle_signatures(g) - _cycle_signatures(before_graph)
    if new_cycle_ids:
        by_id = {n["el_id"]: n for n in g["notes"] if n["el_id"]}
        chains, sources = [], set()
        for ids in sorted(new_cycle_ids):
            chains.append(" → ".join("#" + i for i in ids + ids[:1]))
            for i in ids:
                n = by_id.get(i)
                if n:
                    sources.add(posixpath.basename(n["chapter"]))
        raise ValueError("连线会形成注释环路（%s），提交被阻止；来源：%s"
                         % ("；".join(chains), "、".join(sorted(sources))))
    before_external = {(e["chapter"], e["path"], e["href"])
                       for e in before_graph["blocks"]["external"]}
    ext = {(e["chapter"], e["path"], e["href"])
           for e in g["blocks"]["external"]}
    new_ext = ext - before_external
    if new_ext:
        raise ValueError(
            "提交会新引入跨书目链接，被阻止；来源：%s"
            % "、".join("%s %s（%s）"
                       % (posixpath.basename(ch), path, href)
                       for ch, path, href in sorted(new_ext)))
    return g


def _key_doc(key):
    try:
        return _split_key(key)[0]
    except (ValueError, AttributeError):
        return None


def affected_docs(book, edits):
    """计算一批编辑实际会改写的全部 spine 章节。

    关键点：
    - backlinks 同时改写引用章（可能自动补 id/包裹为锚点）与注释章（写入回链），
      跨章时二者不同，缺一就会漏写回链或漏掉引用章的 id 变更；
    - links 同理（引用章写 href，注释章可能自动补 id）；
    - batch_group 按当前关系图解析出组内每个 ref/note 的章节
      （尾注全书匹配时注释可能在另一章），不能只看负载里出现的 key。"""
    docs = set()

    def add(key):
        doc = _key_doc(key or "")
        if doc:
            docs.add(doc)

    for item in edits.get("marks") or []:
        add(item.get("key"))
    for item in edits.get("unmarks") or []:
        add(item.get("key"))
    for item in edits.get("unlinks") or []:
        add(item.get("key"))
    for item in edits.get("deletes") or []:
        add(item.get("key"))
    for lk in edits.get("links") or []:
        add(lk.get("ref_key"))
        add(lk.get("note_key"))
    for bk in edits.get("backlinks") or []:
        add(bk.get("ref_key"))
        add(bk.get("note_key"))

    gid = edits.get("batch_group")
    if gid:
        g = build_graph(book)
        group = next((x for x in g["batches"] if x["id"] == gid), None)
        if group is not None:
            for it in group["items"]:
                add(it.get("ref_key"))
                add(it.get("note_key"))
    return {h for h in docs if h in book.raw}


def commit_note_edits(book, edits):
    """应用一批注释关系编辑，返回 (summary, affected_docs)。

    任何守恒校验失败都先整体回滚受影响文档再抛出 ValueError；成功后文档树
    已修改（由调用方 save()），逆操作所需的改前快照由 take_snapshot 在
    动作开始前取得。"""
    marks = edits.get("marks") or []
    unmarks = edits.get("unmarks") or []
    links = edits.get("links") or []
    unlinks = edits.get("unlinks") or []
    backlinks = edits.get("backlinks") or []
    deletes = edits.get("deletes") or []
    summary = []
    remap = {}  # 非锚点引用被包裹为 <a> 后：旧 key → 新锚点 key

    # 受影响章节统一由 affected_docs 解析（含跨章 backlinks/links 双方
    # 与 batch_group 展开的全部章节），快照、dirty、写回 EPUB 都以此为准。
    affected = affected_docs(book, edits)
    snapshots = {h: str(book.soup(h)) for h in affected}
    # 提交前已存在的环路 / 跨书目链接：允许保留（本就作为问题报告），
    # 守恒校验只阻止本次提交新引入的关系问题。
    before_graph = build_graph(book)

    try:
        # 0) 断开连线
        for uk in unlinks:
            chapter, el = _resolve(book, uk["key"])
            if el.name == "a" and el.has_attr("href"):
                del el["href"]
                summary.append("断开注号「%s」的连线"
                               % (el.get_text(" ", strip=True)[:8]))

        # 1) 删除（守恒校验：仍被引用的注释一律拒绝，并列出来源）
        for dk in deletes:
            chapter, el = _resolve(book, dk["key"])
            ntype = _own_note_type(el) or _section_note_type(el) or "注释"
            sources = _incoming_sources(book, chapter, el)
            if sources:
                raise ValueError(
                    "注释 %s「%s」仍被 %d 处引用（%s），删除被阻止；"
                    "请先断开这些连线"
                    % ("#" + (el.get("id") or "?"),
                       el.get_text(" ", strip=True)[:12], len(sources),
                       "、".join(sources)))
            el.extract()
            summary.append("删除%s" % ntype)

        # 2) 标注 / 取消标注
        for item in marks:
            kind = item.get("kind")
            if kind not in ROLE_OF:
                raise ValueError("未知节点类型：%r" % kind)
            chapter, el = _resolve(book, item["key"])
            old_key = item["key"]
            if kind == "noteref":
                if el.name != "a":
                    a = book.soup(chapter).new_tag("a")
                    el.wrap(a)
                    el = a
                    remap[old_key] = "%s|%s" % (chapter, dom_path(el))
                _set_semantic(el, "noteref")
            else:
                if el.name not in NOTE_EL_TAGS:
                    raise ValueError(
                        "<%s> 不适合标记为 %s（仅允许 aside/section/div/li）"
                        % (el.name, kind))
                _set_semantic(el, kind)
            summary.append("标记 %s 为 %s"
                           % ("#" + (el.get("id") or el.name), kind))
        for item in unmarks:
            kind = item.get("kind")
            if kind not in ROLE_OF:
                raise ValueError("未知节点类型：%r" % kind)
            chapter, el = _resolve(book, item["key"])
            _clear_semantic(el, kind)
            summary.append("取消 %s 的 %s 语义"
                           % ("#" + (el.get("id") or el.name), kind))

        def rkey(k):
            return remap.get(k, k)

        # 3) 连线（拒绝跨书目；未声明引用自动补 noteref/包裹为锚点；自动生成稳定 id）
        for lk in links:
            rchapter, a = _resolve(book, rkey(lk["ref_key"]))
            nchapter, note = _resolve(book, lk["note_key"])
            a = _as_anchor(book, rchapter, a, lk["ref_key"], remap)
            ntype = _own_note_type(note)
            if not ntype:
                raise ValueError("目标「%s」未声明为脚注/尾注，不能连线"
                                 % note.get_text(" ", strip=True)[:12])
            href = lk.get("href") or ""
            if _is_external(href):
                raise ValueError("跨书目链接 %s 被阻止；注释连线必须指向本书内节点"
                                 % href)
            spi = book.spine.index(nchapter) + 1
            base = ("en" if ntype == "endnote" else "fn") + str(spi)
            nid = _ensure_id(book.soup(nchapter), note, base)
            a["href"] = _relative_href(rchapter, nchapter, nid)
            summary.append("连线注号「%s」→ %s #%s"
                           % (a.get_text(" ", strip=True)[:8], ntype, nid))

        # 4) 回程目标：为每次引用分别建立/移除回链
        for bk in backlinks:
            rchapter, ref = _resolve(book, rkey(bk["ref_key"]))
            nchapter, note = _resolve(book, bk["note_key"])
            ref = _as_anchor(book, rchapter, ref, bk["ref_key"], remap)
            spi = book.spine.index(rchapter) + 1
            rid = _ensure_id(book.soup(rchapter), ref,
                             "ref-ch%d-auto" % spi)
            existing = _find_backlink(book, nchapter, note, rchapter, rid)
            if bk.get("remove"):
                if existing is not None:
                    _remove_backlink(existing)
                    summary.append("移除注释 → #%s 的回程链接" % rid)
                continue
            if existing is not None:
                continue
            _append_backlink(book, nchapter, note, rchapter, rid,
                             bk.get("label") or "↩ 返回引用处")
            summary.append("为引用 #%s 建立独立回程目标" % rid)

        # 5) 批量套用（服务端重算，只接受编号连续且关系唯一的组）
        if edits.get("batch_group"):
            summary.extend(_apply_batch(book, edits["batch_group"], remap))

        for h in affected:
            book.mark_dirty(h)

        # 6) 守恒校验：本次提交新形成的环路 / 新引入的跨书目链接一律拒绝
        #    （提交前已存在的问题仍由问题报告列出，不阻止无关修复）
        g = _guard_graph(book, before_graph)
        return summary, sorted(affected)
    except BaseException:
        for h, snap in snapshots.items():
            _restore_doc(book, h, snap)
        raise


def _incoming_sources(book, chapter, note_el):
    """当前文档树中仍指向该注释（元素自身或其后代 id）的引用描述。"""
    targets = set()
    if note_el.get("id"):
        targets.add(note_el["id"])
    for d in note_el.find_all(True):
        if d.get("id"):
            targets.add(d["id"])
    if not targets:
        return []
    sources = []
    for href in book.spine:
        if href not in book.raw:
            continue
        for a in book.soup(href).find_all("a", href=True):
            h = a["href"]
            if "#" not in h or _is_external(h):
                continue
            f, frag = h.split("#", 1)
            doc = book.resolve(href, f) if f else href
            if doc == chapter and frag in targets:
                sources.append("%s 的注号「%s」"
                               % (posixpath.basename(href),
                                  a.get_text(" ", strip=True)[:8]))
    return sources


def _find_backlink(book, nchapter, note, ref_doc, ref_id):
    """注释 note 内是否已有回到 ref_doc#ref_id 的回程链接。"""
    for a in note.find_all("a", href=True):
        h = a["href"]
        if "#" not in h:
            continue
        f, frag = h.split("#", 1)
        doc = book.resolve(nchapter, f) if f else nchapter
        if frag == ref_id and doc == ref_doc:
            return a
    return None


def _append_backlink(book, nchapter, note, ref_doc, ref_id, label):
    """在注释末尾为单次引用建立独立回程目标（li 追加链接，块级注包一层 <p>）。"""
    soup = book.soup(nchapter)
    href = _relative_href(nchapter, ref_doc, ref_id)
    a = soup.new_tag("a", href=href)
    a["class"] = "note-backlink"
    a.string = label
    if note.name == "li":
        note.append(" ")
        note.append(a)
    else:
        p = soup.new_tag("p")
        p["class"] = "note-backlink"
        p.append(a)
        note.append(p)
    return a


def _remove_backlink(a):
    wrap = a.find_parent("p", class_="note-backlink")
    if wrap is not None:
        wrap.extract()
    else:
        a.extract()


def _apply_batch(book, group_id, remap):
    """按最新关系图重算批量组并套用：标注双方 + 连线 + 每注一条回程。"""
    g = build_graph(book)
    group = next((x for x in g["batches"] if x["id"] == group_id), None)
    if group is None:
        raise ValueError("批量候选已失效（关系可能已被修改），请刷新后重试")
    if not group["applicable"]:
        raise ValueError("该候选编号不连续或关系不唯一，批量套用被阻止：%s"
                         % "；".join(i["reason"] for i in group["items"]
                                    if not i["applicable"]))
    for it in group["items"]:
        if not it["applicable"]:
            raise ValueError("候选 %s 不满足唯一关系，批量整组中止" % it["number"])
        rchapter, rel = _resolve(book, it["ref_key"])
        nchapter, note = _resolve(book, it["note_key"])
        # 未声明引用可能是 sup/span：包裹为带 noteref 语义的 <a> 并登记 key 重映射
        rel = _as_anchor(book, rchapter, rel, it["ref_key"], remap)
        if not _own_note_type(note):
            _set_semantic(note, it["note_type"])
        spi = book.spine.index(nchapter) + 1
        nid = _ensure_id(book.soup(nchapter), note,
                         ("en" if it["note_type"] == "endnote" else "fn")
                         + "%d-auto" % spi)
        rel["href"] = _relative_href(rchapter, nchapter, nid)
        rsi = book.spine.index(rchapter) + 1
        rid = _ensure_id(book.soup(rchapter), rel, "ref-ch%d-auto" % rsi)
        if _find_backlink(book, nchapter, note, rchapter, rid) is None:
            _append_backlink(book, nchapter, note, rchapter, rid,
                             "↩ 返回引用处")
    return ["批量套用注号 %s–%s（%d 对，含声明/连线/回程）"
            % (group["start"], group["end"], len(group["items"]))]


def preview_note_edits(book, edits):
    """在内存文档树上应用编辑、重算模型后整体回滚，不落盘。

    快照覆盖全部 spine 文档（而不仅是负载里出现的 key），保证 batch_group
    解析出的跨章改动或任何未预料的写入都会被回滚：预览结束后实时文档树与
    预览前逐字一致，随后同组 /note/save 仍在原始 DOM 上执行。"""
    snap_docs = [h for h in book.spine if h in book.raw]
    snaps = {h: str(book.soup(h)) for h in snap_docs}
    dirty_before = set(book.dirty)
    try:
        commit_note_edits(book, edits)
        return note_model_json(book, build_graph(book))
    finally:
        for h, s in snaps.items():
            _restore_doc(book, h, s)
        # 回滚不应把这些文档留在 dirty 集合（预览从未落盘）
        book.dirty.clear()
        book.dirty.update(dirty_before)


def take_snapshot(book):
    """提交前全量快照（撤销时逐字恢复原 ID 与链接）。"""
    return {h: str(book.soup(h)) for h in book.spine if h in book.raw}


def _restore_doc(book, href, snapshot):
    soup = book.soup(href)
    fresh = BeautifulSoup(snapshot, "xml")
    soup.clear()
    for c in list(fresh.contents):
        soup.append(c.extract())
    book.mark_dirty(href)


def restore_note_commit(book, snapshots):
    """撤销：用提交前快照整体还原受影响文档。"""
    for href, snap in snapshots.items():
        if href in book.raw:
            _restore_doc(book, href, snap)
