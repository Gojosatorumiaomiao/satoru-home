#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布守卫：白名单路径没有真实变化时不创建提交。

背景
----
2026-10-08 20:00 出现两个同名连续提交 `658f3f3` 与 `fd22c1b`；后者与父提交的
tree 完全相同，是一个**空提交**，只制造历史噪声，并可能无意义地触发 Pages 构建。
本仓库的 `scripts/sync_content.py` 本身不创建提交（它只写工作区文件并打印
`"changed"` 报告），提交步骤由发布流程在 git 层直接完成，缺少「提交前的最终树比较」。

设计（按 Issue #3 复核 2026-10-09 早间 P0 收窄）
------------------------------------------------
1. **白名单必填**：`--path` 至少一项；`--preset daily` / `--preset story` 提供固定白名单。
   不再有「省略 paths 即扫描整棵工作树」的过宽默认。
2. **只看白名单、只比 HEAD**：先 `git add -A -- <白名单>`，再用
   `git diff --cached --quiet HEAD -- <白名单>` 判断暂存区相对当前 HEAD 是否有新 tree；
   无差异直接 no-change。**不再依赖 `origin/main` 或任何远端引用**
   （本机 fetch/push 走 https 会超时，远端默认不稳）。
3. **提交只覆盖白名单**：`git commit -m <msg> -- <白名单>`，工作区其他未跟踪文件或
   无关改动不会进入本次发布提交。
4. **dry-run 只读**：`--dry-run` 不调用 `git add`，只用 `git diff --name-only HEAD`
   与 `git ls-files --others --exclude-standard` 探测白名单内改动，**不改动索引**
   （按 Issue #3 复核 2026-10-09 午后 P0 修正）。

用途
----
    python3 scripts/publish_guard.py --preset daily --message "daily: ..." --push
    python3 scripts/publish_guard.py --message "story: ..." \
        --path index.html --path stories.html --path stories/

  - `--path`    本次发布的路径白名单，可重复；必填（或用 `--preset`）。
  - `--preset`  `daily` = daily.html + data/daily-published.json；
                `story` = index.html + stories.html + stories/ + daily.html
                + data/daily-published.json（全量同步会同时更新动态，见 sync_content.py）。
  - `--push`    提交后把当前分支推送到 `--remote`（默认 origin）。
  - `--dry-run` 只做比较与报告，不写索引、不提交、不推送。

行为与退出码
------------
  0  published              已提交（--dry-run 时为 would-publish）
  0  no-change              白名单暂存后与 HEAD 无差异：不提交、不推送
  3  empty-commit-prevented 提交后复核发现 tree 与父相同，已 git reset --soft 回退
  2  用法或 git 操作错误
