"""生成内置样例书 sample.epub。

故意植入的问题（用于演示各项检查与修复流程）：
- 目录次序与 spine 冲突（目录把第三章排在第二章前）
- 目录漏项（第一章的 h2「背景」未收录）
- 标题层级跳跃（第二章 h1 之后直接 h3）
- 图片缺少 alt（第一章 photo.png）；另有装饰图已正确留空 alt
- 第二章 <html> 缺少 lang
- 第二章重复 id="dup"
- 第二章失效锚点 #nowhere
- 跨章脚注：第一章 noteref → 第三章 fn1，fn1 缺少回链；fn2 有回链作对照
- 错序侧栏：第一章 aside.sidebar 位于其所属小节标题之前，第三章侧栏在引言段之前
- 第四章复杂表格：表A 缺 caption、首行视觉表头仍为 td、headers 引用失效、数据格无法关联表头；
  表B scope 与合并结构冲突、headers 引用循环；表C 为规范用法对照（无问题）
"""
import base64
import os
import zipfile

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

XHTML_HEAD = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<html xmlns="http://www.w3.org/1999/xhtml" '
              'xmlns:epub="http://www.idpf.org/2007/ops"%s>'
              '<head><title>%s</title><link rel="stylesheet" href="css/style.css"/></head>')

CH1 = XHTML_HEAD % (' lang="zh-CN" xml:lang="zh-CN"', '第一章 引言') + """
<body>
<section epub:type="chapter" id="s1">
<h1 id="ch1">第一章 引言</h1>
<p>欢迎来到本书。本章介绍背景知识，并包含一个跨章脚注<a epub:type="noteref" id="ref1" href="ch3.xhtml#fn1">[1]</a>。</p>
<aside epub:type="sidebar" id="sb1" class="sidebar"><p>侧栏：本侧栏在 DOM 中位置错误，应位于「背景」小节之后，请拖拽调整。</p></aside>
<h2 id="ch1s1">背景</h2>
<p>这里有一张缺少替代文本的照片：</p>
<p><img src="images/photo.png" width="1" height="1"/></p>
<p>下面是一张装饰图（已正确标记为空 alt）：</p>
<p><img src="images/deco.png" alt="" role="presentation" width="1" height="1"/></p>
</section>
</body></html>"""

CH2 = XHTML_HEAD % ('', '第二章 方法') + """
<body>
<section epub:type="chapter" id="s2">
<h1 id="ch2">第二章 方法</h1>
<h3 id="ch2s1">实验设计</h3>
<p>本节标题直接从一级跳到了三级。</p>
<p id="dup">第一段。</p>
<p id="dup">这是重复 ID 的段落。</p>
<p>参见<a href="#nowhere">不存在的锚点</a>，以及正常的脚注<a epub:type="noteref" id="ref2" href="ch3.xhtml#fn2">[2]</a>。</p>
<p>Bonjour, ceci est une phrase francaise.</p>
</section>
</body></html>"""

CH3 = XHTML_HEAD % (' lang="zh-CN" xml:lang="zh-CN"', '第三章 结果') + """
<body>
<section epub:type="chapter" id="s3">
<h1 id="ch3">第三章 结果</h1>
<aside epub:type="sidebar" id="sb2" class="sidebar"><p>错序侧栏：应出现在下方引言段之后。</p></aside>
<p>本章展示实验结果，脚注集中于文末。</p>
<section epub:type="footnotes">
<aside epub:type="footnote" id="fn1"><p>1. 这是来自第一章的跨章脚注（缺少返回正文的回链）。</p></aside>
<aside epub:type="footnote" id="fn2"><p>2. 第二章的脚注。<a href="ch2.xhtml#ref2">↩ 返回正文</a></p></aside>
</section>
</section>
</body></html>"""

