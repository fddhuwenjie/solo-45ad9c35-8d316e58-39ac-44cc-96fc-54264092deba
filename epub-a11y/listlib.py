"""列表语义修复：候选识别、结构检查、列表工作区的结构化编辑操作。

设计要点：
- 检查端 ch_list_a11y 与候选端 candidates_json 共用 analyze_lists 的同一组发现：
  疑似编号段落（连续 p / 单段 <br> 拼行 / 孤立 li）、空列表、非法嵌套、
  ol start / li value 与可见编号不符、被标题/图片/注释截断的列表、候选层级跳变。
- 编辑端每个操作（组合、纳邻/缩界、缩进/提升、拆开/接续、起始序号等）都是一次
  独立提交：region_commit 在操作前快照受影响的连续兄弟区间并做守恒校验，
  逆操作整体还原区间，因此每次操作都可撤回；区间外 DOM 不动。
- 转换复用原节点（p 直接改名 li、li 原位搬移），节点 id、链接、脚注、行内样式
  随节点保留；_guard_preservation 在提交前做文字/属性守恒校验，可能丢字时拒绝。
"""
import re
import xml.dom.minidom as minidom
from collections import Counter

from bs4 import BeautifulSoup

from epublib import dom_path, find_by_path

LIST_TAGS = ("ul", "ol")
# 允许“纳入相邻节点”的非列表元素（其余类型须显式拒绝）
CONVERTIBLE_TAGS = ("p", "li", "div")
# 可能误截断列表的元素
INTERRUPTER_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "img", "figure", "aside")
MEDIA_TAGS = ("img", "svg", "audio", "video", "table")

# ---------------- 可见编号识别 ----------------
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_number(s):
    """一…十、十一、二十、二十三 → int；无法解析返回 None。"""
    if not s:
        return None
    total, section = 0, 0
    for ch in s:
        if ch in ("十",):
            section = section or 1
            total += section * 10
            section = 0
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        elif ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        else:
            return None
    val = total + section
    return val if val else None


_BULLETS = "•·●○◦▪‣■★☆▸►◊"
# 编号后的首字符须为空白、CJK、结束或引号，避免把 “1.5”“3.14” 误判成编号
_LOOKAHEAD = r"(?=[\s　「『“\"'（(]|[一-鿿]|$)"
MARKER_RE = re.compile(
    r"^[\s　]*"
    r"(?:"
    r"(?P<circ>[①-⑳])"
    r"|(?P<paren>[（(]\s*(?:\d{1,3}|[零〇一两二三四五六七八九十]{1,4})\s*[）)])"
    r"|(?P<cn>[零〇一两二三四五六七八九十百]{1,4})\s*[、.．）)]"
    r"|(?P<num>\d{1,3})\s*[.．、）),]"
    r"|(?P<alpha>[A-Za-z])\s*[.．、）)]"
    r"|(?P<bullet>[" + _BULLETS + r"])"
    r"|(?P<dash>[-*])[\s　]+"
    r")[\s　]*" + _LOOKAHEAD)


def parse_marker(text):
    """从行首文本识别项目符号/编号。返回 {family,kind,raw,number} 或 None。"""
    if not text:
        return None
    m = MARKER_RE.match(text)
    if not m:
        return None
    raw = m.group(0)
    kind = m.lastgroup
    number = None
    if kind == "circ":
        number = ord(m.group("circ")) - 9311  # ①(U+2460) → 1
    elif kind == "paren":
        inner = re.search(r"(?:\d+|[零〇一两二三四五六七八九十]{1,4})",
                          m.group("paren")).group(0)
        number = int(inner) if inner.isdigit() else cn_number(inner)
    elif kind == "cn":
        number = cn_number(m.group("cn"))
    elif kind == "num":
        number = int(m.group("num"))
    elif kind == "alpha":
        number = ord(m.group("alpha").lower()) - 96
    return {"family": "ul" if kind in ("bullet", "dash") else "ol",
            "kind": kind, "raw": raw.strip(), "number": number}


def _indent_em(el):
    """从行内样式读取左缩进（em）；margin/padding-left 或 text-indent。"""
    st = el.get("style") or ""
    m = re.search(r"(?:margin|padding)-left\s*:\s*([\d.]+)\s*(em|rem|px)", st)
    if not m:
        m = re.search(r"text-indent\s*:\s*([\d.]+)\s*(em|rem|px)", st)
    if not m:
        return 0.0
    val = float(m.group(1))
    return val / 16.0 if m.group(2) == "px" else val


def _is_block(el):
    return getattr(el, "name", None) is not None


def _direct(el, names=None):
    out = []
    for ch in el.find_all(recursive=False):
        if not _is_block(ch):
            continue
        if names is None or ch.name in names:
            out.append(ch)
    return out


def _ol_start(list_el):
    v = (list_el.get("start") or "").strip()
    if v == "":
        return 1, True
    try:
        return int(v), True
    except ValueError:
        return 1, False


def _li_value(li):
    v = (li.get("value") or "").strip()
    if v == "":
        return None, True
    try:
        return int(v), True
    except ValueError:
        return None, False


# ---------------- 结构分析（检查与候选共用） ----------------
def _finding(href, path, severity, message, candidate=None):
    f = {"chapter": href, "path": path, "check": "list_a11y",
         "severity": severity, "message": message}
    if candidate:
        f["candidate"] = candidate
    return f


def _marker_sequence_ok(marks):
    """有序候选：编号必须可解析且逐项 +1（无序候选只要同族）。"""
    fams = {m["family"] for m in marks}
    if len(fams) != 1:
        return False
    if fams.pop() == "ul":
        return True
    nums = [m["number"] for m in marks]
    if any(n is None for n in nums):
        return False
    return all(b == a + 1 for a, b in zip(nums, nums[1:]))


