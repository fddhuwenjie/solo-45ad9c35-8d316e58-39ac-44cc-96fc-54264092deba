"""端到端自测：样例书 → 检查 → 编辑 → 撤销 → 导出 → 重新打开验证。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from app import app

PASS, FAIL = [], []


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
    check("章节数=3", len(st["chapters"]) == 3)

    issues = st["issues"]
    by = {}
    for it in issues:
        by.setdefault(it["check_name"], []).append(it)
    check("检查:目录次序冲突/漏项", len(by.get("toc", [])) >= 2, len(by.get("toc", [])))
    check("检查:标题层级跳跃", len(by.get("heading_hierarchy", [])) == 1)
    check("检查:图片缺 alt", len(by.get("img_alt", [])) == 1)
    check("检查:语言标记缺失", len(by.get("lang", [])) == 1)
    check("检查:重复 ID", len(by.get("duplicate_id", [])) == 1)
    check("检查:失效锚点", len(by.get("broken_anchor", [])) == 1)
    check("检查:脚注回链缺失(跨章)", len(by.get("footnote_backlink", [])) == 1)

    ch1 = [c for c in st["chapters"] if "ch1" in c["href"]][0]
    ch2 = [c for c in st["chapters"] if "ch2" in c["href"]][0]
    ch3 = [c for c in st["chapters"] if "ch3" in c["href"]][0]

    # 2. 节点提取
    nodes1 = c.get(f"/api/books/{bid}/chapters/{ch1['id']}/nodes").get_json()
    img_node = [n for n in nodes1 if n["tag"] == "img" and n["alt"] is None]
    check("章1提取到缺 alt 图片节点", len(img_node) == 1)
    aside = [n for n in nodes1 if n["tag"] == "aside"]
    check("章1提取到侧栏节点", len(aside) == 1)

    # 3. 修改图片 alt → 只重跑 img_alt
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch1["id"], "dom_path": img_node[0]["dom_path"],
        "updates": {"alt": "实验室照片"}})
    d = r.get_json()
    check("修改 alt 成功", r.status_code == 200 and d["ok"])
    check("只重跑 img_alt 检查", d["checks_ran"] == ["img_alt"], d["checks_ran"])
    check("img_alt 问题已消除",
          not any(i["check_name"] == "img_alt" for i in d["state"]["issues"]))

    # 4. 撤销 → 问题恢复，且变更记录保留（修订版本）
    r = c.post(f"/api/books/{bid}/undo")
    d = r.get_json()
    check("撤销成功", d["ok"])
    check("撤销后 img_alt 问题恢复",
          any(i["check_name"] == "img_alt" for i in d["state"]["issues"]))
    check("变更记录保留(含已撤销)",
          any(ch["undone"] for ch in d["state"]["changes"]))

    # 5. 修复标题层级 h3→h2
    nodes2 = c.get(f"/api/books/{bid}/chapters/{ch2['id']}/nodes").get_json()
    h3 = [n for n in nodes2 if n["tag"] == "h3"][0]
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch2["id"], "dom_path": h3["dom_path"],
        "updates": {"heading_level": 2}})
    d = r.get_json()
    check("标题层级修复", d["ok"] and
          not any(i["check_name"] == "heading_hierarchy" for i in d["state"]["issues"]))

    # 6. 修复第二章 html lang（路径 html[1]）
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch2["id"], "dom_path": "html[1]",
        "updates": {"lang": "zh-CN"}})
    d = r.get_json()
    check("补全语言标记", d["ok"] and
          not any(i["check_name"] == "lang" for i in d["state"]["issues"]))

    # 7. 拖拽移动错序侧栏：章1 aside 移到 h2 之后的 p 之前
    nodes1 = c.get(f"/api/books/{bid}/chapters/{ch1['id']}/nodes").get_json()
    aside = [n for n in nodes1 if n["tag"] == "aside"][0]
    h2 = [n for n in nodes1 if n["tag"] == "h2"][0]
    after_h2 = [n for n in nodes1
                if n["order_index"] > h2["order_index"] and n["parent_path"] == h2["parent_path"]]
    target = after_h2[0]
    r = c.post(f"/api/books/{bid}/node/move", json={
        "chapter_id": ch1["id"], "dom_path": aside["dom_path"],
        "before_path": target["dom_path"]})
    d = r.get_json()
    check("移动侧栏节点", d["ok"])
    new_nodes = d["nodes"]
    ai = [n["order_index"] for n in new_nodes if n["tag"] == "aside"][0]
    hi = [n["order_index"] for n in new_nodes if n["tag"] == "h2"][0]
    check("侧栏已位于 h2 之后", ai > hi, f"aside={ai} h2={hi}")
    check("移动重跑了 toc/heading/footnote 检查",
          set(d["checks_ran"]) == {"heading_hierarchy", "toc", "footnote_backlink"})

    # 8. 章节排序：使 spine 与目录一致 (ch1, ch3, ch2)
    st = c.get(f"/api/books/{bid}").get_json()
    order = [ch1["id"], ch3["id"], ch2["id"]]
    r = c.post(f"/api/books/{bid}/spine/reorder", json={"order": order})
    d = r.get_json()
    check("spine 重排", d["ok"])
    check("目录次序冲突消除",
          not any(i["check_name"] == "toc" and "冲突" in i["message"]
                  for i in d["state"]["issues"]))
    # 撤销 spine 重排
    r = c.post(f"/api/books/{bid}/undo")
    d = r.get_json()
    check("撤销 spine 重排后冲突重现",
          any(i["check_name"] == "toc" and "冲突" in i["message"]
              for i in d["state"]["issues"]))

    # 9. 地标：增 / 删
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch1["id"], "dom_path": "html[1]/body[1]/section[1]",
        "updates": {"epub_type": "chapter"}})
    d = r.get_json()
    check("新增/更新地标", d["ok"] and
          any(l["epub_type"] == "chapter" for l in d["state"]["landmarks"]))
    r = c.post(f"/api/books/{bid}/node/update", json={
        "chapter_id": ch1["id"], "dom_path": "html[1]/body[1]/section[1]",
        "updates": {"epub_type": ""}})
    d = r.get_json()
    check("删除地标", d["ok"] and
          not any(l["epub_type"] == "chapter" and l["chapter"].endswith("ch1.xhtml")
                  for l in d["state"]["landmarks"]))

    # 10. 预览与资源
    r = c.get(f"/api/books/{bid}/preview/ch1.xhtml")
    check("预览页面可访问", r.status_code == 200 and b"pathOf" in r.data)
    r = c.get(f"/api/books/{bid}/res/images/photo.png")
    check("预览图片资源可访问", r.status_code == 200 and r.data[:4] == b"\x89PNG")

    # 11. 导出 EPUB 并重新打开
    r = c.get(f"/api/books/{bid}/export/epub")
    check("导出 EPUB", r.status_code == 200 and r.data[:2] == b"PK")
    r2 = c.post("/api/import", data={"file": (io.BytesIO(r.data), "fixed.epub")},
                content_type="multipart/form-data")
    check("修正版 EPUB 可重新打开", r2.status_code == 200, r2.get_json())
    st2 = r2.get_json()["state"]
    check("重开书籍章节数=3", len(st2["chapters"]) == 3)
    check("重开后 h3 修复仍生效",
          not any(i["check_name"] == "heading_hierarchy" for i in st2["issues"]))
    check("重开后未修复项仍在(失效锚点)",
          any(i["check_name"] == "broken_anchor" for i in st2["issues"]))

    # 12. 导出变更记录与报告
    r = c.get(f"/api/books/{bid}/export/changes")
    check("导出变更记录 CSV", r.status_code == 200 and "时间".encode("utf-8-sig") in r.data)
    r = c.get(f"/api/books/{bid}/export/report")
    check("导出问题报告 HTML", r.status_code == 200 and "未解决问题报告".encode() in r.data)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
