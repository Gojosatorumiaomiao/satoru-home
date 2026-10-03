#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日常动态同步的「早退 / 闸门失败」回归测试（全部虚构数据，自包含）。

自包含：每个用例自己创建虚构禁词表，并把路径同时注入子进程环境变量
`SATORU_BLOCKLIST` 与驱动里的 `BLOCKLIST_FILE`；不读操作者本机词表，
也不依赖外部环境变量。因此在**未设置 SATORU_BLOCKLIST 的干净环境**可直接运行：

    python3 scripts/test_daily_sync_early_exit.py

覆盖：
  1. 当天没有 normal 条目      -> no-entry，页面与账本不变
  2. 临时页面缺少 daily 锚点   -> no-anchor，页面与账本不变
  3. 正常路径闸门分流（仅 summary 跳过 / 有 public_text 发布）与重跑幂等
  4. 禁词表缺失 / 为空 / 无法解析 -> 跳过并报告，页面与账本不变
  5. 命中禁词                  -> blocked，页面与账本不变
  6. 时刻缺失 / 空白 / 非法（非 HH:MM） -> invalid-time，页面与账本不变
     且不产生 `daily-<date>-` 空锚点；同状态里的合格条目照常发布

本脚本只在临时目录内造数据、只调用被测脚本的 main()，
不访问 /home/hyr/.openclaw/workspace 下的任何真实文件。
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