def _check_existing_list(href, lst):
    """对一个既有 ul/ol 的结构与编号检查（nav 中的目录列表由调用方排除）。"""
    out = []
    lp = dom_path(lst)
    lis = _direct(lst, ("li",))

    if not lst.find_all("li"):
        out.append(_finding(href, lp, "warning",
                            "空列表 <%s> 不含任何 <li>，读屏会播报一个空列表" % lst.name))

    for ch in _direct(lst):
        if ch.name in LIST_TAGS:
            out.append(_finding(
                href, dom_path(ch), "error",
                "<%s> 直接嵌套在 <%s> 中（非法嵌套）：子列表必须包在某个 <li> 内，"
                "可在列表工作区一键归整" % (ch.name, lst.name)))
        elif ch.name != "li":
            out.append(_finding(
                href, dom_path(ch), "error",
                "<%s> 的直接子元素只能是 <li>，却出现 <%s>（非法嵌套），"
                "读屏对该项的项目边界判断会出错" % (lst.name, ch.name)))

    if lst.name == "ol":
        start, ok = _ol_start(lst)
        if not ok:
            out.append(_finding(href, lp, "error",
                                "<ol start=\"%s\"> 的 start 不是整数，起始序号无效"
                                % lst.get("start")))
        running = start
        for li in lis:
            val, vok = _li_value(li)
            if not vok:
                out.append(_finding(href, dom_path(li), "error",
                                    "<li value=\"%s\"> 的 value 不是整数" % li.get("value")))
            if val is not None:
                running = val
            mk = parse_marker(li.get_text(strip=True) or "")
            if mk and mk["number"] is not None and mk["number"] != running:
                if val is not None:
                    out.append(_finding(
                        href, dom_path(li), "warning",
                        "可见编号「%s」与 li value=%d 不符（实际按 %d 播报）"
                        % (mk["raw"], val, running)))
                else:
                    out.append(_finding(
                        href, dom_path(li), "warning",
                        "可见编号「%s」与有序列表实际序号 %d 不符（ol start=%s）"
                        % (mk["raw"], running, lst.get("start") or "1")))
            running += 1

    # 被后续兄弟误截断：同类列表直接相邻，或仅隔一个标题/图片/注释
    nxt = lst.find_next_sibling(True)
    if nxt is not None:
        blocker, second = None, None
        if nxt.name == lst.name:
            second = nxt
        elif nxt.name in INTERRUPTER_TAGS:
            n2 = nxt.find_next_sibling(True)
            if n2 is not None and n2.name == lst.name:
                blocker, second = nxt, n2
        if second is not None:
            n2_count = len(second.find_all("li"))
            if blocker is None:
                out.append(_finding(
                    href, lp, "warning",
                    "两个相邻的 <%s> 列表被拆成两段，应接续为一个列表（%d+%d 项）"
                    % (lst.name, len(lst.find_all("li")), n2_count)))
            else:
                if blocker.name.startswith("h"):
                    kind = "标题"
                elif blocker.name in ("img", "figure"):
                    kind = "图片"
                else:
                    kind = "注释/侧栏"
                label = blocker.get_text(strip=True)[:20] or blocker.name
                out.append(_finding(
                    href, lp, "warning",
                    "列表被%s「%s」截断后仍以 <%s> 接续（疑似误截断）："
                    "可在列表工作区把%s并入相邻项后接续列表"
                    % (kind, label, lst.name, kind)))
    return out


def _candidate_from_p_run(href, p_els, blocks=()):
    """把一段连续（或仅隔一个拦截元素）的带编号段落整理成候选发现。"""
    marks = [parse_marker(p.get_text(strip=True) or "") for p in p_els]
    if not _marker_sequence_ok([m for m in marks if m]):
        return []
    family = marks[0]["family"]
    numbers = [m["number"] for m in marks if m["number"] is not None]
    start = numbers[0] if family == "ol" and numbers else 1

    inds = [_indent_em(p) for p in p_els]
    positive = sorted({v for v in inds if v > 0.05})
    levels = [0] * len(p_els)
    jumps = []
    if positive:
        diffs = [b - a for a, b in zip(positive, positive[1:]) if b - a > 0.05]
        step = min([positive[0]] + diffs) if (positive[0] > 0.05 or diffs) else 1.0
        if step <= 0.05:
            step = 1.0
        levels = [int(round(v / step)) if v > 0.05 else 0 for v in inds]
        for i in range(1, len(levels)):
            if abs(levels[i] - levels[i - 1]) > 1:
                jumps.append(i)

    raws = [m["raw"] for m in marks if m]
    if family == "ol":
        reason = "连续 %d 个段落以编号（%s…）开头但未使用 <ol>" % (
            len(p_els), "、".join(raws[:3]))
    else:
        reason = "连续 %d 个段落以项目符号（%s…）开头但未使用 <ul>" % (
            len(p_els), "、".join(raws[:3]))
    if blocks:
        b = blocks[0]
        reason += "；中间被<%s>「%s」截断，编号仍延续" % (
            b.name, b.get_text(strip=True)[:12] or b.name)

    cand = {
        "suggested": family, "start": start,
        "item_paths": [dom_path(p) for p in p_els],
        "item_texts": [p.get_text(" ", strip=True)[:40] for p in p_els],
        "markers": [m["raw"] if m else None for m in marks],
        "levels": levels,
        "blocks": [{"path": dom_path(b), "tag": b.name,
                    "text": b.get_text(" ", strip=True)[:20],
                    "placement": "into_prev"} for b in blocks],
        "interrupted": bool(blocks),
        "reason": reason,
    }
    out = [_finding(href, dom_path(p_els[0]), "warning",
                    "疑似列表：" + reason + "，读屏只能读出带符号的普通段落",
                    candidate=cand)]
    for i in jumps:
        out.append(_finding(
            href, dom_path(p_els[i]), "warning",
            "疑似列表层级跳变：第 %d 项「%s」缩进从 L%d 跳到 L%d，"
            "应使用列表嵌套层级而非仅靠缩进"
            % (i + 1, p_els[i].get_text(strip=True)[:12],
               levels[i - 1], levels[i])))
    return out


def _br_fragments(p):
    """按 <br> 把 p 的内容切成片段：[(节点组, 纯文本)]，纯空白片段文本为空串。"""
    frags = [[]]
    for node in p.contents:
        if getattr(node, "name", None) == "br":
            frags.append([])
        else:
            frags[-1].append(node)
    out = []
    for nodes in frags:
        text = "".join(getattr(n, "get_text", lambda: str(n))() for n in nodes).strip()
        out.append((nodes, text))
    return out


