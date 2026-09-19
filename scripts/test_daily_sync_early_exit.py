#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日常动态同步的「早退路径」回归测试（全部虚构数据，不读本机真实状态）。

覆盖 Issue #1 复核指出的 C 方案回归：
    action, added, updated, unscreened = sync_daily(...)
正常路径返回四项，但 sync_daily() 的两个早退仍只返回三项
（"no-entry" / "no-anchor"），调用方在生成报告前就会因解包失败抛异常。

验收（对应复核原始条目）：
  1. 当天 contacts 为空或只有 thought 时，进程正常退出，报告 no-entry，
     不改页面、不写记账。
  2. 临时页面缺少 daily 锚点时，进程正常退出并报告 no-anchor。
  3. 一条仅有 summary、一条有 public_text 的正常路径，
     只有前者计入 unscreened_internal。
  4. 重跑不重复。

本脚本只在临时目录内造数据、只调用被测脚本的 main()，
不访问 /home/hyr/.openclaw/workspace 下的任何真实文件。
用法：python3 scripts/test_daily_sync_early_exit.py
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

DATE = "2099-03-05"          # 虚构日期，避开任何真实数据
D_START = "<!-- daily:list:start -->"
D_END = "<!-- daily:list:end -->"

PAGE_TMPL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>t</title></head>
<body>
    <main>
%s
%s
%s
    </main>
