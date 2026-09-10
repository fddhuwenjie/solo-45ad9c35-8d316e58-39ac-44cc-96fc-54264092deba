"""端到端自测：样例书 → 检查 → 编辑 → 撤销 → 地标同步 → 导出 → 重新打开验证。"""
import io
import json
import os
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


def main():
    db.init_db()
    c = app.test_client()

    # 1. 载入样例书
    r = c.post("/api/load_sample")
    assert r.status_code == 200, r.get_data(as_text=True)
    bid = r.get_json()["book_id"]
    st = r.get_json()["state"]
    check("载入样例书", bid >= 1)
    check("章节数=4", len(st["chapters"]) == 4)

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
    check("移动重跑了 toc/heading/footnote/table 检查",
          set(d["checks_ran"]) == {"heading_hierarchy", "toc", "footnote_backlink",
                                   "table_a11y"})

    # 8. 章节排序：使 spine 与目录一致，然后撤销
    order = [ch1["id"], ch3["id"], ch2["id"], ch4["id"]]
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
    check("重开书籍章节数=4", len(st2["chapters"]) == 4)
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

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