def analyze_lists(book, href):
    """扫描一章，返回全部列表语义发现（结构问题 + 候选 + 层级跳变）。"""
    soup = book.soup(href)
    body = soup.find("body")
    findings = []
    if body is None:
        return findings

    for lst in soup.find_all(["ul", "ol"]):
        if lst.find_parent("nav") is not None or href == book.nav_path:
            continue
        findings.extend(_check_existing_list(href, lst))

    # 在每个块容器的直接子元素序列上识别候选段落 / 孤立 li
    for parent in body.find_all(True):
        if parent.name in ("ul", "ol", "nav") or parent.find_parent("nav"):
            continue
        kids = _direct(parent)
        i = 0
        while i < len(kids):
            el = kids[i]

            # 孤立 li 连续序列
            if el.name == "li" and el.parent.name not in LIST_TAGS:
                run = []
                while i < len(kids) and kids[i].name == "li" \
                        and kids[i].parent.name not in LIST_TAGS:
                    run.append(kids[i])
                    i += 1
                mk0 = parse_marker(run[0].get_text(strip=True) or "")
                sug = mk0["family"] if mk0 else "ul"
                cand = {
                    "suggested": sug,
                    "start": mk0["number"] if mk0 and mk0["number"] else 1,
                    "item_paths": [dom_path(x) for x in run],
                    "item_texts": [x.get_text(" ", strip=True)[:40] for x in run],
                    "markers": ([mk0["raw"]] if mk0 else [None])
                    + [None] * (len(run) - 1),
                    "levels": [0] * len(run), "blocks": [], "interrupted": False,
                    "reason": "%d 个孤立 <li> 未包在 ul/ol 中，读屏读不出项目边界"
                              % len(run)}
                findings.append(_finding(
                    href, dom_path(run[0]), "error",
                    "孤立的 <li>（未包在 ul/ol 中）连续 %d 项：%s"
                    % (len(run), run[0].get_text(strip=True)[:20]),
                    candidate=cand))
                continue

            # 单段内 <br> 拼出的编号行
            if el.name == "p" and not el.find_parent(LIST_TAGS) and el.find("br"):
                frags = _br_fragments(el)
                marked = [(text, parse_marker(text))
                          for _, text in frags if parse_marker(text)]
                if len(marked) >= 2 and _marker_sequence_ok([m for _, m in marked]):
                    fam = marked[0][1]["family"]
                    nums = [m["number"] for _, m in marked if m["number"] is not None]
                    cand = {
                        "suggested": fam, "start": nums[0] if nums else 1,
                        "item_paths": [dom_path(el)],
                        "item_texts": [el.get_text(" ", strip=True)[:40]],
                        "markers": [marked[0][1]["raw"]],
                        "levels": [0], "blocks": [], "interrupted": False,
                        "split_br": True,
                        "fragments": [t for _, t in frags],
                        "reason": "单段内用 <br> 拼出 %d 个编号行，应拆为 <%s> 的多个 <li>"
                                  % (len(marked), fam)}
                    findings.append(_finding(
                        href, dom_path(el), "warning",
                        "单段内用 <br> 拼出 %d 行编号（%s…），读屏无法识别项目边界"
                        % (len(marked),
                           "、".join(m["raw"] for _, m in marked[:3])),
                        candidate=cand))

            # 连续带编号段落（允许中间夹一个或连续多个标题/图片/注释且编号 +1 延续）
            if el.name == "p" and not el.find_parent(LIST_TAGS) \
                    and parse_marker(el.get_text(strip=True) or ""):
                run = [el]
                i += 1
                blocks = []
                while i < len(kids):
                    nxt = kids[i]
                    m = parse_marker(nxt.get_text(strip=True) or "") \
                        if nxt.name == "p" else None
                    if m:
                        # 同族即延续；有序还要求编号 +1，否则在此断开
                        last_m = parse_marker(run[-1].get_text(strip=True) or "")
                        if m["family"] == last_m["family"] and (
                                m["number"] is None or last_m["number"] is None
                                or m["number"] == last_m["number"] + 1):
                            run.append(nxt)
                            i += 1
                            continue
                        break
                    if nxt.name in INTERRUPTER_TAGS:
                        j = i
                        gap = []
                        while j < len(kids) and kids[j].name in INTERRUPTER_TAGS:
                            gap.append(kids[j])
                            j += 1
                        if j < len(kids) and kids[j].name == "p":
                            m_then = parse_marker(kids[j].get_text(strip=True) or "")
                            m_now = parse_marker(run[-1].get_text(strip=True) or "")
                            if m_then and m_now \
                                    and m_then["family"] == m_now["family"] \
                                    and (m_now["number"] is None
                                         or m_then["number"] == m_now["number"] + 1):
                                blocks.extend(gap)
                                run.append(kids[j])
                                i = j + 1
                                continue
                    break
                if len(run) >= 2:
                    findings.extend(_candidate_from_p_run(href, run, blocks))
                continue
            i += 1
    return findings


def ch_list_a11y(book, href):
    return analyze_lists(book, href)


def candidates_json(book, href):
    """候选与既有列表路径，供预览框选高亮与问题面板跳转工作区。"""
    cands = []
    for f in analyze_lists(book, href):
        c = f.get("candidate")
        if not c:
            continue
        c = dict(c)
        c["id"] = f["path"]
        c["chapter"] = href
        c["message"] = f["message"]
        c["severity"] = f["severity"]
        cands.append(c)
    lists = []
    for lst in book.soup(href).find_all(["ul", "ol"]):
        if lst.find_parent("nav") is None and href != book.nav_path:
            lists.append(dom_path(lst))
    return {"candidates": cands, "lists": lists}


# ---------------- 容错列表树（结构归整 / 模型 / 朗读共用） ----------------
def _read_tree(lst):
    """读取为树：每个节点 {el, groups}；group = {el(子列表), type, nodes}。
    非法直接子列表挂到上一项的末尾分组（无上一项则提升其项到本层）；
    非法块元素保留为 el.name != 'li' 的节点（归整时改名为 li）。"""
    nodes = []
    for ch in _direct(lst):
        if ch.name == "li":
            groups = [{"el": sub, "type": sub.name, "nodes": _read_tree(sub)}
                      for sub in ch.find_all(["ul", "ol"], recursive=False)]
            nodes.append({"el": ch, "groups": groups})
        elif ch.name in LIST_TAGS:
            sub_nodes = _read_tree(ch)
            if nodes:
                nodes[-1]["groups"].append(
                    {"el": ch, "type": ch.name, "nodes": sub_nodes})
            else:
                nodes.extend(sub_nodes)
        else:
            nodes.append({"el": ch, "groups": []})
    return nodes


def _item_json(el, depth, ltype, running, illegal):
    text = el.get_text(" ", strip=True)
    mk = parse_marker(text or "")
    val, vok = _li_value(el)
    return {
        "path": dom_path(el), "depth": depth,
        "text": text[:60], "illegal": illegal,
        "marker": mk["raw"] if mk else None,
        "visible_number": mk["number"] if mk else None,
        "value": val if vok and val is not None else None,
        "ordinal": running,
        "has_id": bool(el.get("id")), "el_id": el.get("id"),
        "has_link": bool(el.find("a", href=True)),
        "has_note": el.get("epub:type") is not None
                    or bool(el.find(attrs={"epub:type": True})),
        "has_style": bool(el.get("style")), "style": el.get("style"),
    }


