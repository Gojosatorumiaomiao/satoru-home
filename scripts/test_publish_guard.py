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
  C. Issue #3 复核 2026-10-09 午后 P0：
     7. --dry-run 不改动索引：调用前后 `git diff --cached --name-status` 与
        `git status --porcelain` 逐字节一致；原本未暂存的 daily.html 仍未暂存，
        原本已暂存的无关文件仍已暂存；dry-run 能读到白名单内未跟踪文件但不暂存。
  D. Issue #3 复核 2026-10-09 夜间 P0（本地 bare remote，不依赖公网）：
     8. 推送被远端拒绝 -> result=push-pending、退出码 4，已创建的 commit 保留并记录 SHA，
        远端 tip 不前进；
     9. 恢复远端后用同一输入重跑 -> 先补推、不落进 no-change，远端 tip 到达该 commit，
        不产生第二个 commit，无关暂存内容不变；补推成功后状态被清理。
  E. Issue #3 复核 2026-10-10 早间 P0（本地 bare remote，不依赖公网）：
    10. 补推只推「记录中的提交」：本地再有后继提交 Q 时，远端 tip 也只到 P，不带 Q；
    11. 切到另一条不含该提交的分支后重跑 -> result=push-pending-conflict、退出码 5，
        不把该分支 HEAD 推到记录的目标分支，记录保留；
    12. 记录的提交不在当前 HEAD 历史（本地 reset）时，不静默清理、不落进 no-change，
        保留记录并返回明确冲突；
    13. 若已确认该提交在目标远端，则清理记录、不报冲突。
  F. Issue #3 复核 2026-10-10 下午 P0（本地 bare remote，不依赖公网）：
    14. pending 必须在 push 之前原子落盘：在「记录已落盘、push 刚开始」处硬杀子进程，
        记录仍存在，下一轮发现并只补推原 commit，不重建提交；
    15. 记录损坏（截断 JSON / 非 JSON 对象）时失败封闭：push-pending-conflict、退出码 5，
        不创建新提交、不清理原文件、不推送；dry-run 同样失败封闭且只读。

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


def git_raw(cwd, *args):
    """不 strip 的 git 输出，用于逐字节比较索引/工作区状态。"""
    p = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def status_porcelain(cwd):
    return git_raw(cwd, "status", "--porcelain")


def cached_name_status(cwd):
    return git_raw(cwd, "diff", "--cached", "--name-status")


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


def new_work_repo_with_remote():
    """工作仓库 + 本地 bare remote（基线已推送）。返回 (work, bare, branch)。"""
    work = new_repo()
    bare = tempfile.mkdtemp(prefix="publish-guard-remote-")
    git(bare, "init", "--bare", "-q")
    git(work, "remote", "add", "origin", bare)
    branch = git(work, "rev-parse", "--abbrev-ref", "HEAD")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/" + branch)
    return work, bare, branch