输出为单行 JSON，便于自动化按 `result` 判断。
"""
import argparse
import json
import os
import subprocess
import sys

PRESETS = {
    # 增量发布日常动态时，sync_content.py 只写这两个文件。
    "daily": ["daily.html", "data/daily-published.json"],
    # 全量同步：故事归档 + 故事页 + 首页入口，并同时同步日常动态。
    "story": ["index.html", "stories.html", "stories/",
              "daily.html", "data/daily-published.json"],
}


class PublishError(Exception):
    """git 调用失败或用法错误。"""


def _run(args, cwd, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise PublishError("命令失败 %s: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
    return p


def tree_of(rev, cwd):
    return _run(["git", "rev-parse", rev + "^{tree}"], cwd).stdout.strip()


def changed_paths(cwd, paths):
    """相对 HEAD、已暂存且落在白名单内的改动路径。"""
    diff = _run(["git", "diff", "--cached", "--name-only", "HEAD", "--"] + list(paths), cwd).stdout
    return sorted({ln.strip() for ln in diff.splitlines() if ln.strip()})


def tracked_changes(cwd, paths):
    """相对 HEAD 的已跟踪改动（含索引与工作树），供 dry-run 只读探测。"""
    diff = _run(["git", "diff", "--name-only", "HEAD", "--"] + list(paths), cwd).stdout
    return sorted({ln.strip() for ln in diff.splitlines() if ln.strip()})


def untracked_files(cwd, paths):
    """白名单内、不被 .gitignore 排除的未跟踪文件，供 dry-run 只读探测。"""
    out = _run(["git", "ls-files", "--others", "--exclude-standard", "--"] + list(paths),
               cwd).stdout
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def pending_changes(cwd, paths):
    """dry-run 只读探测：已跟踪改动 + 白名单内未跟踪文件，均相对 HEAD。"""
    return sorted(set(tracked_changes(cwd, paths)) | set(untracked_files(cwd, paths)))


def commit_is_empty(cwd, rev="HEAD"):
    """HEAD 的 tree 是否与父提交相同（即空提交）。无父提交时返回 False。"""
    parent = _run(["git", "rev-parse", "--verify", rev + "^"], cwd, check=False)
    if parent.returncode != 0:
        return False
    return tree_of(rev, cwd) == tree_of(rev + "^", cwd)


def resolve_paths(preset, extra_paths):
    """合并 preset 与显式 --path，去重并保持顺序。"""
    merged = []
    if preset:
        merged.extend(PRESETS[preset])
    merged.extend(extra_paths or [])
    seen = set()
    out = []
    for p in merged:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def publish(message, paths, cwd, push=False, remote="origin", dry_run=False):
    """白名单暂存 -> 暂存区与 HEAD 比较 -> 提交 -> 可选推送。返回 (result_dict, exit_code)。"""
    paths = [p for p in (paths or []) if p]
    if not paths:
        raise PublishError("白名单必填：请给出 --path，或使用 --preset daily/story")

    out = {
        "result": "no-change",
        "paths": list(paths),
        "changed_paths": [],
        "commit": None,
        "commit_tree": None,
        "parent_tree": None,
        "pushed": False,
        "dry_run": bool(dry_run),
    }

    if dry_run:
        # 只读探测：不调用 git add，不改动索引（Issue #3 复核 2026-10-09 午后 P0）。
        changed = pending_changes(cwd, paths)
        if not changed:
            return out, 0
        out["changed_paths"] = changed
        out["result"] = "would-publish"
        return out, 0

    # 1. 正式发布：只暂存白名单（含白名单内的新增文件与删除）。
    _run(["git", "add", "-A", "--"] + list(paths), cwd)
    # 2. 与当前 HEAD 比较暂存区；有差异才算本次发布。不依赖 origin/main。
    if _run(["git", "diff", "--cached", "--quiet", "HEAD", "--"] + list(paths),
            cwd, check=False).returncode == 0:
        return out, 0

    out["changed_paths"] = changed_paths(cwd, paths)

    if not message:
        raise PublishError("有改动待提交，但未提供 --message")

    # 3. 提交只覆盖白名单：其他暂存内容不进入本次发布提交。
    _run(["git", "commit", "-m", message, "--"] + list(paths), cwd)
    out["commit"] = _run(["git", "rev-parse", "HEAD"], cwd).stdout.strip()
    out["commit_tree"] = tree_of("HEAD", cwd)
    parent = _run(["git", "rev-parse", "--verify", "HEAD^"], cwd, check=False)
    if parent.returncode == 0:
        out["parent_tree"] = tree_of("HEAD^", cwd)

    if commit_is_empty(cwd):
        # 复核：绝不把空提交留在历史里。
        _run(["git", "reset", "--soft", "HEAD^"], cwd)
        out["result"] = "empty-commit-prevented"
        out["commit"] = None
        out["commit_tree"] = None
        return out, 3

    out["result"] = "published"
    if push:
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()
        _run(["git", "push", remote, "HEAD:refs/heads/" + branch], cwd)
        out["pushed"] = True
    return out, 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布守卫：白名单无变化时不创建提交")
    ap.add_argument("--preset", choices=sorted(PRESETS), default=None,
                    help="固定白名单：daily 或 story")
    ap.add_argument("--path", action="append", default=[],
                    help="白名单路径，可重复；与 --preset 合并")
    ap.add_argument("--message", default=None)
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args(argv)

    paths = resolve_paths(args.preset, args.path)
    if not paths:
        ap.error("白名单必填：请给出 --path，或使用 --preset daily/story")
    try:
        result, code = publish(args.message, paths, args.cwd,
                              push=args.push, remote=args.remote, dry_run=args.dry_run)
    except PublishError as e:
        print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