def _collect_items(nodes, depth, ltype, start):
    out, run = [], start
    for node in nodes:
        el = node["el"]
        illegal = el.name != "li"
        cur = run
        if ltype == "ol":
            val, ok = _li_value(el)
            if ok and val is not None:
                run = val
            cur = run
            run += 1
        out.append(_item_json(el, depth, ltype, cur if ltype == "ol" else None,
                              illegal))
        for g in node["groups"]:
            gstart, _ = _ol_start(g["el"])
            out.extend(_collect_items(g["nodes"], depth + 1, g["type"], gstart))
    return out


def _tree_illegal(lst, tree):
    for sub in lst.find_all(["ul", "ol"]):
        if sub is lst:
            continue
        if sub.parent.name in LIST_TAGS:
            return True
    return any(n["el"].name != "li" for n in tree)


def list_reading(lst, tree=None):
    """模拟屏幕阅读器朗读：列表项数、序号、嵌套层级。"""
    if tree is None:
        tree = _read_tree(lst)
    lines = []

    def walk(nodes, ltype, start, depth):
        if not nodes:
            return
        pad = "　" * depth
        lines.append("%s%s列表，共 %d 项%s："
                     % (pad, "有序" if ltype == "ol" else "无序", len(nodes),
                        "，从 %d 开始" % start if ltype == "ol" else ""))
        run = start
        for node in nodes:
            label = node["el"].get_text(" ", strip=True)[:40] or "（空）"
            if ltype == "ol":
                val, ok = _li_value(node["el"])
                if ok and val is not None:
                    run = val
                lines.append("%s第 %d 项：%s" % (pad, run, label))
                run += 1
            else:
                lines.append("%s• %s" % (pad, label))
            for g in node["groups"]:
                gstart, _ = _ol_start(g["el"])
                walk(g["nodes"], g["type"], gstart, depth + 1)

    start, _ = _ol_start(lst)
    walk(tree, lst.name, start, 0)
    return "\n".join(lines)


def _neighbor_info(el, list_type):
    if el is None:
        return None
    info = {"path": dom_path(el), "tag": el.name,
            "text": el.get_text(" ", strip=True)[:30]}
    if el.name == list_type:
        info["action"] = "join"
        info["count"] = len(_direct(el, ("li",)))
    elif el.name in CONVERTIBLE_TAGS and not (
            el.name == "li" and el.parent.name in LIST_TAGS):
        info["action"] = "extend"
    elif el.name in INTERRUPTER_TAGS:
        info["action"] = "blocked"
    else:
        info["action"] = "none"
    return info


def list_model_json(book, href, list_path):
    soup = book.soup(href)
    lst = find_by_path(soup, list_path)
    if lst is None or lst.name not in LIST_TAGS:
        raise ValueError("列表不存在: %s" % list_path)
    tree = _read_tree(lst)
    start, _ = _ol_start(lst)
    return {
        "path": dom_path(lst), "type": lst.name,
        "start": start if lst.name == "ol" else None,
        "start_raw": lst.get("start"),
        "items": _collect_items(tree, 0, lst.name, start),
        "reading": list_reading(lst, tree),
        "issues": [f for f in analyze_lists(book, href)
                   if f["path"] == dom_path(lst)
                   or f["path"].startswith(dom_path(lst) + "/")],
        "boundaries": {
            "prev": _neighbor_info(lst.find_previous_sibling(True), lst.name),
            "next": _neighbor_info(lst.find_next_sibling(True), lst.name)},
        "empty": not lst.find_all("li"),
        "illegal": _tree_illegal(lst, tree),
    }


# ---------------- 提交守恒校验与区间快照 ----------------
def _element_index(parent, el):
    return _direct(parent).index(el)


def _ensure_consecutive(parent, els):
    if not els:
        return 0
    if any(e.parent is not parent for e in els):
        raise ValueError("节点不属于同一父节点（交叉嵌套/跨章节），提交被拒绝；位置：%s"
                         % dom_path(els[0]))
    kids = _direct(parent)
    idx = [kids.index(e) for e in els]
    if idx != list(range(idx[0], idx[0] + len(idx))):
        raise ValueError("节点不是连续兄弟，提交被拒绝；位置：%s" % dom_path(els[0]))
    return idx[0]


def _all_ids(el):
    ids = [el.get("id")] if el.get("id") else []
    ids.extend(x.get("id") for x in el.find_all(True) if x.get("id"))
    return ids


def _guard_preservation(before_els, after_els, stripped=()):
    """文字/关键属性守恒：丢字、丢 id、丢链接、丢脚注、丢行内样式一律拒绝。"""
    def chars(els):
        parts = [e.get_text() for e in els]
        parts.extend(img.get("alt") or "" for e in els
                     for img in e.find_all("img"))
        return Counter(re.sub(r"\s+", "", "".join(parts)))

    missing = chars(before_els) - (chars(after_els)
                                   + Counter(re.sub(r"\s+", "", "".join(stripped))))
    if missing:
        raise ValueError(
            "转换可能丢失文字（%s…），提交被拒绝；位置：%s"
            % ("".join(list(missing)[:10]), dom_path(before_els[0])))

    id_b, id_a = [], []
    for e in before_els:
        id_b.extend(_all_ids(e))
    for e in after_els:
        id_a.extend(_all_ids(e))
    cb, ca = Counter(id_b), Counter(id_a)
    lost = cb - ca
    if lost:
        raise ValueError("转换可能丢失节点 id（%s），提交被拒绝；位置：%s"
                         % (",".join(sorted(lost)), dom_path(before_els[0])))

    def hrefs(els):
        return Counter(a.get("href") for e in els
                       for a in e.find_all("a", href=True))

    lost_h = hrefs(before_els) - hrefs(after_els)
    if lost_h:
        raise ValueError("转换可能丢失链接（%s），提交被拒绝；位置：%s"
                         % (",".join(sorted(lost_h)), dom_path(before_els[0])))

    def notes(els):
        return Counter(x.get("epub:type") or "" for e in els
                       for x in e.find_all(attrs={"epub:type": True}))

    lost_n = notes(before_els) - notes(after_els)
    if lost_n:
        raise ValueError("转换可能丢失脚注/语义标记（epub:type=%s），提交被拒绝；位置：%s"
                         % (",".join(sorted(lost_n)), dom_path(before_els[0])))

    def styles(els):
        return Counter(x.get("style") for e in els
                       for x in e.find_all(style=True))

    lost_s = styles(before_els) - styles(after_els)
    if lost_s:
        raise ValueError("转换可能丢失行内样式，提交被拒绝；位置：%s"
                         % dom_path(before_els[0]))


