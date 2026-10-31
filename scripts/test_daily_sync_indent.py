#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日常动态同步：新增日期不得改写历史条目格式（全虚构数据）。

背景（Issue #3 复核 2026-10-11 午后 · P1）：
    sync_daily() 旧实现用正则只捕获 <article>…</article>，块与块之间的
    行首缩进落在匹配之外；重组时又用 "\n".join 重排全部日期，于是每次
    新增一条动态都会把此前所有历史条目的行首缩进（例如六个空格）抹成
    行首，产生只改格式、不改内容的 diff，并放大与其它改 daily.html 分支
    的冲突面。

本测试断言（全部使用虚构日期与虚构文案，不读取任何真实文件）：
    1. 追加新日期后，两条旧日期条目（连同其行首缩进）逐字节不变；
    2. 新条目按既有规范渲染为六空格行首缩进的 <article>；
    3. 页面里不再出现行首 <article>，即没有纯缩进变化；
    4. 第二次运行完全幂等：页面逐字节不变。
    另覆盖一种「历史条目本就是行首 <article>」的混合格式，断言同样不被改写。

用法：python3 scripts/test_daily_sync_indent.py
退出码 0 表示全部通过。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SYNC = os.path.join(HERE, "sync_content.py")

D_START = "<!-- daily:list:start -->"
D_END = "<!-- daily:list:end -->"

NEW_DATE = "2099-03-05"

# 驱动脚本：按路径加载被测模块，把绝对路径常量改到临时目录，再跑 main()。
DRIVER = '''# -*- coding: utf-8 -*-
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("sync_content", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

mod.SITE = SITE
mod.SRC_STORIES = os.path.join(SITE, "src")
mod.DAILY_JSON = STATE
mod.PUBLISHED_LOG = os.path.join(SITE, "data", "daily-published.json")
sys.argv = ["sync_content.py", DATE, "--daily-only"]
sys.exit(mod.main())
'''

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name,
                         ("  —— " + detail) if detail else ""))


def article_blocks(region):
    """把锚点之间的原文拆成 [(行首缩进, 块, 锚点)]，与生成器同口径。"""
    out = []
    prev = 0
    for m in re.finditer(r'<article class="post" id="([^"]+)">.*?</article>',
                         region, re.S):
        out.append((region[prev:m.start()], m.group(0), m.group(1)))
        prev = m.end()
    return out


def region_of(text):
    i = text.find(D_START)
    j = text.find(D_END)
    return text[i + len(D_START):j]


def make_case(old_region):
    root = tempfile.mkdtemp(prefix="dailysyncindent-")
    site = os.path.join(root, "site")
    os.makedirs(os.path.join(site, "data"))
    os.makedirs(os.path.join(site, "src"))
    page = ("<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>t</title></head>\n<body>\n    <main>\n"
            "    " + D_START + old_region + D_END + "\n    </main>\n</body></html>\n")
    with open(os.path.join(site, "daily.html"), "w", encoding="utf-8") as f:
        f.write(page)
    state_path = os.path.join(root, "state.json")
    state = {"date": NEW_DATE, "schema_version": 1, "timezone": "Asia/Shanghai",
             "character": "虚构",
             "contacts": [{"kind": "normal", "slot": "s", "time": "02:15",
                           "public_text": "虚构新增公开文案，用于断言新条目格式。"}]}
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    driver = os.path.join(root, "driver.py")
    with open(driver, "w", encoding="utf-8") as f:
        f.write(DRIVER)
    return root, site, state_path, driver


def run(driver, site, state_path):
    code = ("SRC=%r\nSITE=%r\nSTATE=%r\nDATE=%r\n" % (SYNC, site, state_path,
                                                       NEW_DATE)
            + open(driver, encoding="utf-8").read())
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True)
    return p.returncode, p.stdout, p.stderr


def page_text(site):
    return open(os.path.join(site, "daily.html"), encoding="utf-8").read()


