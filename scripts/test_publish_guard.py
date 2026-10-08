#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_guard.py 的独立测试（全部在 /tmp 临时仓库里跑，不碰真实仓库）。

对应 Issue #3 最新复核（2026-10-08 夜间）P0 的三条验收：
  1. 对同一批输入连续运行两次，第二次不新增 commit；
  2. 内容无变化时返回明确的 no-change；
  3. 复现 2026-10-08 20:00 的空提交场景（fd22c1b）：commit_is_empty 判为真，
     且以父提交为 base 时守卫返回 no-change，不会再造一个空提交。

用法：python3 scripts/test_publish_guard.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_spec = importlib.util.spec_from_file_location(
    "publish_guard", os.path.join(REPO, "scripts", "publish_guard.py"))
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

FAILED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <-- " + str(detail)))
    if not cond:
        FAILED.append(name)


def git(cwd, *args):
    p = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def commit_count(cwd):
    return int(git(cwd, "rev-list", "--count", "HEAD"))


def new_repo():
    d = tempfile.mkdtemp(prefix="publish-guard-")
    git(d, "init", "-q")
    git(d, "config", "user.name", "Guard Test")
    git(d, "config", "user.email", "guard@example.invalid")
    with open(os.path.join(d, "daily.html"), "w", encoding="utf-8") as f:
        f.write("<html>day1</html>\n")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "base")
    return d


def main():
    d = new_repo()
    try:
        # --- 验收 2：无变化 -> no-change，且不新增 commit ---
        n0 = commit_count(d)
        res, code = pg.publish("daily: x", "HEAD", [], d)
        check("no-change 返回明确结果", res["result"] == "no-change", res)
        check("no-change 退出码为 0", code == 0, code)
        check("no-change 不新增 commit", commit_count(d) == n0, commit_count(d))

        # --- 有效发布：只生成一组改动 ---
        with open(os.path.join(d, "daily.html"), "a", encoding="utf-8") as f:
            f.write("<p>new post</p>\n")
        res, code = pg.publish("daily: add post", "HEAD", ["daily.html"], d)
        check("有效发布 result=published", res["result"] == "published", res)
        check("有效发布新增 1 个 commit", commit_count(d) == n0 + 1, commit_count(d))
        check("有效发布的 tree 与父不同", res["commit_tree"] != res["parent_tree"], res)

        # --- 验收 1：同一批输入再跑一次 -> 不新增 commit ---
        n1 = commit_count(d)
        res, code = pg.publish("daily: add post", "HEAD", ["daily.html"], d)
        check("第二次运行 result=no-change", res["result"] == "no-change", res)
        check("第二次运行不新增 commit", commit_count(d) == n1, commit_count(d))

        # --- 未跟踪新文件也算有效改动 ---
        with open(os.path.join(d, "data-new.json"), "w", encoding="utf-8") as f:
            f.write("{}\n")
        res, _ = pg.publish("daily: new file", "HEAD", [], d)
        check("未跟踪新文件触发 published", res["result"] == "published", res)

        # --- --dry-run 不写历史 ---
        with open(os.path.join(d, "daily.html"), "a", encoding="utf-8") as f:
            f.write("<p>dry</p>\n")
        n2 = commit_count(d)
        res, code = pg.publish("daily: dry", "HEAD", ["daily.html"], d, dry_run=True)
        check("dry-run result=would-publish", res["result"] == "would-publish", res)
        check("dry-run 不新增 commit", commit_count(d) == n2, commit_count(d))
        git(d, "checkout", "--", "daily.html")

        # --- 复现 fd22c1b 空提交：检测器为真 ---
        git(d, "commit", "-q", "--allow-empty", "-m", "empty like fd22c1b")
        check("commit_is_empty 识别空提交", pg.commit_is_empty(d) is True, None)

        # 以空提交的父提交为 base：工作区与该 tree 相同 -> no-change，不再造空提交
        n3 = commit_count(d)
        res, code = pg.publish("daily: repeat", "HEAD^", [], d)
        check("空提交场景下守卫返回 no-change", res["result"] == "no-change", res)
        check("空提交场景下不新增 commit", commit_count(d) == n3, commit_count(d))

        # --- 独立复刻 fd22c1b：暂存区为空仍强行提交，检测器应识别 ---
        d2 = new_repo()
        try:
            git(d2, "commit", "-q", "--allow-empty", "-m", "sneaky empty")
            check("手工空提交被检测", pg.commit_is_empty(d2) is True, None)
        finally:
            shutil.rmtree(d2, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print()
    if FAILED:
        print("FAILED: %d 项 -> %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