</body></html>
"""

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


def make_case(page_body_inner, state_obj, with_anchors=True):
    """准备一个隔离的站点目录，返回 (root, site, state_path)。"""
    root = tempfile.mkdtemp(prefix="dailyearly-")
    site = os.path.join(root, "site")
    os.makedirs(os.path.join(site, "data"))
    os.makedirs(os.path.join(site, "src"))
    if with_anchors:
        page = PAGE_TMPL % ("    " + D_START, page_body_inner, "    " + D_END)
    else:
        page = PAGE_TMPL % ("", page_body_inner, "")
    with open(os.path.join(site, "daily.html"), "w", encoding="utf-8") as f:
        f.write(page)
    state_path = os.path.join(root, "state.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state_obj, f, ensure_ascii=False)
    driver = os.path.join(root, "driver.py")
    with open(driver, "w", encoding="utf-8") as f:
        f.write(DRIVER)
    return root, site, state_path, driver


def run(driver, site, state_path):
    env = dict(os.environ)
    src = SYNC
    code = (
        "SRC=%r\nSITE=%r\nSTATE=%r\nDATE=%r\n" % (src, site, state_path, DATE)
        + open(driver, encoding="utf-8").read()
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def page_text(site):
    return open(os.path.join(site, "daily.html"), encoding="utf-8").read()


def read_log(site):
    p = os.path.join(site, "data", "daily-published.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))


def state(date, contacts):
    return {"date": date, "schema_version": 1, "timezone": "Asia/Shanghai",
            "character": "虚构", "contacts": contacts}


def contact(t, summary=None, public=None, kind="normal"):
    c = {"kind": kind, "slot": "s", "time": t}
    if summary is not None:
        c["summary"] = summary
    if public is not None:
        c["public_text"] = public
    return c


# ---------- 用例 1：当天没有任何 normal 条目（只有 thought） ----------
root, site, sp, drv = make_case("", state(DATE, [
    contact("07:15", summary="内部想法甲", kind="thought"),
    contact("09:00", summary="内部想法乙", kind="thought"),
]))
before = page_text(site)
rc, out, err = run(drv, site, sp)
check("1a 仅 thought：进程正常退出（rc=0）", rc == 0,
      "rc=%d stderr=%s" % (rc, err.strip().splitlines()[-1] if err.strip() else ""))
ok_json = False
rep = None
try:
    rep = json.loads(out)
    ok_json = True
except Exception as e:
    ok_json = False
check("1b 仅 thought：仍输出 JSON 报告", ok_json, "" if ok_json else err.strip()[-200:])
if ok_json:
    steps = " | ".join(rep.get("steps", []))
    check("1c 仅 thought：报告为无可发布成稿文案", "没有可发布的公共成稿" in steps,
          steps)
    check("1d 仅 thought：未写 daily_added",
          "daily_added" not in rep, steps)
check("1e 仅 thought：页面未被修改", page_text(site) == before)
check("1f 仅 thought：未写记账文件", read_log(site) is None,
      str(read_log(site)))
shutil.rmtree(root, ignore_errors=True)

# ---------- 用例 2：页面缺少 daily 锚点 ----------
root, site, sp, drv = make_case("", state(DATE, [
    contact("11:15", public="虚构公开文案"),
]), with_anchors=False)
before = page_text(site)
rc, out, err = run(drv, site, sp)
check("2a 缺锚点：进程正常退出（rc=0）", rc == 0,
      "rc=%d stderr=%s" % (rc, err.strip().splitlines()[-1] if err.strip() else ""))
rep2, ok2 = None, False
try:
    rep2 = json.loads(out)
    ok2 = True
except Exception:
    pass
check("2b 缺锚点：仍输出 JSON 报告", ok2, err.strip()[-200:] if not ok2 else "")
if ok2:
    steps = " | ".join(rep2.get("steps", []))
    check("2c 缺锚点：报告为 no-anchor 文案", "daily.html 缺少锚点" in steps, steps)
check("2d 缺锚点：页面未被修改", page_text(site) == before)
check("2e 缺锚点：未写记账文件", read_log(site) is None, str(read_log(site)))
shutil.rmtree(root, ignore_errors=True)

# ---------- 用例 3 / 4：正常路径 — 闸门分流与重跑幂等 ----------
# 闸门改为「有 public_text 且扫描通过才发布」后，仅有 summary 的条目被跳过，
# 不再计入 unscreened_internal。每次运行最多新增 1 条（max_new=1）。
root, site, sp, drv = make_case("""绝密代号
""", state(DATE, [
    contact("08:15", summary="仅内部摘要，无公开成稿"),      # 应被跳过
    contact("14:15", public="明确标记的公开成稿"),           # 应被发布
]))


def parse(out, err):
    try:
        return json.loads(out), True
    except Exception:
        return None, False


rc, out, err = run(drv, site, sp)
rep3, ok3 = parse(out, err)
check("3a 正常路径：进程正常退出（rc=0）", rc == 0,
      "rc=%d stderr=%s" % (rc, err.strip().splitlines()[-1] if err.strip() else ""))
check("3b 正常路径：输出 JSON 报告", ok3, err.strip()[-200:] if not ok3 else "")
if ok3:
    check("3c 正常路径 · 第一次运行新增 1 条（每次上限一条）",
          rep3.get("daily_added") == 1,
          "daily_added=%r" % rep3.get("daily_added"))
check("3d 正常路径：仅有 summary 的条目被跳过并点名 no-public-text",
      ok3 and any(s.get("time") == "08:15" and s.get("reason") == "no-public-text"
                  for s in (rep3.get("skipped") or [])),
      "skipped=%r" % (rep3.get("skipped") if ok3 else None))
log3 = read_log(site)
check("3f 正常路径 · 第一次运行记账只含 14:15",
      log3 == {DATE: ["14:15"]}, str(log3))

rc, out, err = run(drv, site, sp)
rep3b, ok3b = parse(out, err)
check("3g 正常路径 · 第二次运行不重复新增",
      rc == 0 and ok3b and rep3b.get("daily_added") == 0,
      "rc=%d daily_added=%r" % (rc, rep3b.get("daily_added") if ok3b else None))
log3b = read_log(site)
check("3h 正常路径 · 两次运行后记账仍只有 14:15",
      log3b == {DATE: ["14:15"]}, str(log3b))
page3 = page_text(site)
check("3i 正常路径 · 页面只含被发布的 14:15 锚点",
      page3.count('id="daily-%s-1415"' % DATE) == 1
      and page3.count('id="daily-%s-0815"' % DATE) == 0,
      "0815=%d 1415=%d" % (page3.count('id="daily-%s-0815"' % DATE),
                            page3.count('id="daily-%s-1415"' % DATE)))

rc, out, err = run(drv, site, sp)
rep4, ok4 = parse(out, err)
check("4a 重跑：进程正常退出（rc=0）", rc == 0, "rc=%d" % rc)
check("4b 重跑：新增 0 条", ok4 and rep4.get("daily_added") == 0,
      "daily_added=%r" % (rep4.get("daily_added") if ok4 else None))
check("4c 重跑：页面条目数不变",
      page_text(site).count('class="post"') == page3.count('class="post"'))
check("4d 重跑：记账不变", read_log(site) == log3b, str(read_log(site)))
shutil.rmtree(root, ignore_errors=True)

# ---------- 汇总 ----------
bad = [n for n, ok, _ in results if not ok]
print("\n%d 项检查：%d OK / %d FAIL" % (len(results), len(results) - len(bad), len(bad)))
if bad:
    print("失败项：" + "；".join(bad))
sys.exit(1 if bad else 0)
