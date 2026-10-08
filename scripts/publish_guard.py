#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布守卫：内容没有真实变化时不创建提交。

背景
----
2026-10-08 20:00 出现两个同名连续提交 `658f3f3` 与 `fd22c1b`；后者与父提交的
tree 完全相同，是一个**空提交**，只制造历史噪声，并可能无意义地触发 Pages 构建。
本仓库的 `scripts/sync_content.py` 本身不创建提交（它只写工作区文件并打印
`"changed"` 报告），提交步骤由发布流程在 git 层直接完成，缺少「提交前的最终树比较」。

用途
----
把发布流程的**提交步骤**改为调用本脚本，一次调用完成：最终树比较 -> 提交 -> 可选推送。

    python3 scripts/publish_guard.py --base origin/main \
        --message "daily: ..." --path daily.html --push

  - `--base`   比较基线，默认 `origin/main`（即「当前 main tree」）。
  - `--path`   本次发布的路径，可重复；省略时比较全部已跟踪改动与未跟踪新文件。
  - `--push`   提交后把当前分支推送到 `--remote`（默认 origin）。
  - `--dry-run` 只做比较与报告，不写索引、不提交、不推送。

行为与退出码
------------
  0  published           已提交（--dry-run 时为 would-publish）
  0  no-change           工作区相对 base 在指定路径上无差异：不提交、不推送
  3  empty-commit-prevented 提交后复核发现 tree 与父相同，已 git reset --soft 回退
  2  用法或 git 操作错误
输出为单行 JSON，便于自动化按 `result` 判断。
"""
import argparse
import json
import os
import subprocess
import sys


class PublishError(Exception):
    """git 调用失败或用法错误。"""


def _run(args, cwd, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise PublishError("命令失败 %s: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
    return p


def resolve_commit(rev, cwd):
    """把 rev（分支名/引用/短 SHA）解析成完整 commit SHA。"""
    return _run(["git", "rev-parse", "--verify", rev + "^{commit}"], cwd).stdout.strip()


def tree_of(rev, cwd):
    return _run(["git", "rev-parse", rev + "^{tree}"], cwd).stdout.strip()


def changed_paths(base_commit, paths, cwd):
    """相对 base 的改动：已跟踪文件的差异 + 未跟踪的新文件。

    未给出 paths 时覆盖整棵树；给出时只统计这些路径。
    """
    spec = ["--"] + list(paths) if paths else []
    diff = _run(["git", "diff", "--name-only", base_commit] + spec, cwd).stdout
    others = _run(["git", "ls-files", "--others", "--exclude-standard"] + spec, cwd).stdout
    out = sorted({ln.strip() for ln in diff.splitlines() if ln.strip()} |
                 {ln.strip() for ln in others.splitlines() if ln.strip()})
    return out


def commit_is_empty(cwd, rev="HEAD"):
    """HEAD 的 tree 是否与父提交相同（即空提交）。无父提交时返回 False。"""
    parent = _run(["git", "rev-parse", "--verify", rev + "^"], cwd, check=False)
    if parent.returncode != 0:
        return False
    return tree_of(rev, cwd) == _run(["git", "rev-parse", rev + "^^{tree}"], cwd).stdout.strip()


def publish(message, base, paths, cwd, push=False, remote="origin", dry_run=False):
    """最终树比较 + 提交 + 可选推送。返回 (result_dict, exit_code)。"""
    base_commit = resolve_commit(base, cwd)
    base_tree = tree_of(base_commit, cwd)
    changed = changed_paths(base_commit, paths, cwd)

    out = {
        "result": "no-change",
        "base": base,
        "base_commit": base_commit,
        "base_tree": base_tree,
        "changed_paths": changed,
        "commit": None,
        "commit_tree": None,
        "parent_tree": None,
        "pushed": False,
        "dry_run": bool(dry_run),
    }

    if not changed:
        # 新 tree 与当前 base tree 相同：报告 no-change，不创建 commit、不推进 ref。
        return out, 0

    if dry_run:
        out["result"] = "would-publish"
        return out, 0

    if not message:
        raise PublishError("有改动待提交，但未提供 --message")

    add = ["git", "add", "-A"] + (["--"] + list(paths) if paths else [])
    _run(add, cwd)
    if _run(["git", "diff", "--cached", "--quiet"], cwd, check=False).returncode == 0:
        # 索引里没有实际变化（例如只删了未跟踪文件）：同样按 no-change 处理。
        out["changed_paths"] = []
        return out, 0

    _run(["git", "commit", "-m", message], cwd)
    head = _run(["git", "rev-parse", "HEAD"], cwd).stdout.strip()
    out["commit"] = head
    out["commit_tree"] = tree_of("HEAD", cwd)
    parent = _run(["git", "rev-parse", "HEAD^"], cwd, check=False)
    if parent.returncode == 0:
        out["parent_tree"] = tree_of("HEAD^", cwd)

    if commit_is_empty(cwd):
        # 复核：绝不把空提交留在历史里。
        _run(["git", "reset", "--soft", "HEAD^"], cwd)
        out["result"] = "empty-commit-prevented"
        out["commit"] = None
        return out, 3

    out["result"] = "published"
    if push:
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()
        _run(["git", "push", remote, "HEAD:refs/heads/" + branch], cwd)
        out["pushed"] = True
    return out, 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布守卫：无变化时不创建提交")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--message", default=None)
    ap.add_argument("--path", action="append", default=[])
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args(argv)

    try:
        result, code = publish(args.message, args.base, args.path, args.cwd,
                              push=args.push, remote=args.remote, dry_run=args.dry_run)
    except PublishError as e:
        print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