def region_commit(book, href, parent, before_els, after_els, stripped, before_html):
    """校验守恒并构造逆操作。

    before_els 仅用于操作前的连续/同父校验（调用方须在改动前完成）；
    此处的守恒比对基于 before_html 解析出的游离快照，因为 before_els 此刻
    可能已被改名（p→li）或搬入新列表。"""
    if after_els:
        _ensure_consecutive(parent, after_els)
    shadows = []
    for html in before_html:
        frag = BeautifulSoup(html, "xml")
        el = next((c for c in frag.contents if _is_block(c)), None)
        if el is not None:
            shadows.append(el)
    _guard_preservation(shadows, after_els, stripped)
    return {
        "kind": "list", "chapter": href,
        "parent_path": dom_path(parent),
        "index0": _element_index(parent, after_els[0]) if after_els
        else (len(_direct(parent))),
        "after_paths": [dom_path(e) for e in after_els],
        "before_html": before_html,
        "affected": ["list_a11y", "heading_hierarchy", "duplicate_id"],
    }


def restore_region(book, href, parent_path, index0, after_paths, before_html):
    """撤销：用 before_html 整体替换当前连续区间。"""
    soup = book.soup(href)
    parent = find_by_path(soup, parent_path) or soup.find("body")
    after_els = []
    for p in after_paths:
        el = find_by_path(soup, p)
        if el is None:
            raise ValueError("撤销定位失败: %s" % p)
        after_els.append(el)
    anchor = None
    insert_pos = None  # 仍挂在树上的定位节点（先于任何 extract 捕获）
    if after_els:
        _ensure_consecutive(parent, after_els)
        anchor = after_els[0]
        insert_pos = anchor.find_previous_sibling(True)
    for el in after_els:
        el.extract()
    new_els = []
    for html in before_html:
        frag = BeautifulSoup(html, "xml")
        el = next((c for c in frag.contents if _is_block(c)), None)
        if el is not None:
            new_els.append(el)
    if anchor is not None:
        # anchor 已被摘出：用它原来的前一个兄弟定位，前一个兄弟也没了则插到段首
        if insert_pos is not None and insert_pos.parent is parent:
            cursor = insert_pos
            for el in new_els:
                cursor.insert_after(el)
                cursor = el
        else:
            for el in reversed(new_els):
                parent.insert(0, el)
    else:
        kids = _direct(parent)
        if index0 < len(kids):
            for el in reversed(new_els):
                kids[index0].insert_before(el)
        else:
            for el in new_els:
                parent.append(el)
    book.mark_dirty(href)


def pretty_region(els):
    """区间序列化为缩进文本，供左右对照。"""
    html = "".join(str(e) for e in els)
    wrapped = ('<root xmlns="http://www.w3.org/1999/xhtml" '
               'xmlns:epub="http://www.idpf.org/2007/ops">%s</root>' % html)
    try:
        dom = minidom.parseString(wrapped)
        lines = [l for l in dom.toprettyxml(indent="  ").splitlines()
                 if l.strip() and not l.lstrip().startswith("<?xml")
                 and not l.lstrip().startswith("<root")
                 and l.strip() != "</root>"]
        return "\n".join(lines)
    except Exception:
        return html


# ---------------- 行首编号剥离 ----------------
def strip_leading_marker(el):
    """剥离元素行首的项目符号/编号，返回被剥离文本（供守恒校验）。"""
    text = el.get_text(strip=True) or ""
    m = MARKER_RE.match(text)
    if not m:
        return ""
    remain = len(m.group(0))
    for node in list(el.find_all(string=True)):
        if remain <= 0:
            break
        s = str(node)
        lead = len(s) - len(s.lstrip())
        body = s[lead:]
        if not body:
            continue
        take = min(len(body), remain)
        node.replace_with(s[:lead] + body[take:])
        remain -= take
    return m.group(0)


# ---------------- 组合为列表 ----------------
def _resolve_paths(soup, paths):
    els = []
    for p in paths:
        el = find_by_path(soup, p)
        if el is None:
            raise ValueError("路径在本章中不存在（选择可能跨越了章节）：%s" % p)
        els.append(el)
    return els


def _require_nonempty(li, where):
    if not (li.get_text(strip=True) or li.find(MEDIA_TAGS)):
        raise ValueError("生成的列表项为空，可能丢字，提交被拒绝；位置：%s" % where)


def _ensure_consecutive_selection(soup, parent, chosen):
    """chosen 必须恰好覆盖父节点下一段连续块级兄弟，否则拒绝。"""
    for e in chosen:
        if e.parent is not parent:
            raise ValueError(
                "「%s」与其他所选项不在同一父节点（交叉嵌套），提交被拒绝；位置：%s"
                % (e.name, dom_path(e)))
        if e.find_parent("nav"):
            raise ValueError("导航文档中的列表无需校修；位置：%s" % dom_path(e))
    kids = _direct(parent)
    idx = sorted(kids.index(e) for e in chosen)
    if idx != list(range(idx[0], idx[0] + len(idx))):
        gap = kids[idx[0] + 1]
        raise ValueError(
            "选择区间内还有未指定的 <%s>「%s」，提交会产生交叉嵌套，已拒绝；"
            "请把该节点也纳入选择或调整边界（位置：%s）"
            % (gap.name, gap.get_text(strip=True)[:12], dom_path(gap)))


def _p_to_lis(soup, e, strip_markers, stripped):
    """p → li：单段 <br> 多行编号则拆成多个 li，否则整体改名。复用原节点。"""
    frags = _br_fragments(e) if e.find("br") else None
    multi = [fr for fr in frags if parse_marker(fr[1])] if frags else []
    if not frags or len(multi) < 2:
        e.name = "li"
        if strip_markers:
            stripped.append(strip_leading_marker(e))
        _require_nonempty(e, dom_path(e))
        return [e]

    src = e
    lis, cur = [], None
    for nodes, text in frags:
        if parse_marker(text) or (cur is None and text):
            if cur is None:
                # 复用原 p（id/样式/epub:type 随之保留），清空后装入第一行
                cur = src
                cur.name = "li"
                for c in list(cur.contents):
                    c.extract()
            else:
                cur = soup.new_tag("li")
                if src.get("style"):
                    cur["style"] = src["style"]
            lis.append(cur)
        if cur.contents and text:
            cur.append(soup.new_tag("br"))
        for n in nodes:
            cur.append(n)
        if parse_marker(text) and strip_markers:
            stripped.append(strip_leading_marker(cur))
    for li in lis:
        _require_nonempty(li, dom_path(src))
    return lis


