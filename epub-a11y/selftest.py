"""端到端自测：样例书 → 检查 → 编辑 → 撤销 → 地标同步 → 导出 → 重新打开验证。"""
import io
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from app import app

PASS, FAIL = [], []


def build_multi_h1_epub():
    """构造回归用书：nav 只有整章链接，章内含两个带 id 的 h1。"""
    xhtml_head = ('<?xml version="1.0" encoding="utf-8"?>\n'
                  '<html xmlns="http://www.w3.org/1999/xhtml" '
                  'xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">'
                  '<head><title>%s</title></head>')
    ch1 = xhtml_head % "合集" + """
<body><section epub:type="chapter">
<h1 id="part1">第一部分</h1><p>内容一</p>
<h1 id="part2">第二部分</h1><p>内容二</p>
</section></body></html>"""
    nav = xhtml_head % "目录" + """
<body><nav epub:type="toc"><h1>目录</h1>
<ol><li><a href="ch1.xhtml">合集</a></li></ol>
</nav></body></html>"""
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">multi-h1</dc:identifier>
<dc:title>多 h1 测试书</dc:title><dc:language>zh-CN</dc:language>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="ch1"/></spine>
</package>"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        for name, data in [("META-INF/container.xml", container),
                           ("content.opf", opf), ("nav.xhtml", nav),
                           ("ch1.xhtml", ch1)]:
            z.writestr(name, data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    buf.seek(0)
    return buf


def build_table_book_epub():
    """构造回归用书：表格中 headers 指向 td（无效表头引用）。"""
    xhtml_head = ('<?xml version="1.0" encoding="utf-8"?>\n'
                  '<html xmlns="http://www.w3.org/1999/xhtml" '
                  'xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">'
                  '<head><title>%s</title></head>')
    ch1 = xhtml_head % "表格回归" + """
<body><section epub:type="chapter">
<h1 id="t1">表格回归</h1>
<table>
<caption>成绩表</caption>
<tr><th id="h-name" scope="col">姓名</th><th id="h-score" scope="col">成绩</th></tr>
<tr><td id="d-name">张三</td><td headers="d-name">90</td></tr>
</table>
</section></body></html>"""
    nav = xhtml_head % "目录" + """
<body><nav epub:type="toc"><h1>目录</h1>
<ol><li><a href="ch1.xhtml">表格回归</a></li></ol>
</nav></body></html>"""
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">table-headers-td</dc:identifier>
<dc:title>headers 指向 td 回归书</dc:title><dc:language>zh-CN</dc:language>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="ch1"/></spine>
</package>"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        for name, data in [("META-INF/container.xml", container),
                           ("content.opf", opf), ("nav.xhtml", nav),
                           ("ch1.xhtml", ch1)]:
            z.writestr(name, data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    buf.seek(0)
    return buf


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("  " + str(extra) if extra else ""))


def xhtml_sem(s):
    """归一化 XHTML：去掉标签间空白与属性顺序差异，仅比较元素/属性/文本语义。
    BeautifulSoup 重建会丢原始换行，故撤销复原用语义比较而非逐字节比较。"""
    s = re.sub(r">\s+<", "><", s)
    s = re.sub(r"<base[^>]*/?>", "", s)  # 预览注入的 <base>
    s = re.sub(r"<style>.*?</style>", "", s, flags=re.S)  # 预览注入的高亮样式
    s = re.sub(r"<script>.*?</script>", "", s, flags=re.S)  # 预览注入的框选脚本
    # 自闭合标签与开闭对规范化（<img .../> 与 <img ...>）
    s = re.sub(r"/>", ">", s)
    # 属性排序
    def norm_tag(m):
        tag = m.group(0)
        attrs = sorted(re.findall(r'([\w:.-]+)="([^"]*)"', tag))
        name = re.match(r"<\s*([\w:.-]+)", tag).group(1)
        return "<%s %s>" % (name, " ".join('%s="%s"' % a for a in attrs)) \
            if attrs else "<%s>" % name
    def repl(m):
        if m.group(0).startswith(("</", "<!")):
            return m.group(0)
        return norm_tag(m)

    s = re.sub(r"<[a-zA-Z][^>]*>", repl, s)
    return s


