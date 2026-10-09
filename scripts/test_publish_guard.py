#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_guard.py 的独立测试（全部在 /tmp 临时仓库里跑，不碰真实仓库）。

覆盖两批复核要求：
  A. Issue #3 复核 2026-10-08 夜间 P0 的三条验收：
     1. 对同一批输入连续运行两次，第二次不新增 commit；
     2. 内容无变化时返回明确的 no-change；
     3. 复现 2026-10-08 20:00 的空提交场景（fd22c1b）：commit_is_empty 判为真。
  B. Issue #3 复核 2026-10-09 早间 P0 的收窄：
     4. --path 必填、preset 固定白名单；
     5. 判断只看白名单、只比 HEAD（不依赖 origin/main）；
     6. 工作区残留无关文件（secret.tmp / 其他改动）时，daily 发布只提交白名单，
        无关文件既不进入 commit，也不出现在 changed_paths。

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

DAILY = pg.PRESETS["daily"]
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


def commit_files(cwd):
    return sorted(git(cwd, "show", "--name-only", "--format=", "HEAD").splitlines())


def status_porcelain(cwd):
    return git(cwd, "status", "--porcelain")


def write(cwd, rel, text):
    p = os.path.join(cwd, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def new_repo():
    """一个含 daily.html / data/daily-published.json / other.txt 的基线仓库。"""
    d = tempfile.mkdtemp(prefix="publish-guard-")
    git(d, "init", "-q")
    git(d, "config", "user.name", "Guard Test")
    git(d, "config", "user.email", "guard@example.invalid")
    write(d, "daily.html", "<html>day1</html>\n")
    write(d, "data/daily-published.json", "{\"published\": []}\n")
    write(d, "other.txt", "unrelated tracked file\n")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "base")
    return d


def test_presets():
    check("preset daily 为两项且顺序固定",
          pg.PRESETS["daily"] == ["daily.html", "data/daily-published.json"],
          pg.PRESETS["daily"])
    check("preset story 含 index.html 与 stories.html",
          "index.html" in pg.PRESETS["story"] and "stories.html" in pg.PRESETS["story"],
          pg.PRESETS["story"])
    check("resolve_paths 合并 preset 与 --path 并去重",
          pg.resolve_paths("daily", ["daily.html", "extra.html"]) ==
          ["daily.html", "data/daily-published.json", "extra.html"],
          pg.resolve_paths("daily", ["daily.html", "extra.html"]))
    check("publish 无白名单时抛 PublishError",
          _raises(lambda: pg.publish("m", [], os.getcwd())))


def _raises(fn):
    try:
        fn()
        return False
    except pg.PublishError:
        return True