def create_list(book, href, paths, list_type="ul", start=None,
                strip_markers=True, blocks=None):
    """把同一父节点下连续的 p/孤立 li（可含拦截元素）组合为 ul/ol。"""
    soup = book.soup(href)
    els = _resolve_paths(soup, paths)
    blocks = blocks or {}
    if not els:
        raise ValueError("没有选择任何节点")
    parent = els[0].parent
    block_els = []
    for bp, placement in blocks.items():
        bel = find_by_path(soup, bp)
        if bel is None:
            raise ValueError("拦截元素不存在: %s" % bp)
        if placement not in ("into_prev", "move_before", "move_after"):
            raise ValueError("不支持的处置方式: %s" % placement)
        block_els.append((bel, placement))

    chosen = set(els) | {b for b, _ in block_els}
    _ensure_consecutive_selection(soup, parent, chosen)
    els = sorted(els, key=lambda e: _element_index(parent, e))
    block_els.sort(key=lambda x: _element_index(parent, x[0]))

    before_els = sorted(chosen, key=lambda e: _element_index(parent, e))
    idx0 = _element_index(parent, before_els[0])
    before_html = [str(e) for e in before_els]

    stripped, new_items = [], []
    for e in els:
        if e.name in LIST_TAGS:
            raise ValueError(
                "「%s」已是列表，请在列表工作区使用“接续列表”，不要重复组合；位置：%s"
                % (e.name, dom_path(e)))
        if e.name.startswith("h") or e.name in ("table", "nav", "section", "main",
                                                 "article", "header", "footer"):
            raise ValueError("标题/结构性容器不能作为列表项，提交被拒绝；位置：%s「%s」"
                             % (dom_path(e), e.get_text(strip=True)[:12]))
        if e.name == "li":
            if e.parent.name in LIST_TAGS:
                raise ValueError("「%s」已位于列表中，请直接编辑该列表；位置：%s"
                                 % (e.parent.name, dom_path(e)))
            e.extract()
            if strip_markers:
                stripped.append(strip_leading_marker(e))
            _require_nonempty(e, dom_path(e))
            new_items.append(e)
        elif e.name in CONVERTIBLE_TAGS:
            e.extract()
            new_items.extend(_p_to_lis(soup, e, strip_markers, stripped))
        else:
            raise ValueError("<%s>「%s」不适合作为列表项，提交被拒绝；位置：%s"
                             % (e.name, e.get_text(strip=True)[:12], dom_path(e)))

    lst = soup.new_tag(list_type)
    if list_type == "ol" and start not in (None, "", 1):
        try:
            lst["start"] = str(int(start))
        except (TypeError, ValueError):
            raise ValueError("起始序号必须是整数: %r" % start)
    for li in new_items:
        lst.append(li)

    moved_before, moved_after, absorbed = [], [], 0
    for bel, placement in block_els:
        bel.extract()
        if placement == "into_prev":
            lst.find_all("li", recursive=False)[-1].append(bel)
            absorbed += 1
        elif placement == "move_before":
            moved_before.append(bel)
        else:
            moved_after.append(bel)

    kids = _direct(parent)
    anchor = kids[idx0] if idx0 < len(kids) else None
    for el in moved_before:
        if anchor is not None:
            anchor.insert_before(el)
        else:
            parent.append(el)
    if anchor is not None:
        anchor.insert_before(lst)
    else:
        parent.append(lst)
    cursor = lst
    for el in moved_after:
        cursor.insert_after(el)
        cursor = el

    after_els = list(moved_before) + [lst] + list(moved_after)
    span_set = {e for e in before_els
                if not any(b is e for b, pl in block_els if pl == "into_prev")}
    span = [e for e in before_els if e in span_set]
    span_html = [h for e, h in zip(before_els, before_html) if e in span_set]
    inverse = region_commit(book, href, parent, span, after_els,
                            stripped, span_html)
    book.mark_dirty(href)
    summary = ["组合为 <%s>，含 %d 项" % (list_type, len(new_items))]
    n_markers = len([s for s in stripped if s])
    if n_markers:
        summary.append("剥离行首编号 %d 处" % n_markers)
    if absorbed:
        summary.append("将 %d 个标题/图片/注释并入相邻项" % absorbed)
    if moved_before or moved_after:
        summary.append("外移标题/图片/注释 %d 个"
                       % (len(moved_before) + len(moved_after)))
    return inverse, dom_path(lst), summary


# ---------------- 列表工作区操作 ----------------
def _get_list(soup, path):
    lst = find_by_path(soup, path)
    if lst is None or lst.name not in LIST_TAGS:
        raise ValueError("列表不存在: %s" % path)
    return lst


def _find_li(soup, path):
    el = find_by_path(soup, path or "")
    if el is None or el.name != "li":
        raise ValueError("列表项不存在: %s" % path)
    return el


def apply_op(book, href, path, op, params=None):
    """对既有列表应用一个操作，返回 (inverse, new_path, summary)。"""
    params = params or {}
    soup = book.soup(href)
    lst = _get_list(soup, path)
    parent = lst.parent
    fn = {
        "set_type": _op_set_type, "set_start": _op_set_start,
        "set_value": _op_set_value, "indent": _op_indent,
        "outdent": _op_outdent, "split": _op_split,
        "join": _op_join, "extend": _op_extend, "shrink": _op_shrink,
        "remove_empty": _op_remove_empty, "fix_structure": _op_fix_structure,
    }.get(op)
    if fn is None:
        raise ValueError("未知列表操作: %s" % op)
    before_els, after_els, stripped, summary = fn(book, soup, lst, parent, params)
    # 各 _op_* 在改动前序列化 before_html；before_els 此刻可能已被改名/搬移，
    # 仅保留其列表结构用于逆操作定位，真正守恒比对基于 before_html 的游离快照。
    before_html = before_els if before_els and isinstance(before_els[0], str) \
        else [str(e) for e in before_els]
    inverse = region_commit(book, href, parent, before_els, after_els,
                            stripped, before_html)
    new_path = dom_path(after_els[0]) if after_els else ""
    book.mark_dirty(href)
    return inverse, new_path, summary