# 虚构禁词：不使用任何真实词表内容
FAKE_TERMS = "FICTIONSECRET\n虚构禁词甲\n"

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
mod.BLOCKLIST_FILE = BLOCKLIST
# 显式注入本用例自己的虚构词表路径（可能是刻意不存在 / 空 / 无法解析的路径）
os.environ["SATORU_BLOCKLIST"] = BLOCKLIST
sys.argv = ["sync_content.py", DATE, "--daily-only"]
sys.exit(mod.main())
'''

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name,
                         ("  —— " + detail) if detail else ""))


def make_case(page_body_inner, state_obj, with_anchors=True,
              blocklist_terms=FAKE_TERMS, blocklist_raw=None):
    """准备一个隔离的站点目录 + 本用例自己的虚构禁词表。

    返回 (root, site, state_path, driver, blocklist_path)。
    blocklist_terms=None 表示**不创建**词表（路径刻意不存在）；
    blocklist_raw 给 bytes 时按原字节写入（用于制造无法解析的词表）。
    """
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
    bl = os.path.join(root, "blocklist.txt")
    if blocklist_raw is not None:
        with open(bl, "wb") as f:
            f.write(blocklist_raw)
    elif blocklist_terms is not None:
        with open(bl, "w", encoding="utf-8") as f:
            f.write(blocklist_terms)
    else:
        bl = os.path.join(root, "no-such-blocklist.txt")   # 刻意不存在
    return root, site, state_path, driver, bl


def run(driver, site, state_path, blocklist_path):
    """在子进程里跑 main()，环境里显式注入本用例的词表路径。"""
    env = {k: v for k, v in os.environ.items() if k != "SATORU_BLOCKLIST"}
    env["SATORU_BLOCKLIST"] = blocklist_path
    src = SYNC
    code = (
        "SRC=%r\nSITE=%r\nSTATE=%r\nBLOCKLIST=%r\nDATE=%r\n"
        % (src, site, state_path, blocklist_path, DATE)
        + open(driver, encoding="utf-8").read()
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def page_bytes(site):
    with open(os.path.join(site, "daily.html"), "rb") as f:
        return f.read()


def read_log(site):
    p = os.path.join(site, "data", "daily-published.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))


def log_bytes(site):
    p = os.path.join(site, "data", "daily-published.json")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()


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


def parse(out, err):
    try:
        return json.loads(out), True
    except Exception:
        return None, False


def fail_case(name, rc, err):
    tail = err.strip().splitlines()[-1] if err.strip() else ""
    check(name, rc == 0, "rc=%d stderr=%s" % (rc, tail))


def setup_existing(site, date=DATE):
    """在页面与账本里预置一条**已发布**的旧条目，作为“失败时不得改动”的基线。

    预置块按真实渲染格式写（含 `<time>`），否则同步末尾的排序会因
    找不到 `<time>` 而抛异常，把“坏时刻”的失败原因混淆为 fixture 本身不平。
    """
    page = os.path.join(site, "daily.html")
    html = open(page, encoding="utf-8").read()
    block = ('    <article class="post" id="daily-%s-0600">\n'
             '      <time>%s · 06:00</time>\n'
             '      <p>已发布的旧条目</p>\n'
             '    </article>' % (date, date))
    html = html.replace("    " + D_START, "    " + D_START + "\n" + block, 1)
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(site, "data", "daily-published.json"), "w",
              encoding="utf-8") as f:
        json.dump({date: ["06:00"]}, f, ensure_ascii=False)


# ---------- 用例 1：当天没有任何 normal 条目（只有 thought） ----------
root, site, sp, drv, bl = make_case("", state(DATE, [
    contact("07:15", summary="内部想法甲", kind="thought"),
    contact("09:00", summary="内部想法乙", kind="thought"),
]))
before = page_bytes(site)
rc, out, err = run(drv, site, sp, bl)
fail_case("1a 仅 thought：进程正常退出（rc=0）", rc, err)
rep, ok_json = parse(out, err)
check("1b 仅 thought：仍输出 JSON 报告", ok_json, "" if ok_json else err.strip()[-200:])
if ok_json:
    steps = " | ".join(rep.get("steps", []))
    check("1c 仅 thought：报告为无可发布成稿文案", "没有可发布的公共成稿" in steps, steps)
    check("1d 仅 thought：未写 daily_added", "daily_added" not in rep, steps)
check("1e 仅 thought：页面未被修改", page_bytes(site) == before)
check("1f 仅 thought：未写记账文件", read_log(site) is None, str(read_log(site)))
shutil.rmtree(root, ignore_errors=True)

# ---------- 用例 2：页面缺少 daily 锚点 ----------
root, site, sp, drv, bl = make_case("", state(DATE, [
    contact("11:15", public="虚构公开文案"),
]), with_anchors=False)
before = page_bytes(site)
rc, out, err = run(drv, site, sp, bl)
fail_case("2a 缺锚点：进程正常退出（rc=0）", rc, err)
rep2, ok2 = parse(out, err)
check("2b 缺锚点：仍输出 JSON 报告", ok2, err.strip()[-200:] if not ok2 else "")
if ok2:
    steps = " | ".join(rep2.get("steps", []))
    check("2c 缺锚点：报告为 no-anchor 文案", "daily.html 缺少锚点" in steps, steps)
check("2d 缺锚点：页面未被修改", page_bytes(site) == before)
check("2e 缺锚点：未写记账文件", read_log(site) is None, str(read_log(site)))
shutil.rmtree(root, ignore_errors=True)

# ---------- 用例 3 / 4：正常路径 — 闸门分流与重跑幂等 ----------
# 闸门改为「有 public_text 且扫描通过才发布」后，仅有 summary 的条目被跳过，
# 不再计入 unscreened_internal。每次运行最多新增 1 条（max_new=1）。
root, site, sp, drv, bl = make_case("", state(DATE, [
    contact("08:15", summary="仅内部摘要，无公开成稿"),      # 应被跳过
    contact("14:15", public="明确标记的公开成稿"),           # 应被发布
]))
rc, out, err = run(drv, site, sp, bl)
rep3, ok3 = parse(out, err)
fail_case("3a 正常路径：进程正常退出（rc=0）", rc, err)
check("3b 正常路径：输出 JSON 报告", ok3, err.strip()[-200:] if not ok3 else "")
if ok3:
    check("3c 正常路径 · 第一次运行新增 1 条（每次上限一条）",
          rep3.get("daily_added") == 1,
          "daily_added=%r" % rep3.get("daily_added"))
check("3d 正常路径：仅有 summary 的条目被跳过并点名 no-public-text",
      ok3 and any(s.get("time") == "08:15" and s.get("reason") == "no-public-text"
                  for s in (rep3.get("skipped") or [])),
      "skipped=%r" % (rep3.get("skipped") if ok3 else None))
check("3e 正常路径：跳过原因不含命中正文",
      ok3 and all(set(s.keys()) <= {"time", "reason"}
                  for s in (rep3.get("skipped") or [])),
      "skipped=%r" % (rep3.get("skipped") if ok3 else None))
log3 = read_log(site)
check("3f 正常路径 · 第一次运行记账只含 14:15",
      log3 == {DATE: ["14:15"]}, str(log3))

rc, out, err = run(drv, site, sp, bl)
rep3b, ok3b = parse(out, err)
check("3g 正常路径 · 第二次运行不重复新增",
      rc == 0 and ok3b and rep3b.get("daily_added") == 0,
      "rc=%d daily_added=%r" % (rc, rep3b.get("daily_added") if ok3b else None))
log3b = read_log(site)
check("3h 正常路径 · 两次运行后记账仍只有 14:15",
      log3b == {DATE: ["14:15"]}, str(log3b))
page3 = page_bytes(site).decode("utf-8")
check("3i 正常路径 · 页面只含被发布的 14:15 锚点",
      page3.count('id="daily-%s-1415"' % DATE) == 1
      and page3.count('id="daily-%s-0815"' % DATE) == 0,
      "0815=%d 1415=%d" % (page3.count('id="daily-%s-0815"' % DATE),
                            page3.count('id="daily-%s-1415"' % DATE)))

rc, out, err = run(drv, site, sp, bl)
rep4, ok4 = parse(out, err)
check("4a 重跑：进程正常退出（rc=0）", rc == 0, "rc=%d" % rc)
check("4b 重跑：新增 0 条", ok4 and rep4.get("daily_added") == 0,
      "daily_added=%r" % (rep4.get("daily_added") if ok4 else None))
check("4c 重跑：页面条目数不变",
      page3.count('class="post"') == page_bytes(site).decode("utf-8").count('class="post"'))
check("4d 重跑：记账不变", read_log(site) == log3b, str(read_log(site)))
shutil.rmtree(root, ignore_errors=True)

# ---------- 用例 5–8：闸门无法判定 / 命中禁词时，页面与账本必须不变 ----------
FAIL_CASES = [
    ("5", "禁词表缺失", dict(blocklist_terms=None),
     "虚构公开成稿", "no-blocklist"),
    ("6", "禁词表为空（只有注释）", dict(blocklist_terms="# 只有注释\n\n"),
     "虚构公开成稿", "blocklist-empty"),
    ("7", "禁词表无法解析（非法 UTF-8）",
     dict(blocklist_terms=None, blocklist_raw=b"\xff\xfe\x00\xff"),
     "虚构公开成稿", "blocklist-error"),
    ("8", "公开稿命中虚构禁词", dict(blocklist_terms=FAKE_TERMS),
     "公开稿里含虚构禁词甲", "blocked"),
]
for tag, label, blkw, public_text, want_reason in FAIL_CASES:
    root, site, sp, drv, bl = make_case(
        "", state(DATE, [contact("09:00", summary="内部摘要", public=public_text)]),
        **blkw)
    setup_existing(site)
    page_before, log_before = page_bytes(site), log_bytes(site)
    rc, out, err = run(drv, site, sp, bl)
    fail_case("%sa %s：进程正常退出（rc=0）" % (tag, label), rc, err)
    rep, ok = parse(out, err)
    check("%sb %s：输出 JSON 报告" % (tag, label), ok,
          err.strip()[-200:] if not ok else "")
    got = None
    if ok:
        got = [s.get("reason") for s in (rep.get("skipped") or [])
               if s.get("time") == "09:00"]
    check("%sc %s：09:00 被跳过，原因以 %s 开头" % (tag, label, want_reason),
          bool(got) and got[0].startswith(want_reason), "skipped=%r" % (got,))
    check("%sd %s：新增 0 条" % (tag, label), ok and rep.get("daily_added") in (0, None),
          "daily_added=%r" % (rep.get("daily_added") if ok else None))
    check("%se %s：页面逐字节不变" % (tag, label), page_bytes(site) == page_before)
    check("%sf %s：账本逐字节不变" % (tag, label), log_bytes(site) == log_before)
    shutil.rmtree(root, ignore_errors=True)

# ---------- 用例 9–13：时刻缺失 / 空白 / 非法 -> invalid-time（页面与账本不变） ----------
# 回归背景：闸门改写曾丢掉 main 对 `time` 的校验，坏时刻会以 ("", public_text)
# 继续进入同步，生成 `daily-YYYY-MM-DD-` 空锚点并把空串写进发布账本。
# 以下每例都预置一条已发布旧条目作为“失败时不得改动”的基线。
BAD_TIMES = [
    ("9", "时刻键缺失", None),
    ("10", "时刻为空串", ""),
    ("11", "时刻非法（25:99）", "25:99"),
    ("12", "时刻非法（缺冒号，写法 9）", "9"),
]
for tag, label, bad_t in BAD_TIMES:
    c = contact("10:15", summary="内部摘要", public="明确标记的合格公开稿")
    if bad_t is None:
        c.pop("time", None)          # 整键缺失
        seen_t = ""
    else:
        c["time"] = bad_t
        seen_t = bad_t
    root, site, sp, drv, bl = make_case("", state(DATE, [c]))
    setup_existing(site)
    page_before, log_before = page_bytes(site), log_bytes(site)
    rc, out, err = run(drv, site, sp, bl)
    fail_case("%sa %s：进程正常退出（rc=0）" % (tag, label), rc, err)
    rep, ok = parse(out, err)
    check("%sb %s：输出 JSON 报告" % (tag, label), ok,
          err.strip()[-200:] if not ok else "")
    got = None
    if ok:
        got = [s.get("reason") for s in (rep.get("skipped") or [])
               if s.get("time") == seen_t]
    check("%sc %s：该条被跳过，reason=invalid-time" % (tag, label),
          bool(got) and got[0] == "invalid-time",
          "skipped=%r" % (rep.get("skipped") if ok else None))
    check("%sd %s：跳过原因不含正文（只有 time/reason）" % (tag, label),
          ok and all(set(s.keys()) <= {"time", "reason"}
                     for s in (rep.get("skipped") or [])),
          "skipped=%r" % (rep.get("skipped") if ok else None))
    check("%se %s：新增 0 条" % (tag, label),
          ok and rep.get("daily_added") in (0, None),
          "daily_added=%r" % (rep.get("daily_added") if ok else None))
    check("%sf %s：页面逐字节不变" % (tag, label), page_bytes(site) == page_before)
    check("%sg %s：账本逐字节不变" % (tag, label), log_bytes(site) == log_before)
    after_html = page_bytes(site).decode("utf-8")
    check("%sh %s：未产生 daily-%s- 空锚点" % (tag, label, DATE),
          ('id="daily-%s-"' % DATE) not in after_html,
          "出现空锚点" if ('id="daily-%s-"' % DATE) in after_html else "")
    log_now = read_log(site)
    check("%si %s：账本未写入空串" % (tag, label),
          not any("" in v for v in (log_now or {}).values()),
          str(log_now))
    shutil.rmtree(root, ignore_errors=True)

# ---------- 用例 13：坏时刻的兄弟条目不影响同状态里的合格条目 ----------
root, site, sp, drv, bl = make_case("", state(DATE, [
    contact("25:99", summary="内部摘要", public="坏时刻的公开稿"),   # 应被跳过
    contact("10:15", summary="内部摘要", public="合格公开稿"),        # 应被发布
]))
rc, out, err = run(drv, site, sp, bl)
fail_case("13a 坏时刻旁证：进程正常退出（rc=0）", rc, err)
rep13, ok13 = parse(out, err)
check("13b 坏时刻旁证：输出 JSON 报告", ok13,
      err.strip()[-200:] if not ok13 else "")
check("13c 坏时刻旁证：新增的是 10:15（坏时刻不占名额）",
      ok13 and rep13.get("daily_added") == 1,
      "daily_added=%r" % (rep13.get("daily_added") if ok13 else None))
check("13d 坏时刻旁证：25:99 被记为 invalid-time",
      ok13 and any(s.get("reason") == "invalid-time" for s in (rep13.get("skipped") or [])),
      "skipped=%r" % (rep13.get("skipped") if ok13 else None))
log13 = read_log(site)
check("13e 坏时刻旁证：账本只记 10:15", log13 == {DATE: ["10:15"]}, str(log13))
page13 = page_bytes(site).decode("utf-8")
check("13f 坏时刻旁证：页面只有 1015 锚点，无空锚点",
      page13.count('id="daily-%s-1015"' % DATE) == 1
      and ('id="daily-%s-"' % DATE) not in page13,
      "1015=%d" % page13.count('id="daily-%s-1015"' % DATE))
shutil.rmtree(root, ignore_errors=True)

# ---------- 汇总 ----------
bad = [n for n, ok, _ in results if not ok]
print("\n%d 项检查：%d OK / %d FAIL" % (len(results), len(results) - len(bad), len(bad)))
if bad:
    print("失败项：" + "；".join(bad))
sys.exit(1 if bad else 0)