def set_reject_hook(bare, reject):
    """在 bare remote 上装/拆 pre-receive 钩子，用来制造推送拒绝。"""
    hook = os.path.join(bare, "hooks", "pre-receive")
    if reject:
        with open(hook, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
    elif os.path.exists(hook):
        os.remove(hook)


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


def test_dry_run_does_not_touch_index():
    """复核 2026-10-09 午后 P0：--dry-run 不得写索引。"""
    d = new_repo()
    try:
        # 三类状态同时存在：
        #  - daily.html 工作树改动、未暂存（dry-run 后必须仍未暂存）
        #  - other.txt 已暂存（无关改动，dry-run 后必须仍已暂存）
        #  - secret.tmp 未跟踪（dry-run 后必须仍未跟踪）
        write(d, "daily.html", "<html>day1</html><p>dry-no-index</p>\n")
        write(d, "other.txt", "staged unrelated change\n")
        git(d, "add", "--", "other.txt")
        write(d, "secret.tmp", "untracked\n")

        cached0 = cached_name_status(d)
        porcelain0 = status_porcelain(d)
        check("前置：other.txt 已暂存", cached0.splitlines() == ["M\tother.txt"], cached0)
        check("前置：daily.html 未暂存", " M daily.html" in porcelain0.splitlines(), porcelain0)

        n0 = commit_count(d)
        res, code = pg.publish("daily: dry", DAILY, d, dry_run=True)
        check("dry-run result=would-publish", res["result"] == "would-publish", res)
        check("dry-run 退出码 0", code == 0, code)
        check("dry-run 不新增 commit", commit_count(d) == n0, commit_count(d))
        check("dry-run 报告 daily.html", res["changed_paths"] == ["daily.html"], res["changed_paths"])
        check("dry-run 前后 git diff --cached --name-status 逐字节一致",
              cached_name_status(d) == cached0, (cached_name_status(d), cached0))
        check("dry-run 前后 git status --porcelain 逐字节一致",
              status_porcelain(d) == porcelain0, (status_porcelain(d), porcelain0))
        check("dry-run 后 daily.html 仍未暂存",
              " M daily.html" in status_porcelain(d).splitlines(), status_porcelain(d))
        check("dry-run 后 other.txt 仍已暂存",
              "M  other.txt" in status_porcelain(d).splitlines(), status_porcelain(d))
        check("dry-run 后 secret.tmp 仍未跟踪",
              "?? secret.tmp" in status_porcelain(d).splitlines(), status_porcelain(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dry_run_detects_untracked_without_staging():
    """复核 2026-10-09 午后 P0：dry-run 读到白名单内未跟踪文件但不暂存。"""
    d = new_repo()
    try:
        write(d, "data/newlog.json", "{}\n")
        porcelain0 = status_porcelain(d)
        cached0 = cached_name_status(d)
        res, code = pg.publish("x", DAILY + ["data/newlog.json"], d, dry_run=True)
        check("dry-run 读到白名单内未跟踪文件 -> would-publish",
              res["result"] == "would-publish", res)
        check("dry-run 报告含未跟踪文件", res["changed_paths"] == ["data/newlog.json"],
              res["changed_paths"])
        check("dry-run 后未跟踪文件仍未被暂存",
              status_porcelain(d) == porcelain0 and cached_name_status(d) == cached0,
              status_porcelain(d))
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


def test_push_pending_retry():
    """复核 2026-10-09 夜间 P0：push 失败不丢状态，下次运行先补推（本地 bare remote）。"""
    work, bare, branch = new_work_repo_with_remote()
    try:
        base_tip = git(bare, "rev-parse", "refs/heads/" + branch)
        # 无关改动先暂存：验证重试不会破坏暂存区。
        write(work, "other.txt", "staged unrelated change\n")
        git(work, "add", "--", "other.txt")
        cached_before = cached_name_status(work)

        # 1) 远端拒绝推送：提交已创建、推送未确认。
        set_reject_hook(bare, True)
        write(work, "daily.html", "<html>day1</html><p>push-pending</p>\n")
        n_before = commit_count(work)
        res, code = pg.publish("daily: pending", DAILY, work, push=True)
        check("push 被拒时 result=push-pending", res["result"] == "push-pending", res)
        check("push 被拒时退出码 4", code == pg.PENDING_EXIT, code)
        check("push 被拒时已创建提交并记录 SHA",
              res["commit"] is not None and res["pending_push"] is True, res)
        check("push 被拒时带 push_error 字段", bool(res.get("push_error")), res)
        check("push 被拒时本地新增 1 个 commit", commit_count(work) == n_before + 1,
              commit_count(work))
        check("push 被拒时远端 tip 未前进",
              git(bare, "rev-parse", "refs/heads/" + branch) == base_tip,
              git(bare, "rev-parse", "refs/heads/" + branch))
        pending_commit = res["commit"]
        check("待推送状态已落盘", pg.load_pending(work) is not None, pg.load_pending(work))

        # 1b) 有遗留言而未给 --push：仍报告 push-pending，远端不受影响。
        res_np, code_np = pg.publish("daily: pending", DAILY, work, push=False)
        check("未给 --push 时仍报告 push-pending",
              res_np["result"] == "push-pending" and code_np == pg.PENDING_EXIT, res_np)
        check("未给 --push 时不触碰远端",
              git(bare, "rev-parse", "refs/heads/" + branch) == base_tip, base_tip)

        # 1c) dry-run 在有待推送提交时仍只读、不推送。
        res_dry, _ = pg.publish("daily: pending", DAILY, work, dry_run=True)
        check("dry-run 读出待推送状态且不推送",
              res_dry["pending_push"] is True
              and git(bare, "rev-parse", "refs/heads/" + branch) == base_tip, res_dry)

        # 2) 恢复远端、同一输入重跑：必须补推，而不是返回 no-change。
        set_reject_hook(bare, False)
        n_after = commit_count(work)
        res2, code2 = pg.publish("daily: pending", DAILY, work, push=True)
        check("重跑不再落进 no-change", res2["result"] != "no-change", res2)
        check("重跑补推成功标记 pushed", res2["pushed"] is True, res2)
        check("重跑不产生第二个 commit", commit_count(work) == n_after, commit_count(work))
        check("远端 tip 已到达该提交",
              git(bare, "rev-parse", "refs/heads/" + branch) == pending_commit,
              (git(bare, "rev-parse", "refs/heads/" + branch), pending_commit))
        check("重跑后待推送状态已清理", pg.load_pending(work) is None, pg.load_pending(work))
        check("重跑保留无关暂存内容", cached_name_status(work) == cached_before,
              (cached_name_status(work), cached_before))

        # 3) 无遗留、无新改动：回到 no-change，不新增 commit。
        n3 = commit_count(work)
        res3, _ = pg.publish("daily: pending", DAILY, work, push=True)
        check("清理后无变化回到 no-change", res3["result"] == "no-change", res3)
        check("清理后不新增 commit", commit_count(work) == n3, commit_count(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


def _pending_setup():
    """工作仓库 + 本地 bare remote，制造一次 push 被拒的 pending。

    返回 (work, bare, branch, pending_commit, base_tip)。
    """
    work, bare, branch = new_work_repo_with_remote()
    base_tip = git(bare, "rev-parse", "refs/heads/" + branch)
    set_reject_hook(bare, True)
    write(work, "daily.html", "<html>day1</html><p>pending-x</p>\n")
    res, code = pg.publish("daily: pending", DAILY, work, push=True)
    if res["result"] != "push-pending" or code != pg.PENDING_EXIT:
        raise RuntimeError("pending_setup 失败: %r %r" % (res, code))
    return work, bare, branch, res["commit"], base_tip


def test_pending_retry_targets_recorded_commit():
    """复核 2026-10-10 早间 P0：补推只推记录的提交，不把后继提交 Q 一起推。"""
    work, bare, branch, pending_commit, base_tip = _pending_setup()
    try:
        git(work, "commit", "-q", "--allow-empty", "-m", "successor Q")
        q = git(work, "rev-parse", "HEAD")
        check("前置：后继提交 Q 与 P 不同", q != pending_commit, (q, pending_commit))
        set_reject_hook(bare, False)
        res, code = pg.publish("daily: pending", DAILY, work, push=True)
        tip = git(bare, "rev-parse", "refs/heads/" + branch)
        check("重跑不再返回 no-change", res["result"] != "no-change", res)
        check("补推后远端 tip 是记录的 P", tip == pending_commit, (tip, pending_commit))
        check("补推未把后继 Q 推上去", tip != q, (tip, q))
        check("重跑后待推送状态已清理", pg.load_pending(work) is None, pg.load_pending(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


def test_pending_on_other_branch_not_pushed():
    """复核 2026-10-10 早间 P0：切到另一条不含 P 的分支后重跑，不得把该分支 HEAD 推上去。"""
    work, bare, branch, pending_commit, base_tip = _pending_setup()
    try:
        base_commit = git(work, "rev-parse", "HEAD~1")
        git(work, "checkout", "-q", "-b", "other-branch", base_commit)
        write(work, "other.txt", "other branch work\n")
        git(work, "commit", "-q", "-am", "other branch commit")
        other_head = git(work, "rev-parse", "HEAD")
        set_reject_hook(bare, False)
        res, code = pg.publish("daily: pending", DAILY, work, push=True)
        tip = git(bare, "rev-parse", "refs/heads/" + branch)
        check("切分支后返回 push-pending-conflict",
              res["result"] == "push-pending-conflict", res)
        check("切分支后退出码 5", code == pg.PUSH_CONFLICT_EXIT, code)
        check("切分支后目标远端 tip 不变", tip == base_tip, (tip, base_tip))
        check("切分支后未把该分支 HEAD 推上去", tip != other_head, (tip, other_head))
        check("切分支后保留待推送记录", pg.load_pending(work) is not None, pg.load_pending(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


def test_pending_conflict_keeps_state_after_reset():
    """复核 2026-10-10 早间 P0：提交不在 HEAD 历史时不静默清理、不落进 no-change。"""
    work, bare, branch, pending_commit, base_tip = _pending_setup()
    try:
        n_p = commit_count(work)
        git(work, "reset", "--hard", "HEAD~1")
        set_reject_hook(bare, False)
        res, code = pg.publish("daily: pending", DAILY, work, push=True)
        check("reset 后返回 push-pending-conflict",
              res["result"] == "push-pending-conflict", res)
        check("reset 后退出码 5", code == pg.PUSH_CONFLICT_EXIT, code)
        check("reset 后不落进 no-change", res["result"] != "no-change", res)
        check("reset 后不新增 commit", commit_count(work) == n_p - 1, commit_count(work))
        check("reset 后保留待推送记录", pg.load_pending(work) is not None, pg.load_pending(work))
        check("reset 后远端 tip 未前进",
              git(bare, "rev-parse", "refs/heads/" + branch) == base_tip, base_tip)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


def test_pending_conflict_resolves_when_already_on_remote():
    """复核 2026-10-10 早间 P0：已确认提交在目标远端时，允许清理记录。"""
    work, bare, branch, pending_commit, base_tip = _pending_setup()
    try:
        set_reject_hook(bare, False)
        git(work, "push", "-q", "origin", pending_commit + ":refs/heads/" + branch)
        git(work, "reset", "--hard", "HEAD~1")
        res, code = pg.publish("daily: pending", DAILY, work, push=True)
        check("已在远端时清理待推送记录", pg.load_pending(work) is None, pg.load_pending(work))
        check("已在远端时不报冲突", res["result"] != "push-pending-conflict", res)
        check("已在远端时远端 tip 仍是 P",
              git(bare, "rev-parse", "refs/heads/" + branch) == pending_commit, pending_commit)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


def test_pending_written_before_push_survives_kill():
    """复核 2026-10-10 下午 P0：pending 必须在 push 之前原子落盘。

    子进程跑到「记录已落盘、push 刚开始」时被硬杀（os._exit），
    下一轮必须发现记录并只补推原 commit。
    """
    work, bare, branch = new_work_repo_with_remote()
    try:
        base_tip = git(bare, "rev-parse", "refs/heads/" + branch)
        write(work, "daily.html", "<html>day1</html><p>kill-window</p>")
        guard = os.path.join(REPO, "scripts", "publish_guard.py")
        script = "\n".join([
            "import importlib.util, os",
            "spec = importlib.util.spec_from_file_location('pg', %r)" % guard,
            "pg = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(pg)",
            "def hard_kill(cwd, remote, branch, source_rev='HEAD'):",
            "    os._exit(9)",
            "pg._push = hard_kill",
            "pg.publish('daily: kill-window', pg.PRESETS['daily'], %r, push=True)" % work,
        ])
        p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        check("子进程在 push 阶段被硬终止（exit 9）", p.returncode == 9, (p.returncode, p.stderr))
        committed = git(work, "rev-parse", "HEAD")
        rec = pg.load_pending(work)
        check("硬终止后 pending 记录仍然存在", rec is not None, rec)
        check("记录锁定刚创建的提交", bool(rec) and rec.get("commit") == committed,
              (rec, committed))
        check("硬终止时远端 tip 未前进",
              git(bare, "rev-parse", "refs/heads/" + branch) == base_tip, base_tip)

        n = commit_count(work)
        res, code = pg.publish("daily: kill-window", DAILY, work, push=True)
        tip = git(bare, "rev-parse", "refs/heads/" + branch)
        check("下一轮不落进 no-change", res["result"] != "no-change", res)
        check("下一轮补推到记录的提交", tip == committed, (tip, committed))
        check("下一轮不重建提交", commit_count(work) == n, commit_count(work))
        check("下一轮清理记录", pg.load_pending(work) is None, pg.load_pending(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


def test_corrupt_pending_fails_closed():
    """复核 2026-10-10 下午 P0：记录损坏时必须失败封闭。"""
    work, bare, branch = new_work_repo_with_remote()
    try:
        base_tip = git(bare, "rev-parse", "refs/heads/" + branch)
        sp = pg.state_path(work)
        with open(sp, "w", encoding="utf-8") as f:
            f.write('{"commit": "deadbeef"')
        before = open(sp, "rb").read()
        write(work, "daily.html", "<html>day1</html><p>corrupt</p>")
        n0 = commit_count(work)

        res, code = pg.publish("daily: corrupt", DAILY, work, push=True)
        check("损坏记录 -> push-pending-conflict", res["result"] == "push-pending-conflict", res)
        check("损坏记录 -> 退出码 5", code == pg.PUSH_CONFLICT_EXIT, code)
        check("损坏记录不创建新提交", commit_count(work) == n0, commit_count(work))
        check("损坏记录不被清理", open(sp, "rb").read() == before, open(sp, "rb").read())
        check("损坏记录不推送",
              git(bare, "rev-parse", "refs/heads/" + branch) == base_tip, base_tip)

        res_dry, code_dry = pg.publish("daily: corrupt", DAILY, work, push=True, dry_run=True)
        check("dry-run 也报 push-pending-conflict",
              res_dry["result"] == "push-pending-conflict", res_dry)
        check("dry-run 退出码 5", code_dry == pg.PUSH_CONFLICT_EXIT, code_dry)
        check("dry-run 后损坏记录仍在", open(sp, "rb").read() == before)
        check("dry-run 不新增提交", commit_count(work) == n0)

        with open(sp, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        res2, code2 = pg.publish("daily: corrupt", DAILY, work, push=True)
        check("非对象记录也算损坏",
              res2["result"] == "push-pending-conflict" and code2 == pg.PUSH_CONFLICT_EXIT, res2)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bare, ignore_errors=True)


def main():
    test_presets()
    test_daily_flow()
    test_unrelated_workspace_files()
    test_dry_run_does_not_touch_index()
    test_dry_run_detects_untracked_without_staging()
    test_empty_commit_detection()
    test_cli_requires_path()
    test_push_pending_retry()
    test_pending_retry_targets_recorded_commit()
    test_pending_on_other_branch_not_pushed()
    test_pending_conflict_keeps_state_after_reset()
    test_pending_conflict_resolves_when_already_on_remote()
    test_pending_written_before_push_survives_kill()
    test_corrupt_pending_fails_closed()
    print()
    if FAILED:
        print("FAILED: %d 项 -> %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