def _build_and_import_body_list_book(c):
    """编号段落直接挂在 <body> 下（无 section 包裹）的回归书。"""
    xhtml_head = ('<?xml version="1.0" encoding="utf-8"?>'
                  '<html xmlns="http://www.w3.org/1999/xhtml" '
                  'xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">'
                  '<head><title>%s</title></head>')
    ch1 = xhtml_head % "body 列表" + """
<body>
<h1 id="t">body 直属列表</h1>
<p>1. 第一项</p>
<p>2. 第二项</p>
<p>3. 第三项</p>
</body></html>"""
    nav = xhtml_head % "目录" + """
<body><nav epub:type="toc"><h1>目录</h1>
<ol><li><a href="ch1.xhtml">body 列表</a></li></ol>
</nav></body></html>"""
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/ops" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">body-list</dc:identifier>
<dc:title>body 直属列表回归书</dc:title><dc:language>zh-CN</dc:language>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="ch1"/></spine>
</package>"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf"/></rootfiles>
</container>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        for name, data in [("META-INF/container.xml", container),
                           ("content.opf", opf), ("nav.xhtml", nav),
                           ("ch1.xhtml", ch1)]:
            z.writestr(name, data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    buf.seek(0)
    r = c.post("/api/import", data={"file": (buf, "bodylist.epub")},
               content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["book_id"]


def main():
    db.init_db()
    c = app.test_client()

    # 1. 载入样例书
    r = c.post("/api/load_sample")
    assert r.status_code == 200, r.get_data(as_text=True)
    bid = r.get_json()["book_id"]
    st = r.get_json()["state"]
    check("载入样例书", bid >= 1)
    check("章节数=5", len(st["chapters"]) == 5)

    issues = st["issues"]
    by = {}
    for it in issues:
        by.setdefault(it["check_name"], []).append(it)
    check("检查:目录次序冲突+漏项>=2", len(by.get("toc", [])) >= 2, len(by.get("toc", [])))
    check("检查:目录漏项包含未收录的 h2「背景」",
          any("背景" in i["message"] for i in by.get("toc", [])))
    check("检查:标题层级跳跃", len(by.get("heading_hierarchy", [])) == 1)
    check("检查:图片缺 alt", len(by.get("img_alt", [])) == 1)
    check("检查:语言标记缺失", len(by.get("lang", [])) == 1)
    check("检查:重复 ID", len(by.get("duplicate_id", [])) == 1)
    check("检查:失效锚点", len(by.get("broken_anchor", [])) == 1)
    check("检查:脚注回链缺失(跨章)", len(by.get("footnote_backlink", [])) == 1)

    ch1 = [x for x in st["chapters"] if "ch1" in x["href"]][0]
    ch2 = [x for x in st["chapters"] if "ch2" in x["href"]][0]
    ch3 = [x for x in st["chapters"] if "ch3" in x["href"]][0]
    ch4 = [x for x in st["chapters"] if "ch4" in x["href"]][0]

    # 2. 节点提取 + html 根语言
    d1 = c.get(f"/api/books/{bid}/chapters/{ch1['id']}/nodes").get_json()
    d2 = c.get(f"/api/books/{bid}/chapters/{ch2['id']}/nodes").get_json()
    nodes1, nodes2 = d1["nodes"], d2["nodes"]
    check("nodes 接口返回 html_lang", d1["html_lang"] == "zh-CN" and d2["html_lang"] is None)
    img_node = [n for n in nodes1 if n["tag"] == "img" and n["alt"] is None]
    check("章1提取到缺 alt 图片节点", len(img_node) == 1)
    check("章1提取到侧栏节点", len([n for n in nodes1 if n["tag"] == "aside"]) == 1)

    # 3. 修改图片 alt → 只重跑 img_alt
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch1["id"], "dom_path": img_node[0]["dom_path"],
        "updates": {"alt": "实验室照片"}})
    d = r.get_json()
    check("修改 alt 成功", r.status_code == 200 and d["ok"])
    check("只重跑 img_alt 检查", d["checks_ran"] == ["img_alt"], d["checks_ran"])
    check("img_alt 问题已消除",
          not any(i["check_name"] == "img_alt" for i in d["state"]["issues"]))

    # 4. 撤销 alt：原本没有 alt 属性 → 撤销后属性应被删除而非置空
    r = c.post(f"/api/books/{bid}/undo")
    d = r.get_json()
    check("撤销 alt 成功", d["ok"])
    check("撤销后 img_alt 问题恢复",
          any(i["check_name"] == "img_alt" for i in d["state"]["issues"]))
    html1 = c.get(f"/api/books/{bid}/preview/ch1.xhtml").get_data(as_text=True)
    check("撤销后 img 无 alt 属性", 'src="images/photo.png"' in html1 and
          'alt=' not in html1.split('src="images/photo.png"')[0][-200:])
    check("变更记录保留(含已撤销)",
          any(ch["undone"] for ch in d["state"]["changes"]))

    # 5. 标题层级修复与撤销
    h3 = [n for n in nodes2 if n["tag"] == "h3"][0]
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch2["id"], "dom_path": h3["dom_path"],
        "updates": {"heading_level": 2}})
    d = r.get_json()
    check("标题层级修复", d["ok"] and
          not any(i["check_name"] == "heading_hierarchy" for i in d["state"]["issues"]))
    r = c.post(f"/api/books/{bid}/undo")
    d = r.get_json()
    check("撤销标题修复后问题重现",
          any(i["check_name"] == "heading_hierarchy" for i in d["state"]["issues"]))
    nodes2 = c.get(f"/api/books/{bid}/chapters/{ch2['id']}/nodes").get_json()["nodes"]
    check("撤销后标签恢复为 h3", any(n["tag"] == "h3" for n in nodes2))
    h3 = [n for n in nodes2 if n["tag"] == "h3"][0]
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch2["id"], "dom_path": h3["dom_path"],
        "updates": {"heading_level": 2}})
    check("重新修复标题层级", r.get_json()["ok"])

    # 6. 修复第二章 <html> 缺 lang（根元素 html[1] 可定位可编辑）
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch2["id"], "dom_path": "html[1]",
        "updates": {"lang": "zh-CN"}})
    d = r.get_json()
    check("补全 html[1] 语言标记", d["ok"] and
          not any(i["check_name"] == "lang" for i in d["state"]["issues"]))
    check("响应携带更新后的 html_lang", d.get("html_lang") == "zh-CN")
    d2 = c.get(f"/api/books/{bid}/chapters/{ch2['id']}/nodes").get_json()
    check("nodes 接口 html_lang 已更新", d2["html_lang"] == "zh-CN")

    # 7. 拖拽移动错序侧栏：章1 aside 移到 h2 之后
    nodes1 = c.get(f"/api/books/{bid}/chapters/{ch1['id']}/nodes").get_json()["nodes"]
    aside = [n for n in nodes1 if n["tag"] == "aside"][0]
    h2 = [n for n in nodes1 if n["tag"] == "h2"][0]
    target = [n for n in nodes1 if n["order_index"] > h2["order_index"]
              and n["parent_path"] == h2["parent_path"]][0]
    r = c.post(f"/api/books/{bid}/node/move", json={
        "chapter_id": ch1["id"], "dom_path": aside["dom_path"],
        "before_path": target["dom_path"]})
    d = r.get_json()
    check("移动侧栏节点", d["ok"])
    ai = [n["order_index"] for n in d["nodes"] if n["tag"] == "aside"][0]
    hi = [n["order_index"] for n in d["nodes"] if n["tag"] == "h2"][0]
    check("侧栏已位于 h2 之后", ai > hi, f"aside={ai} h2={hi}")
    check("移动重跑了 toc/heading/footnote/table/list 检查",
          set(d["checks_ran"]) == {"heading_hierarchy", "toc", "footnote_backlink",
                                   "table_a11y", "list_a11y"})

    # 8. 章节排序：使 spine 与目录一致，然后撤销
    ch5 = [x for x in st["chapters"] if "ch5" in x["href"]]
    ch5 = ch5[0] if ch5 else None
    order = [ch1["id"], ch3["id"], ch2["id"], ch4["id"]]
    if ch5:
        order.append(ch5["id"])
    r = c.post(f"/api/books/{bid}/spine/reorder", json={"order": order})
    d = r.get_json()
    check("spine 重排", d["ok"])
    check("目录次序冲突消除",
          not any(i["check_name"] == "toc" and "冲突" in i["message"]
                  for i in d["state"]["issues"]))
    r = c.post(f"/api/books/{bid}/undo")
    d = r.get_json()
    check("撤销 spine 重排后冲突重现",
          any(i["check_name"] == "toc" and "冲突" in i["message"]
              for i in d["state"]["issues"]))

    # 9. 地标增删 → 同步 nav 的 epub:type="landmarks" 链接
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch1["id"], "dom_path": "html[1]/body[1]/section[1]",
        "updates": {"epub_type": "bodymatter"}})
    d = r.get_json()
    nav_html = c.get(f"/api/books/{bid}/preview/nav.xhtml").get_data(as_text=True)
    check("新增地标后地标面板可见",
          any(l["epub_type"] == "bodymatter" for l in d["state"]["landmarks"]))
    check("nav landmarks 新增链接", 'epub:type="bodymatter"' in nav_html
          and "ch1.xhtml#s1" in nav_html)
    r = c.post(f"/api/books/{bid}/undo")
    d = r.get_json()
    nav_html = c.get(f"/api/books/{bid}/preview/nav.xhtml").get_data(as_text=True)
    check("撤销地标后 nav 链接移除", 'epub:type="bodymatter"' not in nav_html)
    check("撤销后 nav 恢复原 chapter 地标链接", 'epub:type="chapter"' in nav_html)
    # 删除地标（章3 侧栏 sb2）
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch3["id"], "dom_path": "html[1]/body[1]/section[1]/aside[1]",
        "updates": {"epub_type": ""}})
    d = r.get_json()
    nav_html = c.get(f"/api/books/{bid}/preview/nav.xhtml").get_data(as_text=True)
    check("删除地标成功", d["ok"] and
          not any(l["chapter"].endswith("ch3.xhtml") and l["epub_type"] == "sidebar"
                  for l in d["state"]["landmarks"]))
    check("nav landmarks 同步移除被删链接", "ch3.xhtml#sb2" not in nav_html)

    # 9.5 复杂表格无障碍：检查 → 工作区模型 → 预览（不落盘）→ 保存 → 撤销 → 推断冲突
    st = c.get(f"/api/books/{bid}").get_json()
    t_issues = [i for i in st["issues"] if i["check_name"] == "table_a11y"]
    check("表格问题总数=12", len(t_issues) == 12, len(t_issues))
    check("表格:caption 缺失", any("caption" in i["message"] for i in t_issues))
    check("表格:视觉表头仍为 td", any("视觉表头" in i["message"] for i in t_issues))
    check("表格:headers 引用失效", any("不存在的 id" in i["message"] for i in t_issues))
    check("表格:headers 引用循环", any("循环" in i["message"] for i in t_issues))
    check("表格:scope 与合并结构冲突", any("rowgroup" in i["message"] for i in t_issues))
    check("表格:7 格无法关联表头",
          len([i for i in t_issues if "无法关联" in i["message"]]) == 7,
          [i["message"] for i in t_issues if "无法关联" in i["message"]])
    r = c.get(f"/api/books/{bid}/export/report")
    check("问题报告含表格分类", "表格表头与关联" in r.get_data(as_text=True))

    tA = "html[1]/body[1]/section[1]/table[1]"
    tB = "html[1]/body[1]/section[1]/table[2]"
    r = c.get(f"/api/books/{bid}/table/{ch4['id']}", query_string={"path": tA})
    m = r.get_json()
    check("表格模型 4行3列", m["rows"] == 4 and m["cols"] == 3, (m["rows"], m["cols"]))
    cell10 = [x for x in m["cells"] if x["row"] == 1 and x["col"] == 0][0]
    check("合并单元格展开 rowspan=2", cell10["rowspan"] == 2)
    check("首行仍是 td", all(x["tag"] == "td" for x in m["cells"] if x["row"] == 0))
    check("表A 实时问题=10", len(m["issues"]) == 10, len(m["issues"]))

    # 预览：caption + 批量推断 + 人工坚持 (1,0) 为 td（与行表头推断冲突）+ 点选关联
    editsA = {
        "caption": "各地区季度销售额",
        "cells": [{"row": 1, "col": 0, "tag": "td"}],
        "links": [{"row": 3, "col": 2, "headers": [[0, 2]]}],
        "infer": ["col", "row"],
    }
    r = c.post(f"/api/books/{bid}/table/preview",
               json={"chapter_id": ch4["id"], "path": tA, "edits": editsA})
    d = r.get_json()
    pm = d["model"]
    check("预览即见 caption", pm["caption"] == "各地区季度销售额")
    c00 = [x for x in pm["cells"] if x["row"] == 0 and x["col"] == 0][0]
    check("推断列表头: 首行转 th+scope=col", c00["tag"] == "th" and c00["scope"] == "col")
    c30 = [x for x in pm["cells"] if x["row"] == 3 and x["col"] == 0][0]
    check("推断行表头: 首列转 th+scope=row", c30["tag"] == "th" and c30["scope"] == "row")
    check("推断冲突保留人工选择并解释原因",
          any(x["row"] == 2 and x["col"] == 1 and "人工" in x["reason"]
              for x in d["conflicts"]), d["conflicts"])
    c02 = [x for x in pm["cells"] if x["row"] == 0 and x["col"] == 2][0]
    c32 = [x for x in pm["cells"] if x["row"] == 3 and x["col"] == 2][0]
    check("点选关联生成稳定 id/headers",
          bool(c02["id"]) and c32["headers"] == [c02["id"]], (c02["id"], c32["headers"]))
    read12 = [x for x in pm["cells"] if x["row"] == 1 and x["col"] == 2][0]["reading"]
    check("逐格朗读预览含表头上下文", "销售额" in read12 and "120" in read12, read12)
    st_now = c.get(f"/api/books/{bid}").get_json()
    check("预览不落盘（问题仍是 12）",
          len([i for i in st_now["issues"] if i["check_name"] == "table_a11y"]) == 12)

    # 保存：表A 问题清零，只重检该表（表B 的 2 项原样保留），变更记录可查
    r = c.post(f"/api/books/{bid}/table/save",
               json={"chapter_id": ch4["id"], "path": tA, "edits": editsA})
    d = r.get_json()
    check("保存表格成功", d["ok"])
    ta_after = [i for i in d["state"]["issues"] if i["check_name"] == "table_a11y"]
    check("保存后仅剩表B 的 2 项（只重检受影响表格）",
          len(ta_after) == 2 and all(i["node_path"] == tB for i in ta_after),
          [(i["node_path"], i["message"]) for i in ta_after])
    check("变更记录含表格校修", any(ch["kind"] == "table" for ch in d["state"]["changes"]))

    # 撤销：表A 问题重现；再保存验证 id 生成稳定
    r = c.post(f"/api/books/{bid}/undo")
    d = r.get_json()
    ta_undo = [i for i in d["state"]["issues"] if i["check_name"] == "table_a11y"]
    check("撤销表格校修后表A 问题重现", len(ta_undo) == 12, len(ta_undo))
    r = c.post(f"/api/books/{bid}/table/save",
               json={"chapter_id": ch4["id"], "path": tA, "edits": editsA})
    d = r.get_json()
    check("重新保存表格", d["ok"])
    c02 = [x for x in d["model"]["cells"] if x["row"] == 0 and x["col"] == 2][0]
    check("重新保存生成相同稳定 id", c02["id"] == "tbl1-r1c3", c02["id"])

    # 表B：修复 scope 冲突（row→rowgroup）并清除循环 headers
    editsB = {"cells": [{"row": 1, "col": 0, "scope": "rowgroup"}],
              "links": [{"row": 1, "col": 1, "headers": []},
                        {"row": 1, "col": 2, "headers": []}]}
    r = c.post(f"/api/books/{bid}/table/save",
               json={"chapter_id": ch4["id"], "path": tB, "edits": editsB})
    d = r.get_json()
    check("修复表B scope 冲突与循环", d["ok"])
    ta_b = [i for i in d["state"]["issues"] if i["check_name"] == "table_a11y"]
    check("全部表格问题清零", len(ta_b) == 0, [i["message"] for i in ta_b])

    # 10. 预览与资源
    r = c.get(f"/api/books/{bid}/preview/ch1.xhtml")
    check("预览页面可访问", r.status_code == 200 and b"pathOf" in r.data)
    r = c.get(f"/api/books/{bid}/res/images/photo.png")
    check("预览图片资源可访问", r.status_code == 200 and r.data[:4] == b"\x89PNG")

    # 11. 导出 EPUB 并重新打开
    r = c.get(f"/api/books/{bid}/export/epub")
    check("导出 EPUB", r.status_code == 200 and r.data[:2] == b"PK")
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        nav_bytes = z.read("nav.xhtml")
        ch4_bytes = z.read("ch4.xhtml")
        names = z.namelist()
    check("导出包 nav 含 landmarks 导航", b'epub:type="landmarks"' in nav_bytes)
    check("导出包含表格修复（caption+稳定 id+rowgroup）",
          "各地区季度销售额".encode() in ch4_bytes and b"tbl1-r1c3" in ch4_bytes
          and b'scope="rowgroup"' in ch4_bytes)
    check("导出包 mimetype 为首项", names[0] == "mimetype")
    r2 = c.post("/api/import", data={"file": (io.BytesIO(r.data), "fixed.epub")},
                content_type="multipart/form-data")
    check("修正版 EPUB 可重新打开", r2.status_code == 200, r2.get_json().get("error"))
    st2 = r2.get_json()["state"]
    check("重开书籍章节数=5", len(st2["chapters"]) == 5)
    check("重开后表格修复仍生效（表格问题为零）",
          not any(i["check_name"] == "table_a11y" for i in st2["issues"]))
    check("重开后 h3 修复仍生效",
          not any(i["check_name"] == "heading_hierarchy" for i in st2["issues"]))
    check("重开后 lang 修复仍生效",
          not any(i["check_name"] == "lang" for i in st2["issues"]))
    check("重开后未修复项仍在(失效锚点)",
          any(i["check_name"] == "broken_anchor" for i in st2["issues"]))

    # 12. 导出变更记录与报告
    r = c.get(f"/api/books/{bid}/export/changes")
    check("导出变更记录 CSV", r.status_code == 200 and "时间".encode("utf-8-sig") in r.data)
    r = c.get(f"/api/books/{bid}/export/report")
    check("导出问题报告 HTML", r.status_code == 200 and "未解决问题报告".encode() in r.data)

    # 13. 回归：目录只有整章链接、同章含多个 h1
    #     第一个 h1 被整章链接覆盖（不误报），后续 h1 必须报目录漏项
    r = c.post("/api/import", data={"file": (build_multi_h1_epub(), "multi.epub")},
               content_type="multipart/form-data")
    check("多 h1 测试书可导入", r.status_code == 200, r.get_json().get("error"))
    st3 = r.get_json()["state"]
    toc3 = [i for i in st3["issues"] if i["check_name"] == "toc"]
    check("同章第二个 h1 报目录漏项",
          any("第二部分" in i["message"] for i in toc3),
          [i["message"] for i in toc3])
    check("整章链接覆盖的第一个 h1 不误报",
          not any("第一部分" in i["message"] for i in toc3))
    check("多 h1 书仅 1 项目录问题", len(toc3) == 1, len(toc3))
    # 导出的多 h1 书仍可重新导入
    bid3 = r.get_json()["book_id"]
    r = c.get(f"/api/books/{bid3}/export/epub")
    check("多 h1 书可导出", r.status_code == 200 and r.data[:2] == b"PK")
    r = c.post("/api/import", data={"file": (io.BytesIO(r.data), "multi_fixed.epub")},
               content_type="multipart/form-data")
    check("多 h1 书导出后可重新导入", r.status_code == 200)
    toc4 = [i for i in r.get_json()["state"]["issues"] if i["check_name"] == "toc"]
    check("重导入后漏项判定一致", len(toc4) == 1 and "第二部分" in toc4[0]["message"])

    # 14. 回归：headers→td 无效引用 + 前端对象负载（cells/links 为坐标键控对象）
    r = c.post("/api/import", data={"file": (build_table_book_epub(), "tbl.epub")},
               content_type="multipart/form-data")
    check("headers→td 回归书可导入", r.status_code == 200, r.get_json().get("error"))
    st4 = r.get_json()["state"]
    bid4 = r.get_json()["book_id"]
    t_iss = [i for i in st4["issues"] if i["check_name"] == "table_a11y"]
    check("headers→td 报告无有效表头关联",
          len(t_iss) == 1 and "未关联到有效表头" in t_iss[0]["message"],
          [i["message"] for i in t_iss])
    ch_t = st4["chapters"][0]
    tpath = "html[1]/body[1]/section[1]/table[1]"
    r = c.get(f"/api/books/{bid4}/table/{ch_t['id']}", query_string={"path": tpath})
    mt = r.get_json()
    cell11 = [x for x in mt["cells"] if x["row"] == 1 and x["col"] == 1][0]
    check("headers→td 不进入朗读上下文",
          cell11["covered_by"] == [] and "无关联表头" in cell11["reading"],
          (cell11["covered_by"], cell11["reading"]))
    r = c.get(f"/api/books/{bid4}/export/report")
    check("问题报告含无有效表头关联", "未关联到有效表头" in r.get_data(as_text=True))

    # 前端真实负载：cells/links 为坐标键控对象（切换 th/td、设置 scope、点选关联表头）
    edits_obj = {
        "cells": {"1,0": {"row": 1, "col": 0, "tag": "th", "scope": "row"}},
        "links": {"1,1": {"row": 1, "col": 1, "keep": [], "headers": [[0, 0]]}},
    }
    r = c.post(f"/api/books/{bid4}/table/save",
               json={"chapter_id": ch_t["id"], "path": tpath, "edits": edits_obj})
    d = r.get_json()
    check("对象负载保存成功（不再 500）", r.status_code == 200 and d["ok"])
    check("对象负载保存后表格问题清零",
          not any(i["check_name"] == "table_a11y" for i in d["state"]["issues"]))
    html_t = c.get(f"/api/books/{bid4}/preview/ch1.xhtml").get_data(as_text=True)
    check("XHTML 写回: td→th 并设置 scope",
          "张三</th>" in html_t and 'scope="row"' in html_t)
    check("XHTML 写回: 点选关联生成 headers", 'headers="h-name"' in html_t)
    cell11 = [x for x in d["model"]["cells"] if x["row"] == 1 and x["col"] == 1][0]
    check("修复后朗读上下文含表头", cell11["reading"] == "姓名：90", cell11["reading"])
    r = c.get(f"/api/books/{bid4}/export/report")
    check("问题报告同步更新（表格问题已消除）",
          "未关联到有效表头" not in r.get_data(as_text=True))

    # 15. 列表语义：样例书 ch5 检查、候选、组合、操作、失败不落地与撤销复原
    st = c.get(f"/api/books/{bid}").get_json()
    ch5 = [x for x in st["chapters"] if "ch5" in x["href"]][0]
    l_iss = [i for i in st["issues"] if i["check_name"] == "list_a11y"
             and i["chapter_href"].endswith("ch5.xhtml")]
    check("列表:ch5 检出 >=8 项问题", len(l_iss) >= 8, len(l_iss))
    check("列表:空列表", any("空列表" in i["message"] for i in l_iss))
    check("列表:非法嵌套(直接子列表)", any("直接嵌套" in i["message"] for i in l_iss))
    check("列表:start 与可见编号不符", any("与有序列表实际序号" in i["message"] for i in l_iss))
    check("列表:被标题截断", any("截断" in i["message"] for i in l_iss))
    check("列表:孤立 li", any("孤立" in i["message"] for i in l_iss))
    check("列表:br 拼行候选", any("br" in i["message"] or "<br>" in i["message"]
                                  for i in l_iss))
    cand = c.get(f"/api/books/{bid}/list/candidates/{ch5['id']}").get_json()
    check("列表:候选接口返回候选与既有列表",
          len(cand["candidates"]) >= 4 and len(cand["lists"]) >= 3,
          (len(cand["candidates"]), len(cand["lists"])))
    # 被 h3+图片段 截断的 3 步安装列表候选
    step = [x for x in cand["candidates"] if x["suggested"] == "ol"
            and len(x["item_paths"]) == 3]
    check("列表:安装步骤候选含 3 项与 2 个拦截元素",
          bool(step) and len(step[0]["blocks"]) == 2,
          [(x["tag"]) for x in (step[0]["blocks"] if step else [])])

    def ch5_html():
        return c.get(f"/api/books/{bid}/preview/ch5.xhtml").get_data(as_text=True)

    def ch5_unchanged():
        return xhtml_sem(ch5_html()) == xhtml_sem(before_html)

    before_html = ch5_html()
    # 组合：含拦截元素（并入上一项）；保住 id/链接/图片
    blocks = {b["path"]: "into_prev" for b in step[0]["blocks"]}
    r = c.post(f"/api/books/{bid}/list/create", json={
        "chapter_id": ch5["id"], "paths": step[0]["item_paths"],
        "type": "ol", "blocks": blocks})
    d = r.get_json()
    check("组合被截断列表成功", r.status_code == 200 and d.get("ok"),
          d.get("error"))
    if r.status_code == 200:
        new_path = d["new_path"]
        check("组合后 3 个 li", len(d["model"]["items"]) == 3)
        html_now = ch5_html()
        check("组合保住链接 id(dl-link)", "dl-link" in html_now)
        check("组合保住图片与标题(setup-img/小贴士之外)",
              "setup-img" in html_now and "安装向导截图" in html_now)
        check("组合返回前后 DOM 对照",
              bool(d.get("before_pretty")) and "<ol" in d.get("after_pretty", ""))
        check("组合后模拟朗读含有序列表与序号",
              "有序列表" in d["model"]["reading"] and "第 1 项" in d["model"]["reading"])
        # 撤销：完整复原
        c.post(f"/api/books/{bid}/undo")
        check("撤销组合后 XHTML 完整复原", ch5_unchanged())
    else:
        check("组合失败未改变 XHTML", ch5_unchanged())

    # 失败请求不落地：非法 start（非整数）
    ul_nodes = [x for x in cand["candidates"] if x["suggested"] == "ul"]
    r = c.post(f"/api/books/{bid}/list/create", json={
        "chapter_id": ch5["id"], "paths": ul_nodes[0]["item_paths"][:2],
        "type": "ol", "start": "abc"})
    check("非法 start 被拒绝", r.status_code == 400)
    check("非法 start 不改变 XHTML", ch5_unchanged())

    # 失败请求不落地：跨父节点（交叉嵌套）—— 取两个不同父的段落路径
    nodes5 = c.get(f"/api/books/{bid}/chapters/{ch5['id']}/nodes").get_json()["nodes"]
    top_p = [n["dom_path"] for n in nodes5 if n["tag"] == "p"
             and n["parent_path"] == "html[1]/body[1]/section[1]"]
    # 区间非连续（跳项）必须拒绝且不落地
    if len(top_p) >= 3:
        r = c.post(f"/api/books/{bid}/list/create", json={
            "chapter_id": ch5["id"], "paths": [top_p[0], top_p[2]], "type": "ul"})
        check("非连续选择被拒绝", r.status_code == 400)
        check("非连续选择不改变 XHTML", ch5_unchanged())

    # 既有列表操作：start/value、缩进提升、拆分接续、归整、删空，全部可撤销复原
    lists = cand["lists"]
    # 找 start=5 的 ol
    ol_path = next((p for p in lists if p.endswith("ol[1]")), lists[0])
    m = c.get(f"/api/books/{bid}/list/model/{ch5['id']}",
              query_string={"path": ol_path}).get_json()
    check("列表模型含边界/朗读/问题",
          m.get("reading") and "items" in m and "boundaries" in m)
    # 修正 start=5→1（与可见编号一致），然后撤销复原
    r = c.post(f"/api/books/{bid}/list/op", json={
        "chapter_id": ch5["id"], "path": ol_path,
        "op": "set_start", "params": {"value": 1}})
    check("set_start 成功", r.status_code == 200 and r.get_json().get("ok"),
          r.get_json().get("error"))
    if r.status_code == 200:
        gone = not any(i["chapter_href"].endswith("ch5.xhtml")
                       and "实际序号" in i["message"]
                       and i["node_path"] and i["node_path"].startswith(ol_path)
                       for i in c.get(f"/api/books/{bid}").get_json()["issues"]
                       if i["check_name"] == "list_a11y")
        check("修正 start 后可见编号问题消除", gone)
        c.post(f"/api/books/{bid}/undo")
        check("撤销 set_start 完整复原", ch5_unchanged())

    # 拆分 + 接续（join）往返，撤销复原
    m = c.get(f"/api/books/{bid}/list/model/{ch5['id']}",
              query_string={"path": ol_path}).get_json()
    if len(m["items"]) >= 2:
        split_at = m["items"][1]["path"]
        r = c.post(f"/api/books/{bid}/list/op", json={
            "chapter_id": ch5["id"], "path": ol_path,
            "op": "split", "params": {"path": split_at}})
        d = r.get_json()
        check("拆分列表成功", r.status_code == 200 and d.get("ok"), d.get("error"))
        if r.status_code == 200:
            # 拆出的第二个列表是第一个的平级后继
            html_now = ch5_html()
            check("拆分产生两个平级 ol", html_now.count("<ol") >= 2)
            c.post(f"/api/books/{bid}/undo")
            check("撤销拆分完整复原", ch5_unchanged())

    # 非法结构归整后再撤销
    bad_ul = next((p for p in lists if p.endswith("ul[2]")), None)
    if bad_ul:
        r = c.post(f"/api/books/{bid}/list/op", json={
            "chapter_id": ch5["id"], "path": bad_ul,
            "op": "fix_structure", "params": {}})
        check("归整非法嵌套成功", r.status_code == 200 and r.get_json().get("ok"),
              r.get_json().get("error"))
        if r.status_code == 200:
            html_now = ch5_html()
            check("归整后无直接子 <p> 的 ul",
                  "<ul><p>" not in html_now.replace("\n", "").replace(" ", ""))
            c.post(f"/api/books/{bid}/undo")
            check("撤销归整完整复原", ch5_unchanged())

    # 删除空列表再撤销（空列表节点不丢失——撤销后回来）
    empty_ul = next((p for p in lists if p.endswith("ul[1]")), None)
    if empty_ul:
        r = c.post(f"/api/books/{bid}/list/op", json={
            "chapter_id": ch5["id"], "path": empty_ul,
            "op": "remove_empty", "params": {}})
        check("删除空列表成功", r.status_code == 200 and r.get_json().get("ok"),
              r.get_json().get("error"))
        if r.status_code == 200:
            c.post(f"/api/books/{bid}/undo")
            check("撤销删空后空列表回来且 XHTML 复原", ch5_unchanged())

    # body 直属编号段落（无 section 包裹）也能被识别与组合
    bid_body = _build_and_import_body_list_book(c)
    stb = c.get(f"/api/books/{bid_body}").get_json()
    bch = stb["chapters"][0]
    body_cand = c.get(f"/api/books/{bid_body}/list/candidates/{bch['id']}").get_json()
    check("body 直属编号段落被识别为候选",
          any(len(x["item_paths"]) >= 2 for x in body_cand["candidates"]),
          body_cand["candidates"])
    bc = [x for x in body_cand["candidates"] if len(x["item_paths"]) >= 2][0]
    html_before = c.get(f"/api/books/{bid_body}/preview/ch1.xhtml").get_data(as_text=True)
    r = c.post(f"/api/books/{bid_body}/list/create", json={
        "chapter_id": bch["id"], "paths": bc["item_paths"], "type": "ol"})
    check("body 直属段落组合成功", r.status_code == 200 and r.get_json().get("ok"),
          r.get_json().get("error"))
    if r.status_code == 200:
        c.post(f"/api/books/{bid_body}/undo")
        html_after = c.get(f"/api/books/{bid_body}/preview/ch1.xhtml").get_data(as_text=True)
        check("body 直属组合撤销后完整复原",
              xhtml_sem(html_after) == xhtml_sem(html_before))

    # 被标题截断的两个 ol：接续（标题并入上一项）后撤销，标题必须作为兄弟回来
    ol5 = next((p for p in lists if p.endswith("ol[1]")), None)
    if ol5:
        # 不指定处置方式 → 拒绝（且不落地）
        r = c.post(f"/api/books/{bid}/list/op", json={
            "chapter_id": ch5["id"], "path": ol5,
            "op": "join", "params": {"side": "after"}})
        check("接续被标题截断列表需指定处置方式", r.status_code == 400)
        check("拒绝接续不改变 XHTML", ch5_unchanged())
        r = c.post(f"/api/books/{bid}/list/op", json={
            "chapter_id": ch5["id"], "path": ol5,
            "op": "join", "params": {"side": "after", "placement": "into_prev"}})
        check("接续被截断列表（并入标题）成功",
              r.status_code == 200 and r.get_json().get("ok"), r.get_json().get("error"))
        if r.status_code == 200:
            html_now = ch5_html()
            check("接续后标题文本保留", "小贴士" in html_now)
            c.post(f"/api/books/{bid}/undo")
            html_back = ch5_html()
            check("撤销接续后被并入标题作为兄弟复原",
                  'id="ch5cut"' in html_back and ch5_unchanged())

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