def test_daily_flow():
    d = new_repo()
    try:
        # 验收 2：无变化 -> no-change，不新增 commit
        n0 = commit_count(d)
        res, code = pg.publish("daily: x", DAILY, d)
        check("no-change 返回明确结果", res["result"] == "no-change", res)
        check("no-change 退出码为 0", code == 0, code)
        check("no-change 不新增 commit", commit_count(d) == n0, commit_count(d))

        # 有效发布：只改 daily.html
        write(d, "daily.html", "<html>day1</html><p>new post</p>\n")
        res, code = pg.publish("daily: add post", DAILY, d)
        check("有效发布 result=published", res["result"] == "published", res)
        check("有效发布新增 1 个 commit", commit_count(d) == n0 + 1, commit_count(d))
        check("有效发布 tree 与父不同", res["commit_tree"] != res["parent_tree"], res)
        check("changed_paths 精确等于 daily.html", res["changed_paths"] == ["daily.html"], res)

        # 验收 1：同一批输入再跑一次 -> 不新增 commit
        n1 = commit_count(d)
        res, code = pg.publish("daily: add post", DAILY, d)
        check("第二次运行 result=no-change", res["result"] == "no-change", res)
        check("第二次运行不新增 commit", commit_count(d) == n1, commit_count(d))

        # 白名单内的新文件也会触发发布
        write(d, "data/newlog.json", "{}\n")
        res, _ = pg.publish("daily: whitelist new file", DAILY + ["data/newlog.json"], d)
        check("白名单内新文件触发 published", res["result"] == "published", res)

        # --dry-run 不写历史
        write(d, "daily.html", "<html>day1</html><p>new post</p><p>dry</p>\n")
        n2 = commit_count(d)
        res, code = pg.publish("daily: dry", DAILY, d, dry_run=True)
        check("dry-run result=would-publish", res["result"] == "would-publish", res)
        check("dry-run 不新增 commit", commit_count(d) == n2, commit_count(d))
        git(d, "checkout", "--", "daily.html")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_unrelated_workspace_files():
    """复核 2026-10-09 早间要求的新测试：无关文件不进入发布提交。"""
    d = new_repo()
    try:
        n0 = commit_count(d)
        # 工作区同时存在：未跟踪 secret.tmp、未跟踪目录 temp/、被修改的 other.txt
        write(d, "secret.tmp", "should never be committed\n")
        write(d, "temp/scratch.txt", "scratch\n")
        write(d, "other.txt", "unrelated tracked file CHANGED\n")
        # 本次 daily 发布真正改动的白名单文件
        write(d, "daily.html", "<html>day1</html><p>pub1</p>\n")

        res, code = pg.publish("daily: real publish", DAILY, d)
        check("残留文件场景仍能发布", res["result"] == "published", res)
        check("changed_paths 只含白名单改动", res["changed_paths"] == ["daily.html"], res)
        check("commit 只含 daily.html", commit_files(d) == ["daily.html"], commit_files(d))
        porcelain = status_porcelain(d)
        check("secret.tmp 仍为未跟踪", "?? secret.tmp" in porcelain, porcelain)
        check("temp/ 仍未跟踪", "?? temp/" in porcelain, porcelain)
        check("other.txt 未被提交且仍有改动", "other.txt" not in commit_files(d)
              and "other.txt" in porcelain, porcelain)
        check("未跟踪无关文件未进入 changed_paths",
              all("secret.tmp" not in p and "temp" not in p for p in res["changed_paths"]),
              res["changed_paths"])
        check("本次仍只新增 1 个 commit", commit_count(d) == n0 + 1, commit_count(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_empty_commit_detection():
    d = new_repo()
    try:
        git(d, "commit", "-q", "--allow-empty", "-m", "empty like fd22c1b")
        check("commit_is_empty 识别空提交", pg.commit_is_empty(d) is True, None)
        d2 = new_repo()
        try:
            write(d2, "daily.html", "<html>day1</html><p>x</p>\n")
            n = commit_count(d2)
            res, code = pg.publish("daily: x", DAILY, d2)
            check("非空提交不被误判", pg.commit_is_empty(d2) is False and res["commit"] is not None,
                  res)
            check("非空提交退出码 0", code == 0, code)
        finally:
            shutil.rmtree(d2, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cli_requires_path():
    script = os.path.join(REPO, "scripts", "publish_guard.py")
    d = new_repo()
    try:
        p = subprocess.run([sys.executable, script, "--message", "x", "--cwd", d],
                           capture_output=True, text=True)
        check("CLI 缺少白名单时非零退出", p.returncode != 0, p.returncode)
        p2 = subprocess.run([sys.executable, script, "--preset", "daily", "--dry-run",
                             "--message", "x", "--cwd", d],
                            capture_output=True, text=True)
        check("CLI preset daily 可用且无变化", '"result": "no-change"' in p2.stdout, p2.stdout)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_presets()
    test_daily_flow()
    test_unrelated_workspace_files()
    test_empty_commit_detection()
    test_cli_requires_path()
    print()
    if FAILED:
        print("FAILED: %d 项 -> %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