def _begin_span(parent, els):
    """操作前：校验区间连续同父，返回序列化快照（供 region_commit 守恒比对）。"""
    _ensure_consecutive(parent, els)
    return [str(e) for e in els]


def _op_set_type(book, soup, lst, parent, params):
    want = params.get("type")
    if want not in LIST_TAGS or want == lst.name:
        raise ValueError("列表类型无效或未变化")
    before_html = _begin_span(parent, [lst])
    if lst.name == "ol":  # ol→ul：start/value 对 ul 无意义（整体快照可撤销）
        if lst.has_attr("start"):
            del lst["start"]
        for li in lst.find_all("li"):
            if li.has_attr("value"):
                del li["value"]
    lst.name = want
    return before_html, [lst], [], ["列表类型 ol→ul（已清除 start/value）"] \
        if want == "ul" else ["列表类型 ul→ol"]


def _op_set_start(book, soup, lst, parent, params):
    if lst.name != "ol":
        raise ValueError("只有有序列表可设置起始序号；可先切换为 <ol>")
    v = params.get("value")
    if v not in (None, "", 1):
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError("起始序号必须是整数: %r" % v)
    old = lst.get("start")
    if v in (None, "", 1):
        if not lst.has_attr("start"):
            raise ValueError("起始序号未变化")
    elif str(v) == old:
        raise ValueError("起始序号未变化")
    before_html = _begin_span(parent, [lst])
    if v in (None, "", 1):
        del lst["start"]
    else:
        lst["start"] = str(v)
    return before_html, [lst], [], ["起始序号 %s→%s"
                                    % (old or "1", v if v not in (None, "") else 1)]


def _op_set_value(book, soup, lst, parent, params):
    li = _find_li(soup, params.get("path"))
    if not _is_in_list(li, lst):
        raise ValueError("列表项不属于该列表: %s" % dom_path(li))
    v = params.get("value")
    if v not in (None, ""):
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError("value 必须是整数: %r" % v)
    old = li.get("value")
    if v in (None, ""):
        if not li.has_attr("value"):
            raise ValueError("该项本来就使用自动序号")
    elif str(v) == old:
        raise ValueError("项目 value 未变化")
    before_html = _begin_span(parent, [lst])
    if v in (None, ""):
        del li["value"]
    else:
        li["value"] = str(v)
    return before_html, [lst], [], [
        "项目 value %s→%s" % (old or "（自动）", v if v not in (None, "") else "（自动）")]


def _is_in_list(li, lst):
    p = li.parent
    while p is not None:
        if p is lst:
            return True
        p = p.parent
    return False


def _op_indent(book, soup, lst, parent, params):
    li = _find_li(soup, params.get("path"))
    if li.parent is not lst:
        raise ValueError("只能缩进本层项目；位置：%s" % dom_path(li))
    prev = li.find_previous_sibling("li")
    if prev is None:
        raise ValueError("首项无法缩进（没有可并入的上一项）；位置：%s" % dom_path(li))
    before_html = _begin_span(parent, [lst])
    li.extract()
    sub = None
    for child in reversed(_direct(prev)):
        if child.name == lst.name:
            sub = child
            break
    if sub is None:
        sub = soup.new_tag(lst.name)
        prev.append(sub)
    sub.append(li)
    return before_html, [lst], [], ["缩进：项目并入上一项的子列表"]


def _op_outdent(book, soup, lst, parent, params):
    li = _find_li(soup, params.get("path"))
    sub = li.parent
    if sub.name not in LIST_TAGS or sub is lst:
        raise ValueError("该项已是顶层项目，无法提升；位置：%s" % dom_path(li))
    gp = sub.parent
    if gp.name != "li":
        raise ValueError("嵌套层级非法，请先“一键归整结构”；位置：%s" % dom_path(li))
    before_html = _begin_span(parent, [lst])
    li.extract()
    gp.insert_after(li)
    if not sub.find_all("li", recursive=False):
        sub.extract()
    return before_html, [lst], [], ["提升：项目移到外层列表"]


def _ordinal_of(lst, li):
    run, _ = _ol_start(lst)
    for x in _direct(lst, ("li",)):
        val, ok = _li_value(x)
        if ok and val is not None:
            run = val
        if x is li:
            return run
        run += 1
    return run


def _op_split(book, soup, lst, parent, params):
    li = _find_li(soup, params.get("path"))
    if li.parent is not lst:
        raise ValueError("只能在本层项目处拆开（嵌套项请先提升）；位置：%s" % dom_path(li))
    lis = _direct(lst, ("li",))
    i = lis.index(li)
    if i == 0:
        raise ValueError("不能在第一项之前拆开列表；位置：%s" % dom_path(li))
    before_html = _begin_span(parent, [lst])
    second = soup.new_tag(lst.name)
    if lst.name == "ol":
        second["start"] = str(_ordinal_of(lst, li))
    li.insert_before(second)
    for x in lis[i:]:
        second.append(x)
    return before_html, [lst, second], [], [
        "拆为两个 <%s>（新列表从 %s 开始）" % (lst.name, second.get("start") or "1")]


def _adopt_blocker(lst, blocker, side, placement):
    """处置夹在列表边界上的标题/图片/注释。
    返回 (仍作为列表外兄弟的元素, 是否被吸收进某个 li)。"""
    if placement not in ("into_prev", "move_before", "move_after"):
        raise ValueError(
            "相邻节点是 <%s>「%s」，不能直接变成列表项；请选择处置方式"
            "（并入相邻项/移到列表前/移到列表后）；位置：%s"
            % (blocker.name, blocker.get_text(strip=True)[:12], dom_path(blocker)))
    blocker.extract()
    if placement == "into_prev":
        if side == "after":
            _direct(lst, ("li",))[-1].append(blocker)
        else:
            first = _direct(lst, ("li",))[0]
            first.insert(0, blocker)
        return [], True
    if placement == "move_before":
        lst.insert_before(blocker)
    else:
        lst.insert_after(blocker)
    return [blocker], False


def _merge_next(soup, dst, src):
    """把 src 的本层 li 并入 dst（二者同类型）；src 中残留结构并入末项。"""
    for li in _direct(src, ("li",)):
        dst.append(li)
    leftover = _direct(src)
    if leftover:
        last_li = dst.find_all("li", recursive=False)
        last = last_li[-1] if last_li else soup.new_tag("li")
        if not last_li:
            dst.append(last)
        for c in leftover:
            last.append(c)
    src.extract()


