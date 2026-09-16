#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档保护回归测试（全部使用虚构数据，不读取本机真实故事或状态文件）。

用法：
    python3 scripts/test_archive_protection.py

在临时工作区里对一个含三篇的虚构源文件反复运行 sync_content.main()，验证：
  1. 一次运行把三篇都归档，生成三个独立入口（<date>.md / -2 / -3）；
  2. 重复运行内容与链接稳定，归档逐字不变；
  3. 改动源文本后再运行，已公开归档仍逐字不变（不覆盖），并在报告中列出差异。

用修复前的 sync_content.py 运行时，「摘要 3」与「改源文后归档不变」会 FAIL。
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join("/tmp", "satoru-archive-protection-test")

spec = importlib.util.spec_from_file_location(
    "sc", os.path.join(REPO, "scripts", "sync_content.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

shutil.rmtree(TMP, ignore_errors=True)
shutil.copytree(REPO, TMP + "/site",
                ignore=shutil.ignore_patterns(".git", "evidence", "__pycache__"))
os.makedirs(TMP + "/stories")
os.makedirs(TMP + "/data")

sc.WS = TMP
sc.SITE = TMP + "/site"
sc.SRC_STORIES = TMP + "/stories"
sc.DAILY_JSON = TMP + "/data/satoru-daily.json"
sc.OUT_STORIES = sc.SITE + "/stories"
sc.PUBLISHED_LOG = sc.SITE + "/data/daily-published.json"

DATE = "2099-02-03"
SRC = os.path.join(sc.SRC_STORIES, DATE + ".md")

STORIES = [
    ("甲篇", ["虚构甲篇首段。", "虚构甲篇第二段。"]),
    ("乙篇", ["虚构乙篇首段。", "虚构乙篇第二段。"]),
    ("丙篇", ["虚构丙篇首段。", "虚构丙篇第二段。"]),
]


def write_source(stories):
    with open(SRC, "w", encoding="utf-8") as fh:
        for title, paras in stories:
            fh.write("# %s %s\n\n%s\n\n" % (DATE, title, "\n\n".join(paras)))


def run_main():
    sys.argv = ["sync_content.py", DATE]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sc.main()
    try:
        return json.loads(buf.getvalue())
    except Exception:
        return {"raw": buf.getvalue()}


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]


write_source(STORIES)
with open(sc.DAILY_JSON, "w", encoding="utf-8") as fh:
    json.dump({"date": DATE, "contacts": [
        {"kind": "normal", "time": "09:00", "summary": "虚构动态：早上在窗边翻笔记。"}]},
        fh, ensure_ascii=False)

ARCHIVES = [os.path.join(sc.OUT_STORIES, f) for f in
            (DATE + ".md", DATE + "-2.md", DATE + "-3.md")]

rep1 = run_main()
after_first = [digest(p) if os.path.exists(p) else None for p in ARCHIVES]
stories_html_1 = open(os.path.join(sc.SITE, "stories.html"), encoding="utf-8").read()
links_1 = [x for x in re.findall(r'id="story-([^"]+)"', stories_html_1)
           if x.startswith(DATE)]

rep2 = run_main()
after_second = [digest(p) if os.path.exists(p) else None for p in ARCHIVES]
stories_html_2 = open(os.path.join(sc.SITE, "stories.html"), encoding="utf-8").read()

# 改动源文第一篇正文后重跑：已公开归档不应被改写
changed = [("甲篇", ["虚构甲篇首段（已改动）。", "虚构甲篇第二段（已改动）。"]),
           STORIES[1], STORIES[2]]
write_source(changed)
rep3 = run_main()
after_third = [digest(p) if os.path.exists(p) else None for p in ARCHIVES]
steps3 = " | ".join(rep3.get("steps", []))

lead_ok = all(
    os.path.exists(p) and open(p, encoding="utf-8").read().startswith("# %s %s\n" % (DATE, t))
    for p, (t, _) in zip(ARCHIVES, STORIES))

checks = [
    ("三篇各生成独立归档", all(x for x in after_first)),
    ("列表出现三个独立锚点（%d）" % len(links_1), len(links_1) == 3),
    ("归档锚点为 story-%s / -2 / -3" % DATE,
     sorted(links_1) == sorted([DATE, DATE + "-2", DATE + "-3"])),
    ("三个归档链接均指向对应文件",
     all(('href="stories/%s' % f) in stories_html_1 for f in
         (DATE + ".md", DATE + "-2.md", DATE + "-3.md"))),
    ("归档标题与日期正确", lead_ok),
    ("重复运行归档逐字不变", after_first == after_second),
    ("重复运行列表内容稳定", stories_html_1 == stories_html_2),
    ("改源文后已公开归档逐字不变", after_second == after_third),
    ("报告列出未覆盖的归档",
     ("保留已有归档" in steps3) or bool(rep3.get("archive_conflicts"))),
]

ok = True
for name, res in checks:
    print("[%s] %s" % ("OK" if res else "FAIL", name))
    ok = ok and res
if not ok:
    print("\nrun1 步骤：%s" % " | ".join(rep1.get("steps", [])))
    print("run3 步骤：%s" % steps3)
    print("run3 报告：%s" % json.dumps(rep3, ensure_ascii=False))
print("\n归档保护回归测试：%s" % ("全部通过（虚构数据）" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
