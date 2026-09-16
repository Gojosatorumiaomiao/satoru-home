#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端演练（全部使用虚构数据，不读取本机真实故事或状态文件）。

用法：
    python3 scripts/rehearse_sync_fictional.py

在临时工作区里跑一遍 sync_content.main()，验证：
  1. 长首段故事归档后，stories.html 的首段完整、展开段齐全（Issue #3 P1）；
  2. daily.html 正常追加当日一条；
  3. 重复运行不产生重复条目（幂等）。

第一次运行输出 OK，用修复前的 sync_content.py 运行时「首段完整」「全文可拼回」会 FAIL。
"""
import importlib.util
import json
import os
import re
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join("/tmp", "satoru-fictional-rehearsal")

spec = importlib.util.spec_from_file_location(
    "sc", os.path.join(REPO, "scripts", "sync_content.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

shutil.rmtree(TMP, ignore_errors=True)
shutil.copytree(REPO, TMP + "/site", ignore=shutil.ignore_patterns(".git", "evidence"))
os.makedirs(TMP + "/stories")
os.makedirs(TMP + "/data")

sc.WS = TMP
sc.SITE = TMP + "/site"
sc.SRC_STORIES = TMP + "/stories"
sc.DAILY_JSON = TMP + "/data/satoru-daily.json"
sc.OUT_STORIES = sc.SITE + "/stories"

DATE = "2099-01-01"
lead = ("虚构首段：这一段刻意超过八十八个字符，用来确认端到端跑一遍同步脚本之后，"
        "页面上仍然能读到首段结尾的每一个字，而不是被旧实现的八十八字符截断点吞掉。"
        "再补一句，使长度明显超过阈值；这一句是专门加长用的，请忽略它本身没有含义。")
paras = [lead, "虚构第二段：应当出现在展开区。", "虚构第三段：结束。"]
with open(os.path.join(sc.SRC_STORIES, DATE + ".md"), "w", encoding="utf-8") as fh:
    fh.write("# %s 端到端样例\n\n%s\n" % (DATE, "\n\n".join(paras)))
with open(sc.DAILY_JSON, "w", encoding="utf-8") as fh:
    json.dump({"date": DATE, "contacts": [
        {"kind": "normal", "time": "09:00", "summary": "虚构动态：早上在窗边翻笔记。"}]},
        fh, ensure_ascii=False)

sys.argv = ["sync_content.py", DATE]
sc.main()

html = open(os.path.join(sc.SITE, "stories.html"), encoding="utf-8").read()
block = re.search(r"<article class=\"story\".*?</article>", html, re.S).group(0)
got_lead = re.search(r"</h3>\s*<p>(.*?)</p>", block, re.S).group(1)
got_rest = re.findall(r"<p>(.*?)</p>",
                      re.search(r"<details>.*?</details>", block, re.S).group(0), re.S)
daily = open(os.path.join(sc.SITE, "daily.html"), encoding="utf-8").read()
daily_before = daily.count('id="daily-%s"' % DATE)

sys.argv = ["sync_content.py", DATE]
sc.main()
daily = open(os.path.join(sc.SITE, "daily.html"), encoding="utf-8").read()
daily_after = daily.count('id="daily-%s"' % DATE)

checks = [
    ("首段完整（%d 字符）" % len(got_lead), got_lead == lead),
    ("展开段数量 = %d" % len(got_rest), len(got_rest) == len(paras) - 1),
    ("全文可拼回", [got_lead] + got_rest == paras),
    ("归档写入 stories/%s.md" % DATE, os.path.exists(os.path.join(sc.OUT_STORIES, DATE + ".md"))),
    ("daily.html 首次写入 1 条", daily_before == 1),
    ("重复运行不重复写入（%d 条）" % daily_after, daily_after == 1),
]
ok = True
for name, res in checks:
    print("[%s] %s" % ("OK" if res else "FAIL", name))
    ok = ok and res
print("\n端到端演练：%s" % ("全部通过（虚构数据）" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