def old_region_normal():
    return ("\n"
            '      <article class="post" id="daily-2099-03-01-0215">\n'
            "        <time>2099-03-01 · 02:15</time>\n"
            "        <p>虚构旧条目甲，用于断言格式不变。</p>\n"
            "      </article>\n"
            '      <article class="post" id="daily-2099-03-02-0215">\n'
            "        <time>2099-03-02 · 02:15</time>\n"
            "        <p>虚构旧条目乙，用于断言格式不变。</p>\n"
            "      </article>\n    ")


def old_region_mixed():
    # 混合格式：甲为六空格缩进，乙已被历史运行抹成行首 <article>。
    return ("\n"
            '      <article class="post" id="daily-2099-03-01-0215">\n'
            "        <time>2099-03-01 · 02:15</time>\n"
            "        <p>虚构旧条目甲，用于断言格式不变。</p>\n"
            "      </article>\n"
            '<article class="post" id="daily-2099-03-02-0215">\n'
            "        <time>2099-03-02 · 02:15</time>\n"
            "        <p>虚构旧条目乙，用于断言格式不变。</p>\n"
            "      </article>\n    ")


def run_case(label, old_region, expect_line_start):
    root, site, sp, drv = make_case(old_region)
    before = page_text(site)
    before_blocks = {aid: (lead, block)
                     for lead, block, aid in article_blocks(region_of(before))}

    rc, out, err = run(drv, site, sp)
    check("%s · 进程正常退出（rc=0）" % label, rc == 0,
          "rc=%d stderr=%s" % (rc, err.strip().splitlines()[-1] if err.strip() else ""))

    after = page_text(site)
    after_blocks = {aid: (lead, block)
                    for lead, block, aid in article_blocks(region_of(after))}

    # 1) 旧条目逐字节不变（含行首缩进）
    for aid, pair in before_blocks.items():
        check("%s · 旧条目 %s 逐字节不变（含行首缩进）" % (label, aid),
              after_blocks.get(aid) == pair,
              "before lead=%r / after lead=%r"
              % (pair[0], after_blocks.get(aid, (None,))[0]))

    # 2) 新条目已加入，且为六空格行首缩进
    check("%s · 新条目已加入" % label,
          any(aid == "daily-%s-0215" % NEW_DATE for aid in after_blocks))
    check("%s · 新条目六空格行首缩进" % label,
          '      <article class="post" id="daily-%s-0215">' % NEW_DATE in after)
    check("%s · 新条目排在旧条目之前" % label,
          after.find('id="daily-%s-0215"' % NEW_DATE)
          < after.find('id="daily-2099-03-01-0215"'))

    # 3) 无行首 <article>（不产生纯缩进变化）
    line_start = re.findall(r'(?m)^<article', after)
    if expect_line_start:
        check("%s · 历史行首 <article> 原样保留（%d 处）" % (label, expect_line_start),
              len(line_start) == expect_line_start,
              "行首 <article> 实际 %d 处" % len(line_start))
    else:
        check("%s · 页面无行首 <article>" % label, len(line_start) == 0,
              "行首 <article> 实际 %d 处" % len(line_start))

    # 4) 幂等：第二次运行逐字节不变
    rc2, out2, err2 = run(drv, site, sp)
    check("%s · 第二次运行 rc=0" % label, rc2 == 0,
          "rc=%d stderr=%s" % (rc2, err2.strip().splitlines()[-1] if err2.strip() else ""))
    check("%s · 第二次运行页面逐字节不变" % label, page_text(site) == after)

    shutil.rmtree(root, ignore_errors=True)


run_case("规范缩进", old_region_normal(), 0)
run_case("混合格式", old_region_mixed(), 1)

bad = [n for n, ok, _ in results if not ok]
print("\n%d 项检查：%d OK / %d FAIL" % (len(results), len(results) - len(bad), len(bad)))
if bad:
    print("失败项：" + "；".join(bad))
sys.exit(1 if bad else 0)
