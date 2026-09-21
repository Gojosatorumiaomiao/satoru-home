#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开文本闸门的路径测试（全部虚构数据，不读本机真实状态或禁词表）。

闸门规则（用户指定）：
    明确公开的 public_text → 扫描 → 发布
    缺少公开成稿 / 命中禁词 / 无法扫描 → 跳过并报告，
    不消耗发布名额、不影响已有页面。

验收：
  1. 有 public_text 且扫描通过            -> 发布
  2. 只有 summary、没有 public_text       -> 跳过，reason=no-public-text
  3. public_text 命中禁词                 -> 跳过，reason=blocked
  4. 禁词表缺失 / 为空 / 不可读           -> 跳过，reason=no-blocklist 等
  5. 被跳过的条目不推进游标、不消耗当日名额
  6. 后续补上 public_text 后能正常发布（不被卡死）
  7. 重跑幂等：不重复发布

本脚本只在临时目录内造数据，只调用被测脚本的函数；
禁词表用临时文件，不访问工作区下的真实文件。
用法：python3 scripts/test_public_text_gate.py
"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sc", os.path.join(HERE, "sync_content.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s %s" % (name, extra))


def make_env(tmp, blocklist_terms=None, write_blocklist=True):
    """在 tmp 下建一个最小站点并接好路径。返回每日页路径。"""
    if blocklist_terms is not None:
        bl = os.path.join(tmp, "block.txt")
        with open(bl, "w", encoding="utf-8") as fh:
            fh.write(blocklist_terms)
        os.environ["SATORU_BLOCKLIST"] = bl
        sc.BLOCKLIST_FILE = bl
    elif not write_blocklist:
        os.environ.pop("SATORU_BLOCKLIST", None)
        sc.BLOCKLIST_FILE = os.path.join(tmp, "does-not-exist.txt")
    sc.PUBLISHED_LOG = os.path.join(tmp, "published.json")
    page = os.path.join(tmp, "daily.html")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write("<!-- daily:list:start -->\n<!-- daily:list:end -->\n")
    return page


def ids(page):
    with open(page, encoding="utf-8") as fh:
        return sorted(re.findall(r'id="(daily-[^"]*)"', fh.read()))


def state(contacts):
    return {"date": "2026-09-19", "contacts": contacts}


