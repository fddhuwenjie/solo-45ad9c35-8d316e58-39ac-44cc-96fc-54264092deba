"""生成内置样例书 sample.epub。

故意植入的问题（用于演示各项检查与修复流程）：
- 目录次序与 spine 冲突（目录把第三章排在第二章前）
- 目录漏项（第一章的 h2「背景」未收录）
- 标题层级跳跃（第二章 h1 之后直接 h3）
- 图片缺少 alt（第一章 photo.png）；另有装饰图已正确留空 alt
- 第二章 <html> 缺少 lang
- 第二章重复 id="dup"
- 第二章失效锚点 #nowhere
- 跨章脚注错指：第一章 ref1、第二章 ref2 → 第三章 fn1/fn2（目标章设有脚注区），
  fn1 缺回链；另有一个引用指向 EPUB 之外的跨书目链接
- 错序侧栏：第一章 aside.sidebar 位于其所属小节标题之前，第三章侧栏在引言段之前
- 第四章复杂表格：表A 缺 caption、首行视觉表头仍为 td、headers 引用失效、数据格无法关联表头；
  表B scope 与合并结构冲突、headers 引用循环；表C 为规范用法对照（无问题）
- 第五章列表语义：连续编号段落伪装成有序列表、单段 <br> 拼行、孤立 li、
  空列表、ol start/可见编号不符、非法直接嵌套、相邻 ol 被标题与图片截断
- 第六章尾注关系图：一号多引且回链只回第一处、未声明引用（[6] 无连线）、
  孤立尾注、一号多注（两个 5 号尾注）、尾注区混入普通列表项、嵌套注释、
  未声明注释（缺 epub:type）、跨书目链接与跨章脚注错指
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
<p>欢迎来到本书。本章介绍背景知识，并包含一个跨章错指的脚注<a epub:type="noteref" id="ref1" href="ch3.xhtml#fn1">[1]</a>。</p>
<p>另有一处引用指向了 EPUB 之外的资料<a epub:type="noteref" id="ref-ext" href="https://example.org/paper">[2]</a>。</p>
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
<p>参见<a href="#nowhere">不存在的锚点</a>，以及同样跨章错指的脚注<a epub:type="noteref" id="ref2" href="ch3.xhtml#fn2">[2]</a>。</p>
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
<aside epub:type="footnote" id="fn1"><p>1. 这是被第一章跨章错指的脚注（且缺少返回正文的回链）。</p></aside>
<aside epub:type="footnote" id="fn2"><p>2. 第二章跨章错指的脚注。<a href="ch2.xhtml#ref2">↩ 返回正文</a></p></aside>
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

CH5 = XHTML_HEAD % (' lang="zh-CN" xml:lang="zh-CN"', '第五章 列表') + """
<body>
<section epub:type="chapter" id="s5">
<h1 id="ch5">第五章 列表</h1>
<p>本章演示“视觉上是列表、读屏却读不出项目边界”的各种排版。</p>

<h2 id="ch5s1">安装步骤（编号段落）</h2>
<p>1. 下载安装包<a id="dl-link" href="http://example.com/dl">前往下载页</a>。</p>
<p>2. 双击运行安装程序。</p>
<h3 id="ch5fig">图 1 安装向导截图</h3>
<p><img id="setup-img" src="images/deco.png" alt="安装向导截图" width="1" height="1"/></p>
<p>3. 按向导提示完成安装并重启。</p>

<h2 id="ch5s2">注意事项（项目符号段落）</h2>
<p>• 请备份重要数据。</p>
<p>• 安装过程中不要断电。</p>
<p style="margin-left:2em">◦ 笔记本电脑请接上电源。</p>
<p style="margin-left:4em">· 长时间安装建议关闭休眠。</p>

<h2 id="ch5s3">单段内换行拼成的列表</h2>
<p>（一）核对姓名<br/>（二）核对证件号<br/>（三）签字确认</p>

