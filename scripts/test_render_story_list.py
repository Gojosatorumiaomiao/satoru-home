#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_story_list() 完整性自测：确认「继续读」展开后不漏字、不重字。

用法：
    python3 scripts/test_render_story_list.py

做法：在临时目录里生成三种样例故事（长首段多段、短首段多段、单段长文），
把 sync_content.OUT_STORIES 指向该临时目录，调用 render_story_list()，
再从渲染结果中取回首段与 details 内段落，与原始段落逐段比对。

覆盖 Issue #3 P1 的验收：长首段的尾部必须出现在页面上（以前只会出现在
段落之间被静默丢掉）。
"""
import html
import importlib.util
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module():
    path = os.path.join(HERE, "sync_content.py")
    spec = importlib.util.spec_from_file_location("sync_content", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASES = [
    # (文件名, 标题, 段落)
    (
        "2026-01-01",
        "长首段样例",
        [
            "首段故意写得超过八十八个字符，用来验证折叠之外的首段不会被截断，"
            "并且在展开之后整段都能读到，不会在中途少掉任何一句话。"
            "这里再补一句收尾，让首段明显长于旧实现里那个八十八字符的截断点。",
            "第二段也要出现，用来确认段落顺序没有被打乱。",
            "第三段：结束。",
        ],
    ),
    (
        "2026-01-02",
        "短首段多段样例",
        [
            "很短的首段。",
            "第二段，应当出现在展开正文里。",
            "第三段，也应当出现在展开正文里。",
        ],
    ),
    (
        "2026-01-03",
        "单段样例",
        [
            "整篇只有一段的长文本：没有第二段，所以页面不应该出现「继续读」，"
            "而这一段本身必须完整显示，同样不能因为超过八十八个字符就被截断或吞掉。",
        ],
    ),
]


HEADING_DATE = "2026-01-04"
HEADING_TITLE = "标题行样例"
HEADING_LEAD = "标题行之前的正文。"
HEADING_TEXT = "那两笔账"
HEADING_TAIL = "标题行之后的正文。"


def write_sample(root, date, title, paras):
    path = os.path.join(root, date + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# %s %s\n\n%s\n" % (date, title, "\n\n".join(paras)))
    return path


def parse_articles(rendered):
    """从渲染结果里取回 (标题, 首段, 展开段落列表)。"""
    out = []
    for block in re.findall(r"<article class=\"story\".*?</article>", rendered, re.S):
        title = html.unescape(re.search(r"<h3>(.*?)</h3>", block, re.S).group(1))
        lead = html.unescape(re.search(r"</h3>\s*<p>(.*?)</p>", block, re.S).group(1))
        details = re.search(r"<details>.*?</details>", block, re.S)
        rest = []
        if details:
            rest = [html.unescape(x) for x in
                    re.findall(r"<p>(.*?)</p>", details.group(0), re.S)]
        out.append((title, lead, rest))
    return out


def main():
    mod = load_module()
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        mod.OUT_STORIES = tmp
        for date, title, paras in CASES:
            write_sample(tmp, date, title, paras)
        # 第四种样例：正文含 Markdown 标题行（Issue #3 P2）
        write_sample(tmp, HEADING_DATE, HEADING_TITLE,
                     [HEADING_LEAD, "## " + HEADING_TEXT, HEADING_TAIL])
        rendered = mod.render_story_list()
        got = {t: (lead, rest) for t, lead, rest in parse_articles(rendered)}

        for date, title, paras in CASES:
            if title not in got:
                failures.append("%s：渲染结果里找不到该篇" % title)
                continue
            lead, rest = got[title]
            roundtrip = [lead] + rest
            status = "OK"
            if roundtrip != paras:
                status = "FAIL"
                failures.append(
                    "%s：段落不一致\n  原文(%d段)：%r\n  渲染(%d段)：%r" %
                    (title, len(paras), paras, len(roundtrip), roundtrip))
            if rest and len(paras) == 1:
                status = "FAIL"
                failures.append("%s：单段样例不该出现展开区" % title)
            print("[%s] %s 日期=%s 原文段落=%d 渲染段落=%d 首段字符=%d" %
                  (status, title, date, len(paras), len(roundtrip), len(lead)))

        # 额外：确认没有把整篇复制两遍（重字）
        for date, title, paras in CASES:
            lead, rest = got.get(title, ("", []))
            joined = lead + "".join(rest)
            if len(paras) > 1 and lead and lead in "".join(rest):
                failures.append("%s：首段在展开正文中重复出现" % title)

        # 额外：Markdown 标题行必须变成语义标题，页面不能残留 "## "
        hblock = [b for b in re.findall(r'<article class="story".*?</article>', rendered, re.S)
                  if 'id="story-%s"' % HEADING_DATE in b]
        if not hblock:
            failures.append("标题行样例：渲染结果里找不到该篇")
            print("[FAIL] %s 日期=%s" % (HEADING_TITLE, HEADING_DATE))
        else:
            block = hblock[0]
            detail = re.search(r"<details>.*?</details>", block, re.S)
            inner = detail.group(0) if detail else ""
            status = "OK"
            if "##" in rendered:
                status = "FAIL"
                failures.append("标题行样例：页面仍出现字面量 '##'")
            if "<h4>%s</h4>" % HEADING_TEXT not in inner:
                status = "FAIL"
                failures.append("标题行样例：标题没有渲染成 <h4>%s</h4>" % HEADING_TEXT)
            if HEADING_LEAD not in block:
                status = "FAIL"
                failures.append("标题行样例：首段丢失")
            order = [inner.find(HEADING_TEXT), inner.find(HEADING_TAIL)]
            if -1 in order or order[0] > order[1]:
                status = "FAIL"
                failures.append("标题行样例：标题与后文顺序不正确")
            print("[%s] %s 日期=%s 原文段落=3 渲染段落=3" %
                  (status, HEADING_TITLE, HEADING_DATE))

        # 额外：折叠控件的可访问名称必须包含所属篇名（Issue #3 P1 可访问性）。
        # 只靠一次性浏览器计数不够，未来新增故事时这条断言会先失败。
        aria_names = []
        aria_checked = 0
        for block in re.findall(r'<article class="story".*?</article>', rendered, re.S):
            h3 = re.search(r"<h3>(.*?)</h3>", block, re.S)
            title = html.unescape(h3.group(1)) if h3 else ""
            msum = re.search(r"<summary([^>]*)>(.*?)</summary>", block, re.S)
            if not msum:
                continue
            attrs, visible = msum.group(1), msum.group(2)
            label = re.search(r'aria-label="([^"]*)"', attrs)
            got_label = label.group(1) if label else None
            expected = "展开《%s》全文" % title
            aria_names.append(got_label)
            if got_label != expected:
                failures.append(
                    "%s：折叠控件可访问名称不正确（%r，应为 %r）" %
                    (title, got_label, expected))
            if visible != "继续读":
                failures.append(
                    "%s：折叠控件可见文字应为「继续读」，实为 %r" % (title, visible))
            aria_checked += 1
        if aria_checked == 0:
            failures.append("折叠控件：没有找到带展开区的 summary，无法检查可访问名称")
        distinct = len(set(aria_names))
        distinct_ok = distinct == len(aria_names) and aria_checked > 0
        if not distinct_ok:
            failures.append("折叠控件：可访问名称存在重复，读屏用户无法区分")
        print("[%s] 折叠控件可访问名称：检查 %d 个，互不相同 %s" %
              ("OK" if aria_checked and distinct_ok else "FAIL",
               aria_checked, distinct_ok))

        # 额外：每篇「全文」链接的可访问名称必须包含所属篇名（Issue #3 P1 可访问性）。
        # 与折叠控件同理：读屏用户按链接浏览时要能区分将打开哪一篇。
        link_names = []
        link_checked = 0
        for block in re.findall(r'<article class="story".*?</article>', rendered, re.S):
            h3 = re.search(r"<h3>(.*?)</h3>", block, re.S)
            title = html.unescape(h3.group(1)) if h3 else ""
            mlink = re.search(r'<a class="story-link"([^>]*)>(.*?)</a>', block, re.S)
            if not mlink:
                continue
            attrs, visible = mlink.group(1), mlink.group(2)
            label = re.search(r'aria-label="([^"]*)"', attrs)
            got_label = label.group(1) if label else None
            expected = "阅读《%s》全文" % title
            link_names.append(got_label)
            if got_label != expected:
                failures.append(
                    "%s：「全文」链接可访问名称不正确（%r，应为 %r）" %
                    (title, got_label, expected))
            if visible != "全文":
                failures.append(
                    "%s：「全文」链接可见文字应为「全文」，实为 %r" % (title, visible))
            if 'href="stories/' not in attrs:
                failures.append("%s：「全文」链接地址不像归档路径" % title)
            link_checked += 1
        if link_checked == 0:
            failures.append("「全文」链接：没有找到 story-link，无法检查可访问名称")
        link_distinct = len(set(link_names))
        link_distinct_ok = link_distinct == len(link_names) and link_checked > 0
        if not link_distinct_ok:
            failures.append("「全文」链接：可访问名称存在重复，读屏用户无法区分")
        print("[%s] 「全文」链接可访问名称：检查 %d 个，互不相同 %s" %
              ("OK" if link_checked and link_distinct_ok else "FAIL",
               link_checked, link_distinct_ok))

    if failures:
        print("\n测试失败：")
        for f in failures:
            print(" -", f)
        return 1
    print("\n全部样例通过：无漏字、无重字。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
