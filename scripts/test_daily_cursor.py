#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日常动态「发布游标」回归测试（全部虚构数据，自带隔离禁词表）。

对应 PR #13 复核 1 · P1：过滤后的数组下标不能与历史记账长度充当同一个游标。
过滤结果会随 public_text 补齐 / 禁词表变化而改变，因此必须用
**稳定时刻**判断「是否已记账」，并从「合格且未记账」的条目里按时间取最早一条。

验收（复核原文）：
  A. 先跳过早条目、发布晚条目、再补早条目 —— 新合格条目可发布一次
  B. 已有旧记账、旧条目已被过滤的迁移场景 —— 后续合格条目不被卡住
  C. 跳过项不占名额、已发布项不重复
  D. 每天上限仍然生效
  E. 已撤下的记录不复活，页面上已有条目不被静默删除
  F. 无法扫描（禁词表缺失）时，页面与账本都不变

本脚本只在临时目录内造数据；禁词表由本脚本自建为临时文件并显式注入
环境变量，不读取操作者本机词表，也不读真实状态文件。
用法：python3 scripts/test_daily_cursor.py
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sc", os.path.join(HERE, "sync_content.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

DATE = "2099-03-05"          # 虚构日期，避开任何真实数据
PASS = FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILED.append(name)
        print("  FAIL  %s %s" % (name, extra))


def setup(tmp, terms="绝密代号\n"):
    """在 tmp 下建一个最小站点 + 自带虚构禁词表。返回 (page, ledger)。"""
    bl = os.path.join(tmp, "blocklist.txt")
    with open(bl, "w", encoding="utf-8") as fh:
        fh.write(terms)
    os.environ["SATORU_BLOCKLIST"] = bl
    sc.BLOCKLIST_FILE = bl
    ledger = os.path.join(tmp, "published.json")
    sc.PUBLISHED_LOG = ledger
    page = os.path.join(tmp, "daily.html")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write("<!-- daily:list:start -->\n<!-- daily:list:end -->\n")
    return page, ledger


def use_missing_blocklist(tmp):
    """把禁词表指到一个不存在的路径（模拟本机词表缺失）。"""
    os.environ["SATORU_BLOCKLIST"] = os.path.join(tmp, "no-such-blocklist.txt")
    sc.BLOCKLIST_FILE = os.environ["SATORU_BLOCKLIST"]


def ids(page):
    with open(page, encoding="utf-8") as fh:
        return sorted(re.findall(r'id="(daily-[^"]*)"', fh.read()))


def page_bytes(page):
    with open(page, "rb") as fh:
        return fh.read()


def read_ledger(ledger):
    if not os.path.exists(ledger):
        return None
    return json.load(open(ledger, encoding="utf-8"))