<h2 id="ch5s4">既有列表的问题</h2>
<p>下面是空列表：</p>
<ul></ul>
<p>下面的有序列表 start 与可见编号不符（start=5，首项却写“1.”）：</p>
<ol start="5">
<li>1. 第一步可见编号与播报序号不一致。</li>
<li>2. 第二步。</li>
</ol>
<h3 id="ch5cut">小贴士</h3>
<ol start="3">
<li>3. 这其实是被标题截断的同一张列表，应接续上一段。</li>
</ol>
<p>下面是非法嵌套（子列表直接放在 ul 里、列表中混入段落）：</p>
<ul>
<li>合法的第一项。</li>
<p>这是错放在 ul 里的段落。</p>
<ul><li>被直接塞进 ul 的子项。</li></ul>
</ul>
<p>下面是两个未包在列表里的孤立 li：</p>
<li id="orphan1">孤立项目甲</li>
<li id="orphan2">孤立项目乙</li>
</section>
</body></html>"""

CH6 = XHTML_HEAD % (' lang="zh-CN" xml:lang="zh-CN"', '第六章 尾注') + """
<body>
<section epub:type="chapter" id="s6">
<h1 id="ch6">第六章 尾注</h1>
<p>同一尾注在正文中被引用两次：第一次<a epub:type="noteref" id="enr3a" href="ch6.xhtml#en3">[3]</a>，
稍后第二次引用<a epub:type="noteref" id="enr3b" href="ch6.xhtml#en3">[3]</a>，
而该尾注只有一条回程链接，只能把读者送回第一处。</p>
<p>这里有一个未声明的引用：<sup>[6]</sup>，它没有 noteref 语义也没有连线。</p>
<p>一个引用指向 EPUB 之外：<a epub:type="noteref" id="enr7" href="https://example.org/study">[7]</a>。</p>
<p>另有一个跨章脚注错指：<a epub:type="noteref" id="enr1" href="ch3.xhtml#fn1">[1]</a>。</p>

<section epub:type="endnotes" id="endnotes">
<h2>尾注</h2>
<ol>
<li epub:type="endnote" id="en3"><p>3. 两次引用共用的尾注。<a href="#enr3a">↩ 返回引用处</a></p></li>
<li epub:type="endnote" id="en4"><p>4. 孤立尾注：正文没有任何引用指向这里。</p></li>
<li epub:type="endnote" id="en5a"><p>5. 第一个五号尾注。</p></li>
<li epub:type="endnote" id="en5b"><p>5. 第二个五号尾注（一号多注）。
  <aside epub:type="endnote" id="en-nested"><p>[8] 嵌套在尾注里的注释。</p></aside>
  <aside epub:type="endnote" id="en-loop"><p>[9] 含引用环路的嵌套注释，内部的注释引用又指回自身
    <a epub:type="noteref" id="enr-loop" href="#en-loop">[9]</a>。</p></aside></p></li>
<li><p>出版方信息（这是混入尾注区的普通列表项，没有注号）。</p></li>
</ol>
<aside id="en6"><p>[6] 未声明的注释：有可见注号，却缺少 epub:type/role 语义。</p></aside>
</section>
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
<li><a href="ch5.xhtml">第五章 列表</a></li>
<li><a href="ch6.xhtml">第六章 尾注</a></li>
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
<item id="ch5" href="ch5.xhtml" media-type="application/xhtml+xml"/>
<item id="ch6" href="ch6.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="css/style.css" media-type="text/css"/>
<item id="img1" href="images/photo.png" media-type="image/png"/>
<item id="img2" href="images/deco.png" media-type="image/png"/>
</manifest>
<spine><itemref idref="ch1"/><itemref idref="ch2"/><itemref idref="ch3"/><itemref idref="ch4"/><itemref idref="ch5"/><itemref idref="ch6"/></spine>
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
            ("ch5.xhtml", CH5),
            ("ch6.xhtml", CH6),
            ("css/style.css", CSS),
        ]:
            z.writestr(name, data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("images/photo.png", PNG_1PX, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("images/deco.png", PNG_1PX, compress_type=zipfile.ZIP_DEFLATED)
    return path


if __name__ == "__main__":
    p = create_sample(os.path.join(os.path.dirname(__file__), "data", "sample.epub"))
    print("sample written:", p, os.path.getsize(p), "bytes")