def _op_join(book, soup, lst, parent, params):
    side = params.get("side", "after")
    placement = params.get("placement")
    target = lst.find_next_sibling(True) if side == "after" \
        else lst.find_previous_sibling(True)
    blockers = []
    if target is not None and target.name != lst.name:
        if target.name in INTERRUPTER_TAGS:
            beyond = target.find_next_sibling(True) if side == "after" \
                else target.find_previous_sibling(True)
            if beyond is not None and beyond.name == lst.name:
                blockers.append(target)
                target = beyond
    if target is None or target.name != lst.name:
        raise ValueError("该方向没有相邻的同类型 <%s> 列表，无法接续；位置：%s"
                         % (lst.name, dom_path(lst)))
    span = [target] + blockers + [lst] if side == "before" \
        else [lst] + blockers + [target]
    span = sorted(set(span), key=lambda e: _element_index(parent, e))
    before_html = _begin_span(parent, span)
    blockers_after, absorbed = [], []
    for blocker in blockers:
        moved, is_absorbed = _adopt_blocker(lst, blocker, side, placement)
        blockers_after.extend(moved)
        if is_absorbed:
            absorbed.append(blocker)
    span_html = [h for e, h in zip(span, before_html) if e not in absorbed]
    if side == "before":
        _merge_next(soup, target, lst)
        head = target
    else:
        _merge_next(soup, lst, target)
        head = lst
    after = sorted(set(blockers_after + [head]),
                   key=lambda e: _element_index(parent, e))
    return span_html, after, [], ["接续相邻 <%s>%s"
                               % (lst.name,
                                  "（并处置截断元素）" if blockers else "")]


def _convert_neighbor(soup, el, stripped):
    """相邻 p/孤立 li → li（p 内 <br> 多行编号会拆分）。"""
    if el.name == "li":
        if el.parent.name in LIST_TAGS:
            raise ValueError("相邻项已属于其他列表，请使用“接续列表”；位置：%s"
                             % dom_path(el))
        el.extract()
        stripped.append(strip_leading_marker(el))
        _require_nonempty(el, dom_path(el))
        return [el]
    el.extract()
    return _p_to_lis(soup, el, True, stripped)


def _op_extend(book, soup, lst, parent, params):
    side = params.get("side", "after")
    placement = params.get("placement")
    neighbor = lst.find_next_sibling(True) if side == "after" \
        else lst.find_previous_sibling(True)
    if neighbor is None:
        raise ValueError("该方向已没有可纳入的节点；位置：%s" % dom_path(lst))
    if neighbor.name == lst.name:
        return _op_join(book, soup, lst, parent, {"side": side})
    if neighbor.name in INTERRUPTER_TAGS:
        before_html_all = _begin_span(parent, [lst, neighbor])
        moved, is_absorbed = _adopt_blocker(lst, neighbor, side, placement)
        span_html = before_html_all if not is_absorbed else before_html_all[:1]
        after = sorted(set(moved + [lst]), key=lambda e: _element_index(parent, e))
        return span_html, after, [], ["把截断元素 <%s> 纳入边界" % neighbor.name]
    if neighbor.name not in CONVERTIBLE_TAGS or (
            neighbor.name == "li" and neighbor.parent.name in LIST_TAGS):
        raise ValueError("相邻 <%s>「%s」不适合作为列表项，已拒绝；位置：%s"
                         % (neighbor.name, neighbor.get_text(strip=True)[:12],
                            dom_path(neighbor)))
    before_html = _begin_span(parent, [lst, neighbor])
    stripped = []
    new_lis = _convert_neighbor(soup, neighbor, stripped)
    if side == "after":
        for li in new_lis:
            lst.append(li)
    else:
        anchor = lst.find_all("li", recursive=False)
        for li in reversed(new_lis):
            if anchor:
                anchor[0].insert_before(li)
            else:
                lst.insert(0, li)
    n_markers = len([s for s in stripped if s])
    return before_html, [lst], stripped, [
        "纳入相邻 <%s>（%d 项%s）"
        % (neighbor.name, len(new_lis),
           "，剥离编号 %d 处" % n_markers if n_markers else "")]


def _op_shrink(book, soup, lst, parent, params):
    side = params.get("side", "after")
    lis = _direct(lst, ("li",))
    if not lis:
        raise ValueError("列表已无项目可缩界；位置：%s" % dom_path(lst))
    li = lis[-1] if side == "after" else lis[0]
    if li.find_all(["ul", "ol"]):
        raise ValueError("该项含子列表，请先提升/移出其子项再缩界；位置：%s"
                         % dom_path(li))
    before_html = _begin_span(parent, [lst])
    li.extract()
    li.name = "p"
    if li.has_attr("value"):
        del li["value"]
    if side == "after":
        lst.insert_after(li)
        after = [lst, li]
    else:
        lst.insert_before(li)
        after = [li, lst]
    return before_html, after, [], (["缩界：末项移出列表成为段落"]
                                    if side == "after"
                                    else ["缩界：首项移出列表成为段落"])


def _op_remove_empty(book, soup, lst, parent, params):
    if lst.find_all("li"):
        raise ValueError("列表非空，不能按“空列表”删除；位置：%s" % dom_path(lst))
    before_html = _begin_span(parent, [lst])
    lst.extract()
    return before_html, [], [], ["删除空列表"]


def _rebuild_item(node, default_type):
    """根据容错树重建 li（复用原节点与原子列表元素）。"""
    el = node["el"]
    if el.name != "li":
        el.name = "li"
    for g in node["groups"]:
        sub = g["el"]
        for c in _direct(sub):
            c.extract()
        for sn in g["nodes"]:
            sub.append(_rebuild_item(sn, g["type"]))
        if sub.parent is not el:  # 原非法直接子列表：挂回该项
            el.append(sub)
    return el


def _op_fix_structure(book, soup, lst, parent, params):
    """归整非法嵌套：直接子列表挂入上一项、非法块元素改为 li，整体重建。"""
    tree = _read_tree(lst)
    if not _tree_illegal(lst, tree):
        raise ValueError("列表结构合法，无需归整；位置：%s" % dom_path(lst))
    before_html = _begin_span(parent, [lst])
    new = soup.new_tag(lst.name)
    for k, v in lst.attrs.items():
        new[k] = v
    for node in tree:
        new.append(_rebuild_item(node, lst.name))
    lst.insert_before(new)
    lst.extract()
    return before_html, [new], [], ["一键归整非法嵌套结构"]