def write_ledger(ledger, obj):
    with open(ledger, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def state(contacts):
    return {"date": DATE, "contacts": contacts}


def c(t, summary=None, public=None, kind="normal"):
    o = {"kind": kind, "time": t, "slot": "s"}
    if summary is not None:
        o["summary"] = summary
    if public is not None:
        o["public_text"] = public
    return o


def id_of(t):
    return "daily-%s-%s" % (DATE, t.replace(":", ""))


def run():
    base = tempfile.mkdtemp(prefix="cursor-test-")

    # ---------- A：复核给出的复现路径 ----------
    print("[A] 先跳过早条目、发布晚条目、再补早条目")
    tmp = os.path.join(base, "A")
    os.makedirs(tmp)
    page, ledger = setup(tmp)
    st1 = state([c("09:00", summary="内部摘要A（无公开稿）"),
                 c("12:00", public="公开稿B")])
    action, added, updated, skipped = sc.sync_daily(page, DATE, st1)
    check("A1 早条目无 public_text 时被跳过并点名",
          [s["time"] for s in skipped if s["reason"] == "no-public-text"] == ["09:00"],
          str(skipped))
    check("A2 晚条目照常发布", added == 1 and id_of("12:00") in ids(page),
          "added=%d ids=%s" % (added, ids(page)))
    check("A3 记账只含 12:00", read_ledger(ledger) == {DATE: ["12:00"]},
          str(read_ledger(ledger)))

    st2 = state([c("09:00", summary="内部摘要A（无公开稿）", public="补上的公开稿A"),
                 c("12:00", public="公开稿B")])
    action, added, updated, skipped = sc.sync_daily(page, DATE, st2)
    check("A4 补上 public_text 后早条目可发布一次",
          added == 1 and id_of("09:00") in ids(page),
          "added=%d ids=%s" % (added, ids(page)))
    check("A5 记账补齐两条", read_ledger(ledger) == {DATE: ["09:00", "12:00"]},
          str(read_ledger(ledger)))

    before = ids(page)
    action, added, updated, skipped = sc.sync_daily(page, DATE, st2)
    check("A6 再跑不重复发布", added == 0 and ids(page) == before,
          "added=%d ids=%s" % (added, ids(page)))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- B：迁移场景（旧记账仍在，旧条目已被过滤） ----------
    print("[B] 已有旧记账但旧条目被过滤的迁移场景")
    tmp = os.path.join(base, "B")
    os.makedirs(tmp)
    page, ledger = setup(tmp)
    write_ledger(ledger, {DATE: ["09:00", "12:00"]})
    sc.sync_daily(page, DATE, state([c("12:00", public="公开稿B")]))  # 先在页面放上 12:00
    st = state([c("09:00", summary="内部摘要A（public_text 已撤回）"),
                c("12:00", public="公开稿B"),
                c("15:00", public="公开稿C")])
    action, added, updated, skipped = sc.sync_daily(page, DATE, st)
    check("B1 旧条目被过滤后，新合格条目仍可发布",
          added == 1 and id_of("15:00") in ids(page),
          "added=%d ids=%s" % (added, ids(page)))
    check("B2 记账追加 15:00，旧记账不丢",
          read_ledger(ledger) == {DATE: ["09:00", "12:00", "15:00"]},
          str(read_ledger(ledger)))
    before = ids(page)
    action, added, updated, skipped = sc.sync_daily(page, DATE, st)
    check("B3 迁移后重跑不重复", added == 0 and ids(page) == before,
          "added=%d ids=%s" % (added, ids(page)))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- C/D：跳过不占名额 + 每天上限 ----------
    print("[C/D] 跳过项不占名额；每天上限生效；按时间取最早一条")
    tmp = os.path.join(base, "CD")
    os.makedirs(tmp)
    page, ledger = setup(tmp)
    st = state([c("09:00", summary="无公开稿"),
                c("12:00", public="公开稿B"),
                c("15:00", public="公开稿C"),
                c("18:00", public="公开稿D")])
    seq = []
    for i in range(4):
        action, added, updated, skipped = sc.sync_daily(page, DATE, st, max_per_day=2)
        seq.append(added)
    check("C1 每次最多新增一条", seq[:3] == [1, 1, 0], str(seq))
    check("C2 按时间顺序取最早一条",
          ids(page) == [id_of("12:00"), id_of("15:00")], str(ids(page)))
    check("C3 达到当天上限后不再新增",
          read_ledger(ledger) == {DATE: ["12:00", "15:00"]},
          str(read_ledger(ledger)))
    check("C4 跳过的 09:00 仍被点名，未占名额",
          [s["time"] for s in skipped if s["reason"] == "no-public-text"] == ["09:00"],
          str(skipped))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- E：已撤下的记录不复活，页面已有条目不被删 ----------
    print("[E] 已撤下的记录不复活、页面已有条目保留")
    tmp = os.path.join(base, "E")
    os.makedirs(tmp)
    page, ledger = setup(tmp)
    # 人工预置：账本记过 09:00，页面上也已经有这一条（模拟已发布过的历史条目）
    write_ledger(ledger, {DATE: ["09:00"]})
    with open(page, "w", encoding="utf-8") as fh:
        fh.write("<!-- daily:list:start -->\n"
                 + sc.render_daily_entry(DATE, "09:00", "早先的公开稿A")
                 + "\n<!-- daily:list:end -->\n")
    check("E1 预置：页面已有 09:00", ids(page) == [id_of("09:00")], str(ids(page)))

    st = state([c("09:00", summary="公开稿A 已撤回，仅剩内部摘要"),
                c("12:00", public="公开稿B")])
    action, added, updated, skipped = sc.sync_daily(page, DATE, st)
    check("E2 撤回后不复活旧的 09:00",
          ids(page).count(id_of("09:00")) == 1, str(ids(page)))
    check("E3 新条目 12:00 正常发布", id_of("12:00") in ids(page), str(ids(page)))
    check("E4 记账保留 09:00 并追加 12:00",
          read_ledger(ledger) == {DATE: ["09:00", "12:00"]},
          str(read_ledger(ledger)))
    action, added, updated, skipped = sc.sync_daily(page, DATE, st)
    check("E5 重跑仍不复活、不重复",
          added == 0 and ids(page).count(id_of("09:00")) == 1,
          "added=%d ids=%s" % (added, ids(page)))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- F：无法扫描时页面与账本都不变 ----------
    print("[F] 禁词表缺失（无法扫描）时页面与账本不变")
    tmp = os.path.join(base, "F")
    os.makedirs(tmp)
    page, ledger = setup(tmp, terms="绝密代号\n")
    sc.sync_daily(page, DATE, state([c("09:00", public="早先的公开稿A")]))
    page_before, ledger_before = page_bytes(page), read_ledger(ledger)
    use_missing_blocklist(tmp)
    action, added, updated, skipped = sc.sync_daily(
        page, DATE, state([c("09:00", public="早先的公开稿A"), c("12:00", public="公开稿B")]))
    check("F1 无法扫描 -> 不新增", added == 0, "added=%d" % added)
    check("F2 原因报告为 no-blocklist",
          skipped and all(s["reason"] == "no-blocklist" for s in skipped), str(skipped))
    check("F3 页面逐字未变", page_bytes(page) == page_before)
    check("F4 账本未变", read_ledger(ledger) == ledger_before,
          str(read_ledger(ledger)))
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d 项检查：%d OK / %d FAIL" % (PASS + FAIL, PASS, FAIL))
    if FAILED:
        print("失败项：" + "；".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