def run():
    base = tempfile.mkdtemp(prefix="gate-test-")

    # ---- 1/2/3/4：扫描结果分流 ----
    print("[1-4] 扫描结果分流")
    tmp = os.path.join(base, "scan")
    os.makedirs(tmp)
    page = make_env(tmp, "绝密代号\n私密电话13700000000\n")
    st = state([
        {"time": "02:15", "kind": "normal", "summary": "内部A",
         "public_text": "公开稿A：今天天气不错"},
        {"time": "05:15", "kind": "normal", "summary": "只有内部摘要"},
        {"time": "08:15", "kind": "normal", "summary": "内部C",
         "public_text": "公开稿C：含绝密代号"},
        {"time": "11:15", "kind": "normal", "summary": "内部D",
         "public_text": "公开稿D：私密电话13700000000"},
    ])
    action, added, updated, skipped = sc.sync_daily(page, "2026-09-19", st)
    reasons = {s["time"]: s["reason"] for s in skipped}
    check("扫描通过的条目被发布", ids(page) == ["daily-2026-09-19-0215"], str(ids(page)))
    check("只有 summary 的条目被跳过", reasons.get("05:15") == "no-public-text", str(reasons))
    check("命中禁词的条目被跳过(1)", reasons.get("08:15") == "blocked", str(reasons))
    check("命中禁词的条目被跳过(2)", reasons.get("11:15") == "blocked", str(reasons))
    check("跳过项不计入 added", added == 1, "added=%s" % added)

    # ---- 5：跳过不消耗名额 ----
    print("[5] 被跳过的条目不消耗名额、不推进游标")
    before = len(ids(page))
    for _ in range(3):
        sc.sync_daily(page, "2026-09-19", st)
    check("重复运行不重复发布", len(ids(page)) == before, str(ids(page)))

    # ---- 6：后续补 public_text 能发布 ----
    print("[6] 上游补上 public_text 后不被卡住")
    st2 = state([
        {"time": "02:15", "kind": "normal", "summary": "内部A",
         "public_text": "公开稿A：今天天气不错"},
        {"time": "05:15", "kind": "normal", "summary": "内部B",
         "public_text": "公开稿B：后续补上的公开发布稿"},
    ])
    action, added, updated, skipped = sc.sync_daily(page, "2026-09-19", st2)
    check("补上后可发布", "daily-2026-09-19-0515" in ids(page), str(ids(page)))

    # ---- 4：禁词表缺失/为空 -> 无法扫描，跳过 ----
    print("[4] 禁词表缺失或为空 -> 跳过并报告")
    tmp = os.path.join(base, "nobl")
    os.makedirs(tmp)
    page2 = make_env(tmp, write_blocklist=False)
    st3 = state([
        {"time": "02:15", "kind": "normal", "summary": "A", "public_text": "公开稿A"},
    ])
    action, added, updated, skipped = sc.sync_daily(page2, "2026-09-19", st3)
    check("禁词表缺失时跳过", added == 0 and ids(page2) == [], str(ids(page2)))
    check("原因报告为 no-blocklist",
          skipped and skipped[0]["reason"] == "no-blocklist", str(skipped))

    tmp = os.path.join(base, "emptybl")
    os.makedirs(tmp)
    page3 = make_env(tmp, "# 只有注释\n\n")
    action, added, updated, skipped = sc.sync_daily(page3, "2026-09-19", st3)
    check("禁词表为空时跳过", added == 0, "added=%s" % added)
    check("原因报告为 blocklist-empty",
          skipped and skipped[0]["reason"] == "blocklist-empty", str(skipped))

    # ---- 7：幂等 ----
    print("[7] 重跑幂等")
    tmp = os.path.join(base, "idem")
    os.makedirs(tmp)
    page4 = make_env(tmp, "绝密\n")
    st4 = state([{"time": "02:15", "kind": "normal", "summary": "A",
                  "public_text": "公开稿A"}])
    sc.sync_daily(page4, "2026-09-19", st4)
    n1 = len(ids(page4))
    sc.sync_daily(page4, "2026-09-19", st4)
    check("重跑不重复", len(ids(page4)) == n1 == 1, str(ids(page4)))

    # ---- 8：规范化匹配（全角 / 零宽绕过） ----
    # 复核要求：全角与零宽写法不能再绕过；命中只给类别，不回显正文；
    # 发布正文保留原文，不因扫描而改写。
    print("[8] 全角 / 零宽绕过会被拦截；发布正文保留原文")
    tmp = os.path.join(base, "norm")
    os.makedirs(tmp)
    ZW = "\u200b"
    # 词表三条：一条 ASCII 虚构词、一条中虚构词、一条**全角**写的虚构词
    page5 = make_env(tmp, "FICTIONSECRET\n虚构禁词乙\nＦＵＬＬＷＩＤＴＨ\n")
    st5 = state([
        {"time": "01:00", "kind": "normal", "summary": "内部1",
         "public_text": "全角绕过：ＦＩＣＴＩＯＮＳＥＣＲＥＴ"},
        {"time": "02:00", "kind": "normal", "summary": "内部2",
         "public_text": "零宽绕过：虚" + ZW + "构禁词乙"},
        {"time": "03:00", "kind": "normal", "summary": "内部3",
         "public_text": "词表全角：fullwidth"},
        {"time": "04:00", "kind": "normal", "summary": "内部4",
         "public_text": "正常公开稿：天气不错"},
    ])
    action, added, updated, skipped = sc.sync_daily(page5, "2026-09-19", st5)
    reasons = {s["time"]: s["reason"] for s in skipped}
    check("全角写法（ＦＩＣＴＩＯＮＳＥＣＲＥＴ）被拦下",
          reasons.get("01:00") == "blocked", str(reasons))
    check("插入零宽的相同写法被拦下",
          reasons.get("02:00") == "blocked", str(reasons))
    check("词表里的全角词也能匹配 ASCII 正文",
          reasons.get("03:00") == "blocked", str(reasons))
    check("未被绕过的正常公开稿照常发布",
          ids(page5) == ["daily-2026-09-19-0400"] and added == 1, str(ids(page5)))
    check("命中项只给类别，不回显正文或禁词",
          all(set(s.keys()) <= {"time", "reason"} for s in skipped)
          and all(v == "blocked" for k, v in reasons.items()),
          str(skipped))

    # 发布正文保留原文：含零宽与全角的合格稿在页面上逐字保留
    tmp = os.path.join(base, "verbatim")
    os.makedirs(tmp)
    page6 = make_env(tmp, "FICTIONSECRET\n")
    original = "公开稿：全角ＡＢＣ" + ZW + "正常叙述"
    normalized = "公开稿：全角ABC正常叙述"
    st6 = state([{"time": "02:15", "kind": "normal", "summary": "A",
                 "public_text": original}])
    action, added, updated, skipped = sc.sync_daily(page6, "2026-09-19", st6)
    page6_text = open(page6, encoding="utf-8").read()
    check("合格公开稿被发布", added == 1 and ids(page6) == ["daily-2026-09-19-0215"],
          str(ids(page6)))
    check("发布正文逐字保留原文（含零宽与全角）", original in page6_text)
    check("页面未被写入规范化副本", normalized not in page6_text)

    # ---- 9：通用类型检测（邮箱/手机/凭据/本机路径/IP/内部标记） ----
    # 全部为虚构样例。要求：该拦的拦下、不该拦的放行，控制误报。
    print("[9] 通用类型检测（正例）")
    tmp = os.path.join(base, "types")
    os.makedirs(tmp)
    page7 = make_env(tmp, "虚构词表项\n")
    positive = [
        ("01:00", "联系 zhang@example.com 处理", "email"),
        ("02:00", "打 13800138000 找他", "phone"),
        ("03:00", "座机 010-12345678", "phone"),
        ("04:00", "key sk-abcdefghijklmnop1234", "credential"),
        ("05:00", "Authorization: Bearer abcdef123456", "credential"),
        ("06:00", "文件在 /home/hyr/.openclaw/workspace", "local-path"),
        ("07:00", "服务器 192.168.1.100 上", "ip-address"),
        ("08:00", "【内部】这段不要公开", "internal-marker"),
    ]
    st7 = state([{"time": t, "kind": "normal", "summary": "内部%s" % t,
                  "public_text": txt} for t, txt, _ in positive])
    action, added, updated, skipped = sc.sync_daily(page7, "2026-09-19", st7)
    got = {s["time"]: s["reason"] for s in skipped}
    for t, txt, why in positive:
        check("类型检测 %s -> %s" % (txt[:22], why), got.get(t) == why, str(got))
    check("类型命中项一条都没发布", added == 0 and ids(page7) == [], str(ids(page7)))
    check("类型命中项只给类别，不回显原文",
          all(set(s.keys()) <= {"time", "reason"} for s in skipped),
          str(skipped))

    # 负向：正常文案不应被类型规则误伤
    print("[9] 通用类型检测（负例，控制误报）")
    tmp = os.path.join(base, "types-neg")
    os.makedirs(tmp)
    page8 = make_env(tmp, "虚构词表项\n")
    negative = [
        ("01:00", "2026年9月21日的安排"),
        ("02:00", "只买了两份甜点，第三组过了"),
        ("03:00", "订单 A1234 已发货"),
        ("04:00", "【今日推荐】限定甜点"),
        ("05:00", "下午 15:30 收工"),
        ("06:00", "编号 20260921001"),
    ]   # 额度为每日 6 条，负例控制在 6 条以内
    st8 = state([{"time": t, "kind": "normal", "summary": "内部%s" % t,
                  "public_text": txt} for t, txt in negative])
    # 每时段一条：要发满 6 条需依次跑 6 次
    for _ in range(len(negative)):
        action, added, updated, skipped = sc.sync_daily(page8, "2026-09-19", st8)
    check("正常文案全部安全发布、无误报",
          len(ids(page8)) == len(negative), "%d/%d" % (len(ids(page8)), len(negative)))
    check("负向样例没有任何一条被拦", skipped == [], str(skipped))

    # 单独确认版本号与裸 IP 写法不被误判（与额度无关，直接用扫描函数）
    for txt in ("版本 1.2.3.4 发布", "更新到 1.2.3.4", "【好耶】今天真不错"):
        ok, why = sc.scan_public_text(txt)
        check("不误伤：%s" % txt, ok, why)

    shutil.rmtree(base, ignore_errors=True)
    print()
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