CH4 = XHTML_HEAD % (' lang="zh-CN" xml:lang="zh-CN"', '第四章 数据表格') + """
<body>
<section epub:type="chapter" id="s4">
<h1 id="ch4">第四章 数据表格</h1>
<p>本章演示复杂表格的表头层级与关联校修。</p>
<h2>销售额表（待修复）</h2>
<table>
<tr><td><strong>地区</strong></td><td><strong>季度</strong></td><td><strong>销售额(万元)</strong></td></tr>
<tr><td rowspan="2">华东</td><td>Q1</td><td>120</td></tr>
<tr><td>Q2</td><td>135</td></tr>
<tr><td>华南</td><td>Q1</td><td headers="sale-q1">98</td></tr>
</table>
<h2>员工出勤表（部分标记错误）</h2>
<table>
<caption>员工出勤表</caption>
<tr><th scope="col">项目组</th><th scope="col">姓名</th><th scope="col">出勤天数</th></tr>
<tr><th id="b-g1" scope="row" rowspan="2">A组</th><th id="b-n1" scope="row" headers="b-c1">张三</th><td id="b-c1" headers="b-n1">22</td></tr>
<tr><th id="b-n2" scope="row">李四</th><td>21</td></tr>
</table>
<h2>规范示例</h2>
<table>
<caption>各地区季度销售额（规范示例）</caption>
<tr><th scope="col">地区</th><th scope="col">季度</th><th scope="col">销售额(万元)</th></tr>
<tr><th scope="rowgroup" rowspan="2">华东</th><th scope="row">Q1</th><td>120</td></tr>
<tr><th scope="row">Q2</th><td>135</td></tr>
<tr><th scope="rowgroup">华南</th><th scope="row">Q1</th><td>98</td></tr>
</table>
</section>
</body></html>"""

NAV = XHTML_HEAD % (' lang="zh-CN" xml:lang="zh-CN"', '目录') + """
<body>
<nav epub:type="toc" id="toc">
<h1>目录</h1>
<ol>
<li><a href="ch1.xhtml">第一章 引言</a></li>
<li><a href="ch3.xhtml">第三章 结果</a></li>
<li><a href="ch2.xhtml">第二章 方法</a></li>
<li><a href="ch4.xhtml">第四章 数据表格</a></li>
</ol>
</nav>
<nav epub:type="landmarks" id="landmarks">
<h1>地标</h1>
<ol>
<li><a epub:type="bodymatter" href="ch1.xhtml">正文开始</a></li>
</ol>
</nav>
</body></html>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="zh-CN">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">sample-a11y-001</dc:identifier>
<dc:title>无障碍校修样例书</dc:title>
<dc:language>zh-CN</dc:language>
<meta property="dcterms:modified">2026-09-10T00:00:00Z</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
<item id="ch3" href="ch3.xhtml" media-type="application/xhtml+xml"/>
<item id="ch4" href="ch4.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="css/style.css" media-type="text/css"/>
<item id="img1" href="images/photo.png" media-type="image/png"/>
<item id="img2" href="images/deco.png" media-type="image/png"/>
</manifest>
<spine><itemref idref="ch1"/><itemref idref="ch2"/><itemref idref="ch3"/><itemref idref="ch4"/></spine>
</package>"""

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

CSS = "body{font-family:serif;margin:2em;} .sidebar{border:1px solid #999;padding:.5em;background:#f6f6f6;} aside{font-size:.9em;}"


def create_sample(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        for name, data in [
            ("META-INF/container.xml", CONTAINER),
            ("content.opf", OPF),
            ("nav.xhtml", NAV),
            ("ch1.xhtml", CH1),
            ("ch2.xhtml", CH2),
            ("ch3.xhtml", CH3),
            ("ch4.xhtml", CH4),
            ("css/style.css", CSS),
        ]:
            z.writestr(name, data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("images/photo.png", PNG_1PX, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("images/deco.png", PNG_1PX, compress_type=zipfile.ZIP_DEFLATED)
    return path


if __name__ == "__main__":
    p = create_sample(os.path.join(os.path.dirname(__file__), "data", "sample.epub"))
    print("sample written:", p, os.path.getsize(p), "bytes")
